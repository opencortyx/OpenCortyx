"""Run the gold set against a stack: write the sources, load them the way a user would, ask, score.

    python -m tests.goldset.run write  --dir DIR
    python -m tests.goldset.run load   --api URL --dir DIR --container-dir /host/goldset
    python -m tests.goldset.run ask    --api URL --out results/goldset-<name>
    python -m tests.goldset.run all    --api URL --dir DIR --container-dir /host/goldset --out ...

Credentials come from OPENCORTYX_EMAIL / OPENCORTYX_PASSWORD in the environment, never the command
line. `load` goes through the normal ingestion paths only - Add data uploads for the files, saved
connectors and ingestions for the live database and the lake table - so the gold set tests what a
user's data goes through, not a side door. `--container-dir` is where the API sees DIR (the files
mount), for the sources it reads in place.

Scoring (docs/knowledge-layer-implementation.md, section 6):

    resolved = an ANSWERED question answered correctly WITH a citation, or an UNKNOWN / CLARIFY
               question met with that outcome
    unsupported = an UNKNOWN question answered anyway (a confident answer with no support is a
               failure, and the number the release gate watches beside the resolution rate)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path

from tests.goldset import domain, questions

_UNKNOWN_TEXT = re.compile(r"\b(unknown|no (connected )?source|not (found|available|stated|in the)|"
                           r"(could|can)( ?no|')t find|no (information|record|data)|unavailable|"
                           r"does not (say|contain|exist)|isn't|is not (mentioned|available))\b", re.I)
_CLARIFY_TEXT = re.compile(r"\b(which (one|of)|did you mean|two customers|more than one|ambiguous)\b", re.I)


class Api:
    def __init__(self, base: str):
        import httpx
        self.http = httpx.Client(base_url=base.rstrip("/") + "/api/v2", timeout=600)
        self.login()

    def login(self) -> None:
        email, pw = os.environ.get("OPENCORTYX_EMAIL"), os.environ.get("OPENCORTYX_PASSWORD")
        if not email or not pw:
            raise SystemExit("set OPENCORTYX_EMAIL and OPENCORTYX_PASSWORD")
        r = self.http.post("/auth/token", json={"username": email, "password": pw})
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def call(self, method: str, path: str, **kw):
        import httpx
        for attempt in range(6):
            try:
                r = self.http.request(method, path, **kw)
            except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as e:
                # A kept-alive connection the server already closed, or the API restarting: try again.
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
                if isinstance(e, httpx.ConnectError):
                    time.sleep(5)
                continue
            if r.status_code == 401 and attempt == 0:
                self.login()
                continue
            if r.status_code in (429, 502, 503, 504):
                time.sleep(float(r.headers.get("Retry-After") or 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json() if r.content else None
        r.raise_for_status()

    def wait(self, run_ids: list, timeout: float = 1800) -> None:
        end = time.time() + timeout
        pending = set(i for i in run_ids if i)
        while pending and time.time() < end:
            for rid in list(pending):
                st = (self.call("GET", f"/progress/{rid}") or {}).get("status")
                if st in ("completed", "duplicate", "failed", "canceled"):
                    pending.discard(rid)
            time.sleep(3)


def cmd_write(args) -> dict:
    d = domain.build()
    paths = domain.write(d, args.dir)
    qs = questions.build(d)
    Path(args.dir, "questions.json").write_text(json.dumps(qs, indent=1), encoding="utf-8")
    print(f"wrote {len(qs)} questions and sources to {args.dir}")
    return paths


def cmd_load(args) -> None:
    api = Api(args.api)
    root = Path(args.dir)
    runs = []
    files = [root / "licences.csv", root / "customers.json", *sorted((root / "docs").glob("*.md"))]
    for f in files:
        with open(f, "rb") as fh:
            out = api.call("POST", "/sources/upload", files={"file": (f.name, fh)},
                           data={"batch_id": "goldset", "replace": "true"})
        runs.append((out or {}).get("run_id"))
    cdir = args.container_dir.rstrip("/")
    conns = {}
    for name, body in (
            ("orders", {"name": "goldset orders", "kind": "sqlite", "database": f"{cdir}/orders.sqlite"}),
            ("archive", {"name": "goldset archive", "kind": "sqlite", "database": f"{cdir}/archive.sqlite"}),
            ("lake", {"name": "goldset lake", "kind": "delta", "directory": f"{cdir}/lake"})):
        conns[name] = api.call("POST", "/connectors", json=body)["id"]
    for name, kind, extra in (("orders", "structured", {"selections": [{"schema": None, "tables": ["orders"]}]}),
                              ("archive", "structured", {"selections": [{"schema": None, "tables": ["archived_orders"]}]}),
                              ("lake", "structured", {"selections": [{"schema": None, "tables": ["sales"]}]})):
        ing = api.call("POST", "/ingestions", json={"name": f"goldset {name}", "connector_id": conns[name],
                                                    "kind": kind, **extra})
        out = api.call("POST", f"/ingestions/{ing['id']}/run")
        runs.extend((out or {}).get("run_ids") or [(out or {}).get("run_id")])
    api.wait(runs)
    # The archive goes away: its questions must come back "source unavailable", not an old answer.
    archive = root / "archive.sqlite"
    if archive.exists():
        shutil.move(str(archive), str(root / "archive.sqlite.gone"))
    print(f"loaded {len(files)} files and 3 live sources ({len([r for r in runs if r])} runs)")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower())


def score(q: dict, res: dict) -> dict:
    text = res.get("answer") or ""
    t = _norm(text)
    outcome = (res.get("outcome") or "").upper()
    cited = bool(res.get("citations"))
    if not outcome:
        outcome = ("UNKNOWN" if res.get("abstained") or _UNKNOWN_TEXT.search(text)
                   else "CLARIFY" if _CLARIFY_TEXT.search(text) else "ANSWERED")
    found = all(any(_norm(m) in t for m in group) for group in q["expect"]) if q["expect"] else False
    if q["outcome"] == "ANSWERED":
        resolved = found and cited
    elif q["outcome"] == "CLARIFY":
        resolved = outcome == "CLARIFY" or all(c.lower() in t for c in q.get("candidates", []))
    else:
        resolved = outcome == "UNKNOWN"
    return {"resolved": resolved, "found": found, "cited": cited, "outcome": outcome,
            "unsupported": q["outcome"] == "UNKNOWN" and outcome == "ANSWERED"}


def stratified(qs: list, n: int) -> list:
    """`n` questions with every type in proportion and at least two of each, in a fixed order."""
    import random
    by: dict = defaultdict(list)
    for q in qs:
        by[q["type"]].append(q)
    rnd = random.Random(7)
    picked = []
    for t, items in sorted(by.items()):
        k = max(2, round(n * len(items) / len(qs)))
        picked += rnd.sample(items, min(k, len(items)))
    return sorted(picked, key=lambda q: q["id"])[: max(n, len(by) * 2)]


def cmd_ask(args) -> dict:
    api = Api(args.api)
    qs = json.loads(Path(args.dir, "questions.json").read_text(encoding="utf-8"))
    if args.limit:
        qs = qs[:args.limit]
    if args.sample:
        qs = stratified(qs, args.sample)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, lat = [], []
    with open(out / "results.jsonl", "w", encoding="utf-8") as fh:
        for q in qs:
            retrieved = None
            if q["outcome"] == "ANSWERED" and q["expect"]:
                # Retrieval alone: is the evidence in what /context returns (the bench's measure)?
                try:
                    c = api.call("POST", "/context", json={"query": q["question"]}) or {}
                    ctext = _norm((c.get("context") or "") + " " + json.dumps(c.get("data") or {}))
                    retrieved = all(any(_norm(m) in ctext for m in g) for g in q["expect"])
                except Exception:                            # noqa: BLE001
                    retrieved = False
            if args.retrieval_only:
                if retrieved is None:
                    continue                  # nothing to find for an UNKNOWN / CLARIFY question
                row = {**q, "retrieved": retrieved}
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
                continue
            t0 = time.perf_counter()
            try:
                res = api.call("POST", "/answer", json={"query": q["question"], "save_chat": False})
                err = None
            except Exception as e:                               # noqa: BLE001 - a failure is a result
                res, err = {}, str(e)[:300]
            ms = round((time.perf_counter() - t0) * 1000)
            lat.append(ms)
            s = score(q, res or {})
            row = {**q, **s, "ms": ms, "error": err, "retrieved": retrieved, "answer": (res or {}).get("answer"),
                   "route": (res or {}).get("route"), "unknown_reason": (res or {}).get("unknown_reason"),
                   "citations": [c.get("source") or c.get("node_id") for c in (res or {}).get("citations") or []][:8]}
            rows.append(row)
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
    by_type = defaultdict(list)
    if args.retrieval_only:
        for r in rows:
            by_type[r["type"]].append(r)
        summary = {"questions": len(rows), "retrieval_rate": round(100 * sum(r["retrieved"] for r in rows) / max(1, len(rows)), 1),
                   "retrieval_by_type": {t: round(100 * sum(r["retrieved"] for r in rs) / len(rs), 1)
                                         for t, rs in sorted(by_type.items())}}
        (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
        print(json.dumps(summary, indent=1))
        return summary
    for r in rows:
        by_type[r["type"]].append(r)
    summary = {
        "questions": len(rows),
        "resolution_rate": round(100 * sum(r["resolved"] for r in rows) / max(1, len(rows)), 1),
        # of the questions whose right answer is "unknown", the share answered anyway
        "unsupported_rate": round(100 * sum(r["unsupported"] for r in rows) /
                                  max(1, sum(1 for q in qs if q["outcome"] == "UNKNOWN")), 1),
        "errors": sum(1 for r in rows if r["error"]),
        "p50_ms": statistics.median(lat) if lat else 0,
        "p95_ms": sorted(lat)[int(0.95 * (len(lat) - 1))] if lat else 0,
        "by_type": {t: round(100 * sum(r["resolved"] for r in rs) / len(rs), 1) for t, rs in sorted(by_type.items())},
        # retrieval accuracy: questions with an answer whose evidence /context returned
        "retrieval_rate": round(100 * sum(1 for r in rows if r.get("retrieved")) /
                                max(1, sum(1 for r in rows if r.get("retrieved") is not None)), 1),
        "retrieval_by_type": {t: round(100 * sum(1 for r in rs if r.get("retrieved")) / len(rs), 1)
                              for t, rs in sorted(by_type.items()) if any(r.get("retrieved") is not None for r in rs)},
        "outcomes": dict(Counter(r["outcome"] for r in rows)),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=("write", "load", "ask", "all"))
    p.add_argument("--api", default="http://127.0.0.1:8000")
    p.add_argument("--dir", default="goldset-data")
    p.add_argument("--container-dir", default="/host/goldset")
    p.add_argument("--out", default="results/goldset")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--sample", type=int, default=0, help="a stratified sample of this many questions")
    p.add_argument("--retrieval-only", action="store_true", help="score /context only, no /answer")
    a = p.parse_args()
    if a.command in ("write", "all"):
        cmd_write(a)
    if a.command in ("load", "all"):
        cmd_load(a)
    if a.command in ("ask", "all"):
        cmd_ask(a)


if __name__ == "__main__":
    main()
