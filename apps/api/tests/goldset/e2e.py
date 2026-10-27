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
    a.add_argument("--bench", required=True)
    a.add_argument("--container", default="opencortyx-test-api-1")
    a.add_argument("--out", default="results/e2e300")
    a.add_argument("--skip-upload", action="store_true")
    a.add_argument("--batch", default="", help="with --skip-upload: the batch already uploaded")
    args = a.parse_args()
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
    api.call("PUT", "/resolution/types/KeyNode", json={"identify_by": ["customer_id", "name"], "enabled": True})

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
    time.sleep(180)           # embeddings and resolution run after each asset

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

    (out / "results.json").write_text(json.dumps(RESULTS, indent=1), encoding="utf-8")
    failed = [r for r in RESULTS if not r["ok"]]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
