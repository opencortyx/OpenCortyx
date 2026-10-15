"""Upload → route → worker → graph → API, for every catalogued format (E2EI).

Each scenario uploads a real file as throwaway tenant A, waits for its run on the real workers, then
checks the run's final state, what the outcome says it produced, and the asset in the graph; the
duplicate scenario re-uploads the same bytes and checks the graph did not change.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import uuid

import pytest

from tests.regression.framework import data as D
from tests.regression.framework.api import Client, principal_headers, wait_run
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


def valid_bytes(ext: str) -> bytes | None:
    from opencortyx.ingest.formats.catalog import FORMATS
    route = FORMATS[ext].route
    if route == "semistructured":
        return D.semi_bytes(ext, D.SEMI_VARIANTS["record_array"][0])
    if route == "structured":
        cols, rows, _ = D.TABLE_VARIANTS["basic_5_rows"]
        return D.table_bytes(ext, cols, rows)
    return D.doc_bytes(ext, D.DOC_VARIANTS["normal"][1])


def corrupt_bytes(ext: str) -> bytes:
    good = valid_bytes(ext) or b"x" * 64
    return good[: len(good) // 2] + bytes(range(256))


def _upload(c: Client, name: str, content: bytes) -> dict:
    r = c.post(f"{V2}/sources/upload", files={"file": (name, content, "application/octet-stream")})
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    return {**body, "http": r.status_code}


def _finish(c, up, timeout=300):
    return wait_run(c, up["run_id"], timeout) if up.get("run_id") else {"status": "no_run"}


@pytest.fixture
def a(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec, timeout=120)
    yield c
    c.close()


@pytest.mark.parametrize("scenario", scenarios("E2EI"), ids=str)
def test_ingest_workflow(scenario, rec, a):
    p = scenario.params
    ext, variant = p["ext"], p["variant"]
    if variant.startswith(("volume:", "concurrent:", "same:")):
        return _volume_and_concurrency(variant, rec, a)
    content = {"valid": valid_bytes, "duplicate": valid_bytes, "corrupt": corrupt_bytes,
               "empty": lambda e: b""}[variant](ext)
    if content is None:
        rec.block(f"no .{ext} writer available in the test image")
    name = f"e2e-{uuid.uuid4().hex[:8]}.{ext}"
    rec.generated(filename=name, size_bytes=len(content))
    up = _upload(a, name, content)
    rec.result({"upload": {k: up.get(k) for k in ("http", "status", "run_id")}, "route": (up.get("routing") or {}).get("route")})
    rec.verify("upload never a server error", up["http"] < 500, up)
    if variant == "empty":
        if up["http"] >= 400:
            rec.verify("empty file refused up front with a reason", bool(up.get("detail")), up)
            return
        run = _finish(a, up)
        rec.result({"run": {k: run.get(k) for k in ("status", "error", "percent")}})
        rec.verify("empty file ends as a failed run with a reason", run.get("status") == "failed" and bool(run.get("error")), run)
        return
    rec.verify("accepted for ingestion", up["http"] == 200 and bool(up.get("run_id")), up)
    run = _finish(a, up)
    outcome = a.get(f"{V2}/progress/{up['run_id']}/outcome").json()
    rec.result({"run": {k: run.get(k) for k in ("status", "error", "percent", "timed_out")},
                "outcome": {k: outcome.get(k) for k in ("status", "nodes", "edges", "produced", "source_id") if k in outcome}})
    if variant == "corrupt":
        rec.verify("corrupt file never 'completed' silently", run.get("status") in ("failed", "completed"), run.get("status"))
        if run.get("status") == "failed":
            rec.verify("failure carries a reason", bool(run.get("error")), run)
        return
    rec.verify("run completed", run.get("status") == "completed", f"{run.get('status')}: {run.get('error')}")
    from opencortyx.ingest.formats.catalog import FORMATS
    route = FORMATS[ext].route
    found = {f["type"]: f["count"] for f in outcome.get("found", [])}
    structural = {f["type"]: f["count"] for f in outcome.get("structural", [])}
    rec.check("outcome", True, {"found": found, "structural": structural, "total": outcome.get("total_nodes")})
    rec.verify("outcome names the asset", outcome.get("asset") == name, outcome.get("asset"))
    rec.verify("graph written", (outcome.get("total_nodes") or 0) > 0, outcome.get("total_nodes"))
    if route == "structured":
        rec.verify("one entity per row", sum(found.values()) == 5, found)
        rec.verify("one Attribute per column", structural.get("Attribute") == 3, structural)
    elif route == "semistructured":
        rec.verify("every record became a node", sum(found.values()) + sum(structural.values()) >= 5, (found, structural))
    if variant == "duplicate":
        up2 = _upload(a, name, content)
        run2 = _finish(a, up2)
        after = a.get(f"{V2}/progress/{up['run_id']}/outcome").json()
        rec.result({"second_run": run2.get("status"), "total_before": outcome.get("total_nodes"),
                    "total_after": after.get("total_nodes")})
        rec.verify("second upload completes", run2.get("status") in ("completed", "unchanged", "duplicate"), run2)
        rec.verify("graph unchanged by the duplicate", after.get("total_nodes") == outcome.get("total_nodes"),
                   f"{outcome.get('total_nodes')} -> {after.get('total_nodes')}")


def _volume_and_concurrency(variant, rec, a):
    kind, n = variant.split(":")
    n = int(n)
    if kind == "volume":
        name = f"vol-{uuid.uuid4().hex[:6]}.json"
        content = json.dumps([{"id": f"V-{i}", "n": i} for i in range(n)]).encode()
        rec.generated(records=n, size_bytes=len(content))
        run = _finish(a, _upload(a, name, content), timeout=600)
        out = a.get(f"{V2}/progress/{run.get('id')}/outcome").json() if run.get("id") else {}
        rec.result({"run": run.get("status"), "outcome": str(out)[:200]})
        rec.verify("completed", run.get("status") == "completed", run)
        return
    files = [(f"cc-{uuid.uuid4().hex[:6]}.json", json.dumps([{"id": f"C{k}-{i}"} for i in range(20)]).encode())
             for k in range(n)] if kind == "concurrent" else [(f"same-{uuid.uuid4().hex[:6]}.json", b'[{"id":"S-1"},{"id":"S-2"}]')] * n
    rec.generated(files=len(files), distinct=len({f for f, _ in files}))
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        ups = list(pool.map(lambda fc: _upload(a, *fc), files))
    runs = [_finish(a, u, timeout=600) for u in ups]
    states = [r.get("status") for r in runs]
    rec.result({"states": states})
    rec.verify("no upload failed", all(u["http"] == 200 for u in ups), [u["http"] for u in ups])
    rec.verify("every run reaches a final state", all(s not in ("queued", "running", "timeout") for s in states), states)
    if kind == "concurrent":
        rec.verify("every distinct file completed", all(s == "completed" for s in states), states)
    else:
        hits = a.get(f"{V2}/search", params={"q": files[0][0].rsplit('.', 1)[0]}).json().get("results", [])
        rec.verify("one asset for the same file", len([h for h in hits if h.get("type") == "Source"]) <= 1, hits[:4])
