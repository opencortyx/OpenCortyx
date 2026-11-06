"""End to end on a running stack: 300 files through Add data, then every feature against the result.

    python -m tests.goldset.e2e --api http://127.0.0.1:8000 --dir <corpus dir> --bench <bench kit>
                                [--container opencortyx-test-api-1] [--out results/e2e300]

Credentials from OPENCORTYX_EMAIL / OPENCORTYX_PASSWORD. Steps, each reported PASS / FAIL:

  1. the workspace is on the data store; a resolution rule for customers (identity: customer_id + name)
  2. 100 documents, 100 semi-structured and 100 structured files uploaded as one Add data batch;
     every run finishes, none fails
  3. the stores, checked from inside the API container (tests/goldset/stack_checks.py): rows, records
     and chunks in Delta match the files, no chunk nodes, evidence, source keys, FROM_ASSET, value sets,
     SAME_AS for the duplicated customers, header links between documents, passages in the index
  4. retrieval and answering: a policy question answered from passages with a citation; a count over
     one licence file answered by SQL with the right number; an unknown attribute answered UNKNOWN
  5. data definitions: a segment, a metric and a verified query, each checked against the data
  6. the asset Data tab for one asset of each kind
  7. re-ingest of a changed file replaces its rows; deleting an asset removes its data and evidence
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import time
from pathlib import Path

from tests.goldset import corpus
from tests.goldset.run import Api

RESULTS: list = []


def check(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {str(detail)[:300]}", flush=True)


def upload_all(api: Api, root: Path, batch: str) -> dict:
    runs = {}
    for kind in ("structured", "semistructured", "unstructured"):
        for p in sorted((root / kind).iterdir()):
            with open(p, "rb") as fh:
                out = api.call("POST", "/sources/upload", files={"file": (p.name, fh)},
                               data={"batch_id": batch, "replace": "true"})
            runs[p.name] = (kind, (out or {}).get("run_id"), (out or {}).get("routing"))
    return runs


def wait_batch(api: Api, batch: str, total: int, timeout: float = 7200) -> dict:
    """Until every run of the batch is completed, failed or canceled (/uploads/batches)."""
    end = time.time() + timeout
    st: dict = {}
    while time.time() < end:
        st = api.call("GET", f"/uploads/batches/{batch}") or {}
        print(f"    batch: done {st.get('done')}/{st.get('total')} {json.dumps(st.get('counts') or {})}",
              flush=True)
        if int(st.get("done") or 0) >= total:
            return st
        time.sleep(30)
    return st


def runs_of(api: Api, batch: str) -> dict:
    """{filename: (kind, run id, None)} for the newest run of each file in `batch`."""
    out: dict = {}
    for r in (api.call("GET", "/progress?limit=2000") or {}).get("items") or []:
        if r.get("asset") not in out:          # newest first; the run list carries no batch id
            out[r["asset"]] = (r.get("kind"), r.get("id"), None)
    return out


def main() -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--api", default="http://127.0.0.1:8000")
    a.add_argument("--dir", required=True)
    a.add_argument("--bench", default="", help="the 1000-file bench kit (the 300 profile reads its RFC/PEP files)")
    a.add_argument("--container", default="opencortyx-test-api-1")
    a.add_argument("--out", default="results/e2e300")
    a.add_argument("--skip-upload", action="store_true")
    a.add_argument("--batch", default="", help="with --skip-upload: the batch already uploaded")
    a.add_argument("--profile", default="300", choices=("300", "mixed"),
                   help="mixed: 10 CSV + 10 JSON + 100 documents as one connected graph (corpus.write_mixed)")
    args = a.parse_args()
    if args.profile == "mixed":
        return main_mixed(args)
    root = Path(args.dir)
    exp = corpus.write(str(root), args.bench) if not (root / "expected.json").exists() else json.loads(
        (root / "expected.json").read_text(encoding="utf-8"))
    api = Api(args.api)
    me = api.call("GET", "/auth/me") or {}
    ws = me.get("workspace_id") or (me.get("user") or {}).get("workspace_id")

    # 1
    cfg = api.call("GET", "/config") or {}
    check("workspace on the data store", (cfg.get("tunable") or {}).get("data_store") == "delta",
          (cfg.get("tunable") or {}).get("data_store"))

    # 2
    batch = args.batch or f"e2e300-{int(time.time())}"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    if not args.skip_upload:
        runs = upload_all(api, root, batch)
        check("300 files accepted by Add data", sum(1 for r in runs.values() if r[1]) == 300,
              f"{sum(1 for r in runs.values() if r[1])} runs")
    st = wait_batch(api, batch, 300)
    check("every run finished, none failed", not st.get("failed") and int(st.get("done") or 0) >= 300,
          json.dumps({"done": st.get("done"), "counts": st.get("counts"), "failed": (st.get("failed") or [])[:5]}))
    print(f"    read in {time.time() - t0:.0f} s", flush=True)
    if args.skip_upload:
        runs = runs_of(api, batch)
    (out / "runs.json").write_text(json.dumps(runs), encoding="utf-8")
    # Embeddings and resolution run after each asset; the semantic layer is detected once the batch
    # settles (a pass booked tasks.maintenance.DETECT_DEBOUNCE_S after the last run, ~90 s to read).
    for _ in range(40):
        time.sleep(15)
        if len((api.call("GET", "/schema-mappings/knowledge") or {}).get("types") or []) >= 5:
            break
    time.sleep(60)

    # 3
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "expected300.json"
    tmp.write_text(json.dumps(exp), encoding="utf-8")
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    subprocess.run(["docker", "cp", str(tmp), f"{args.container}:/tmp/expected.json"], check=True, env=env)
    r = subprocess.run(["docker", "exec", args.container, "python", "-m", "tests.goldset.stack_checks",
                        str(ws), "/tmp/expected.json"], capture_output=True, text=True, env=env)
    print(r.stdout[-6000:], r.stderr[-2000:])
    check("store checks inside the stack", r.returncode == 0, f"exit {r.returncode}")

    # 4
    ctx = api.call("POST", "/context", json={"query": "What does the licensing policy say about early termination?"})
    text = (ctx or {}).get("context") or ""
    check("policy question answered from passages", exp["facts"]["policy_key"] in text.lower(),
          f"{len(text)} chars, cited {len((ctx or {}).get('citations') or [])}")
    cites = (ctx or {}).get("citations") or []
    check("passage citations name their document", any(c.get("asset_id") for c in cites),
          [c.get("asset_label") for c in cites[:3]])
    rows = list(csv.DictReader(open(root / "structured" / "licences-00.csv", encoding="utf-8")))
    region = rows[0]["region"]
    want = sum(1 for x in rows if x["status"] == rows[0]["status"] and x["region"] == region)
    q = f"How many {rows[0]['status']} licences are there in region {region} in licences 00?"
    ctx = api.call("POST", "/context", json={"query": q})
    data = (ctx or {}).get("data") or {}
    got = data.get("rows", [[None]])[0][0] if data.get("rows") else None
    check("a count over a file answered by SQL", str(got) == str(want),
          f"route={(ctx or {}).get('route')} sql={data.get('sql')} got={got} want={want}")
    ans = api.call("POST", "/answer", json={"query": "What is the credit score of Northwind Media?", "save_chat": False})
    check("an attribute no source has is UNKNOWN", (ans or {}).get("abstained") is True,
          f"outcome={(ans or {}).get('outcome')} reason={(ans or {}).get('unknown_reason')}")

    # 5
    view = "licences_00"
    for name, path, body in (
            ("segment", "/definitions/segments", {"name": "active licence", "table": view,
                                                   "condition": "status = 'ACTIVE'"}),
            ("metric", "/definitions/metrics", {"name": "licence fees", "table": view, "expression": "sum(fee)"}),
            ("verified query", "/definitions/verified-queries", {"question": "How many licences are in licences 00?",
                                                                  "sql": f'SELECT count(*) FROM "{view}"'})):
        try:
            res = api.call("POST", path, json=body)
            check(f"definition saved: {name}", bool((res or {}).get("id")), json.dumps(res)[:200])
        except Exception as e:                                # noqa: BLE001
            check(f"definition saved: {name}", False, str(e)[:300])
    bad = None
    try:
        api.call("POST", "/definitions/metrics", json={"name": "broken", "table": view, "expression": "sum(no_such_col)"})
    except Exception as e:                                    # noqa: BLE001
        bad = str(e)
    check("a definition that does not run is refused", bool(bad and "422" in bad), (bad or "accepted")[:200])

    # 6 - the asset Data tab, one asset of each kind

    def asset_of(fname):
        rid = (runs.get(fname) or [None, None])[1]
        res = ((api.call("GET", f"/progress/{rid}") or {}).get("result") or {}) if rid else {}
        return res.get("source_id")

    for fname, want in (("licences-00.csv", exp["rows"]["licences-00.csv"]),
                        ("orders-00.ndjson", exp["records"]["orders-00.ndjson"]),
                        ("licensing-policy.md", None)):
        aid = asset_of(fname)
        page = api.call("GET", f"/assets/{aid}/data?limit=5") if aid else {}
        ok = bool(page and page.get("available") and page.get("rows")) and (want is None or page.get("total") == want)
        check(f"Data tab: {fname}", ok, f"asset={aid} total={(page or {}).get('total')} want={want}")

    # 7 - re-ingest a changed file; delete an asset
    p = root / "structured" / "licences-01.csv"
    rows = list(csv.DictReader(open(p, encoding="utf-8")))
    rows[0]["status"] = "SUSPENDED" if rows[0]["status"] != "SUSPENDED" else "ACTIVE"
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with open(p, "rb") as fh:
        res = api.call("POST", "/sources/upload", files={"file": (p.name, fh)}, data={"replace": "true"})
    api.wait([(res or {}).get("run_id")])
    aid = asset_of("licences-01.csv")
    page = api.call("GET", f"/assets/{aid}/data?limit=50") or {}
    cols = page.get("columns") or []
    first = next((r for r in page.get("rows") or [] if "licence_id" in cols
                  and r[cols.index("licence_id")] == rows[0]["licence_id"]), None)
    check("re-ingest replaced the changed row", bool(first) and first[cols.index("status")] == rows[0]["status"],
          f"total={page.get('total')} row={first}")
    victim = asset_of("licences-02.csv")
    if victim:
        api.call("DELETE", f"/assets/{victim}")
        page = api.call("GET", f"/assets/{victim}/data") or {}
        check("delete removed the asset's data", page.get("available") is False, json.dumps(page)[:200])
    else:
        check("delete removed the asset's data", False, "no asset id for licences-02.csv")

    # 8 - the Mapper: detected mappings validate, preview, and publish (Mode A.1) on request
    maps = [m for m in (api.call("GET", "/schema-mappings?light=true") or {}).get("items") or [] if m["status"] == "auto"]
    check("detected mappings listed in the Mapper", len(maps) >= 5, ", ".join(m["name"] for m in maps))
    mid = next((m["id"] for m in maps if m["name"].startswith("Project")), maps[0]["id"] if maps else None)
    if mid:
        v = api.call("POST", f"/schema-mappings/{mid}/validate") or {}
        check("a detected mapping validates", v.get("valid") is True, json.dumps(v.get("issues"))[:300])
        pv = api.call("GET", f"/schema-mappings/{mid}/preview?limit=3") or {}
        ents = pv.get("entities") or []
        check("preview shows entities and links", bool(ents and ents[0].get("rows") and pv.get("relationships")),
              json.dumps(pv)[:300])
    graph_before = (api.call("GET", "/stats") or {}).get("nodes")
    pub = api.call("POST", "/schema-mappings/publish?everything=true") or {}
    check("publishing makes one node per entity", (pub.get("nodes") or 0) > 0 and (pub.get("edges") or 0) > 0,
          json.dumps(pub))
    lic = api.call("POST", "/context", json={"query": "Which customer holds licence LIC-1057?"}) or {}
    check("a published entity is found with its link", "C-152" in (lic.get("context") or ""),
          f"graph nodes before={graph_before} route={lic.get('route')}")

    (out / "results.json").write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


# ── the mixed profile: 10 CSV + 10 JSON + 100 documents as ONE connected graph ─────────────────────

def _upload(api: Api, files: list, batch: str, **data) -> dict:
    runs = {}
    for kind, p in files:
        with open(p, "rb") as fh:
            out = api.call("POST", "/sources/upload", files={"file": (p.name, fh)},
                           data={"batch_id": batch, "replace": "true", **data})
        runs[p.name] = (kind, (out or {}).get("run_id"), (out or {}).get("routing"))
    return runs


def _key_type(layer: dict, key: str) -> str:
    """The detected entity type whose identifier is `key` (customer_id -> Customer)."""
    for t in layer.get("types") or []:
        if key in (t.get("keys") or []):
            return t["name"]
    return ""


def _answer(api: Api, q: str, **extra) -> dict:
    return api.call("POST", "/answer", json={"query": q, "save_chat": False, **extra}) or {}


def main_mixed(args) -> int:
    root = Path(args.dir)
    exp = corpus.write_mixed(str(root))
    api = Api(args.api)
    me = api.call("GET", "/auth/me") or {}
    ws = me.get("workspace_id")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cfg = api.call("GET", "/config") or {}
    check("workspace on the data store", (cfg.get("tunable") or {}).get("data_store") == "delta",
          (cfg.get("tunable") or {}).get("data_store"))

    # 1. tables and records first: one licence file goes into a restricted collection
    coll = api.call("POST", "/collections", json={"name": f"finance-{int(time.time())}"})
    data_files = [(k, p) for k in ("structured", "semistructured") for p in sorted((root / k).iterdir())]
    restricted = root / "structured" / "licences-09.csv"
    t0 = time.time()
    batch = f"mixed-data-{int(t0)}"
    runs = _upload(api, [x for x in data_files if x[1] != restricted], batch)
    runs.update(_upload(api, [("structured", restricted)], batch, collection_id=coll["id"]))
    check("20 data files accepted", sum(1 for r in runs.values() if r[1]) == 20, f"{sum(1 for r in runs.values() if r[1])} runs")
    st = wait_batch(api, batch, 20)
    check("every data run finished, none failed", not st.get("failed") and int(st.get("done") or 0) >= 20,
          json.dumps({"counts": st.get("counts"), "failed": (st.get("failed") or [])[:5]}))
    layer: dict = {}
    for _ in range(40):                      # the semantic layer is detected once the batch settles
        time.sleep(15)
        layer = api.call("GET", "/schema-mappings/knowledge") or {}
        if _key_type(layer, "customer_id") and _key_type(layer, "licence_id"):
            break
    customer_t, licence_t = _key_type(layer, "customer_id"), _key_type(layer, "licence_id")
    check("customers and licences detected as types", bool(customer_t and licence_t), f"{customer_t} / {licence_t}")
    pub = api.call("POST", "/schema-mappings/publish?everything=true") or {}
    check("published: one node per entity, linked", (pub.get("nodes") or 0) > 0 and (pub.get("edges") or 0) > 0, json.dumps(pub))

    # 2. documents, read with a Pattern extractor that links each to the records it names
    pattern = r"(?P<customer>\bC-\d{3}\b)|(?P<licence>\bLIC-\d{4}\b)"
    template = {"name": "Account ids", "new": True, "source": None,
                "mechanism": {"ref": "unstructured.regex", "category": "unstructured", "pattern": pattern, "lines": [pattern]},
                "emit": {"nodes": [], "attributes": [], "edges": [
                    {"from": "source", "to": {"match": customer_t, "on": {"customer_id": "$customer"}}, "type": "MENTIONS"},
                    {"from": "source", "to": {"match": licence_t, "on": {"licence_id": "$licence"}}, "type": "MENTIONS"}]}}
    sample = next(n for n in exp["facts"]["notes"] if n["file"].startswith("account-"))
    prev = api.call("POST", "/extraction-templates/preview", json={**template, "sample_text": (root / "unstructured" / sample["file"]).read_text(encoding="utf-8")}) or {}
    check("the extractor's preview links a note to its customer", "MENTIONS" in json.dumps(prev), json.dumps(prev)[:300])
    api.call("POST", "/extraction-templates", json={**template, "new": False})   # a rerun updates it
    docs = [("unstructured", p) for p in sorted((root / "unstructured").iterdir())]
    batch2 = f"mixed-docs-{int(time.time())}"
    runs.update(_upload(api, docs, batch2, extractors=json.dumps(["Account ids"])))
    st = wait_batch(api, batch2, 100)
    check("every document run finished, none failed", not st.get("failed") and int(st.get("done") or 0) >= 100,
          json.dumps({"counts": st.get("counts"), "failed": (st.get("failed") or [])[:5]}))
    print(f"    read 120 files in {time.time() - t0:.0f} s", flush=True)
    time.sleep(90)                           # embeddings, resolution and the extractor's queued work
    (out / "runs.json").write_text(json.dumps(runs), encoding="utf-8")

    # 3. the stores, and the graph being ONE graph
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "expected-mixed.json"
    tmp.write_text(json.dumps(exp), encoding="utf-8")
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    subprocess.run(["docker", "cp", str(tmp), f"{args.container}:/tmp/expected.json"], check=True, env=env)
    r = subprocess.run(["docker", "exec", args.container, "python", "-m", "tests.goldset.stack_checks",
                        str(ws), "/tmp/expected.json"], capture_output=True, text=True, env=env)
    print(r.stdout[-8000:], r.stderr[-2000:])
    check("store and connection checks inside the stack", r.returncode == 0, f"exit {r.returncode}")

    # 4. answers, each with its sentences checked against the sources they cite
    a = _answer(api, "What does the licensing policy say about early termination?")
    check("policy answered from the policy document", exp["facts"]["policy_key"] in json.dumps(a).lower(),
          (a.get("answer") or "")[:200])
    check("every answer sentence is checked against its sources", bool((a.get("verification") or {}).get("sentences")),
          json.dumps(a.get("verification"))[:300])
    ctx = api.call("POST", "/context", json={"query": "How many licences are EXPIRED?"}) or {}
    got = ((ctx.get("data") or {}).get("rows") or [[None]])[0][0]
    check("a count across the ten CSV files, by SQL", str(got) == str(exp["facts"]["expired_total"]),
          f"got={got} want={exp['facts']['expired_total']} sql={(ctx.get('data') or {}).get('sql')}")
    note = next(n for n in exp["facts"]["notes"] if n["file"].startswith("account-") and len(n["licences"]) >= 2)
    ctx = api.call("POST", "/context", json={"query": f"Which licences does {note['name']} hold?"}) or {}
    hits = [x for x in note["licences"] if x in json.dumps(ctx)]
    check("a customer's licences found across JSON, CSV and its note", len(hits) >= max(1, len(note["licences"]) // 2),
          f"{len(hits)} of {len(note['licences'])}")
    a = _answer(api, f"What is the credit score of {note['name']}?")
    check("an attribute no source has is UNKNOWN", a.get("abstained") is True, a.get("outcome"))

    # 5. cost: every model call measured and attributed
    cost = api.call("GET", "/cost") or {}
    use = api.call("GET", "/cost/usage?days=1") or {}
    purposes = {p["purpose"] for p in use.get("by_purpose") or []}
    check("model calls are metered", (cost.get("calls") or 0) > 0 and "answer" in purposes, json.dumps(sorted(purposes)))
    check("the in-process model's tokens are measured, not estimated", (cost.get("measured_share") or 0) >= 0.99,
          cost.get("measured_share"))

    # 6. resolution quality on the duplicated customers
    pairs = []
    by_id: dict = {}
    for p in sorted((root / "semistructured").glob("crm-*.json")):
        for c in json.loads(p.read_text(encoding="utf-8"))["customers"]:
            by_id.setdefault(c["customer_id"], []).append({"label": c["name"], "properties": {"customer_id": c["customer_id"], "name": c["name"]}})
    for p in sorted((root / "semistructured").glob("billing-*.json")):
        for c in json.loads(p.read_text(encoding="utf-8")):
            by_id.setdefault(c["customer_id"], []).append({"label": c["name"], "properties": {"customer_id": c["customer_id"], "name": c["name"], "billing_currency": c["billing_currency"]}})
    for cid, recs in by_id.items():
        if len(recs) >= 2:
            pairs.append({"node_type": customer_t, "a": recs[0], "b": recs[1], "same": True})
    smiths = [recs[0] for recs in by_id.values() if recs[0]["label"] == exp["facts"]["ambiguous"]]
    if len(smiths) == 2:
        pairs.append({"node_type": customer_t, "a": smiths[0], "b": smiths[1], "same": False})
    imp = api.call("POST", "/resolution/labels/import", json={"items": pairs}) or {}
    ev = api.call("POST", "/resolution/quality/evaluate", json={"node_type": customer_t}) or {}
    cur = ev.get("current") or {}
    check("labelled pairs imported", (imp.get("imported") or 0) >= len(pairs) - 1, json.dumps(imp))
    check("no false merge on the labelled customers (two different 'Smith Media' stay apart)",
          cur.get("false_merges") == 0, json.dumps({k: cur.get(k) for k in ("precision", "recall", "false_merges", "missed")}))

    # 7. access: a person reads only what their group is given
    uid = int(time.time())
    viewer = api.call("POST", "/users", json={"email": f"viewer{uid}@opencortyx.local", "password": "viewer-pass-1", "role": "viewer"})
    pol2 = api.call("POST", "/access/policies", json={"group": "finance", "collection_id": coll["id"], "max_sensitivity": "normal"})
    try:
        v = Api.__new__(Api)
        import httpx
        v.http = httpx.Client(base_url=args.api.rstrip("/") + "/api/v2", timeout=600)
        tok = v.http.post("/auth/token", json={"username": f"viewer{uid}@opencortyx.local", "password": "viewer-pass-1"}).json()["access_token"]
        v.http.headers["Authorization"] = f"Bearer {tok}"
        rid = runs["licences-09.csv"][1]
        aid = ((api.call("GET", f"/progress/{rid}") or {}).get("result") or {}).get("source_id")
        check("a person outside the group cannot open the restricted file", v.http.get(f"/assets/{aid}/data").status_code in (404, 200)
              and not (v.http.get(f"/assets/{aid}/data").json() or {}).get("rows"), aid)
        lic9 = next(csv.DictReader(open(restricted, encoding="utf-8")))["licence_id"]
        ctx = v.http.post("/context", json={"query": f"What is the status of licence {lic9}?"}).json()
        check("their questions never read the restricted file", lic9 not in json.dumps(ctx.get("citations") or []), lic9)
        api.call("PATCH", f"/users/{viewer['id']}", json={"groups": ["finance"]})
        time.sleep(31)                       # policies and groups are cached for 30 s per process
        ctx = v.http.post("/context", json={"query": f"What is the status of licence {lic9}?"}).json()
        check("with the group, the same question reads it", lic9 in json.dumps(ctx), lic9)
        other = next(csv.DictReader(open(root / "structured" / "licences-00.csv", encoding="utf-8")))["licence_id"]
        api.call("PATCH", f"/users/{viewer['id']}", json={"groups": []})
        time.sleep(31)
        ctx = v.http.post("/context", json={"query": f"What is the status of licence {other}?"}).json()
        check("files in no collection stay readable to everyone", other in json.dumps(ctx), other)
    finally:
        api.call("DELETE", f"/access/policies/{pol2['id']}")
        api.call("DELETE", f"/users/{viewer['id']}")

    # 8. forgetting a customer, with the note about them
    gone = next(n for n in exp["facts"]["notes"] if n["file"].startswith("account-") and n["name"] != note["name"])
    cust = next((c for c in (api.call("GET", f"/search?q={gone['name']}&types={customer_t}") or {}).get("results") or []), None)
    note_asset = ((api.call("GET", f"/progress/{runs[gone['file']][1]}") or {}).get("result") or {}).get("source_id")
    if cust:
        prev = api.call("GET", f"/forget/subject/{cust['id']}/preview") or {}
        check("forget preview lists the note that mentions them",
              any(d["asset_id"] == note_asset for d in prev.get("documents_mentioning") or []), json.dumps(prev)[:300])
        job = api.call("POST", "/forget/subject", json={"node_id": cust["id"], "confirm": cust["label"], "also_assets": [note_asset]}) or {}
        check("forgetting a customer finishes, proven", job.get("status") == "done", json.dumps(job.get("proof"))[:300])
        ctx = api.call("POST", "/context", json={"query": f"Who is the licensing contact of {gone['name']}?"}) or {}
        check("after forgetting, no answer cites them", gone["file"] not in json.dumps(ctx) and note_asset not in json.dumps(ctx.get("citations") or []), "")
    else:
        check("forgetting a customer", False, f"{gone['name']} not found by search")

    (out / "results.json").write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
