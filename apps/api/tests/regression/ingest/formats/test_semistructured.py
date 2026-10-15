"""Semi-structured formats × data variants through the default walker (FMTS)."""
from __future__ import annotations

import uuid

import pytest

from tests.regression.framework import data as D
from tests.regression.framework.matrix import scenarios
from tests.regression.framework.support import graph_problems, ingest_default, MemRepo, oracle_plain_walk, walked_data

pytestmark = pytest.mark.regression


# ── FMTS: semi-structured records ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("FMTS"), ids=str)
async def test_semistructured_format(scenario, rec, monkeypatch):
    from opencortyx.ingest.formats import semistructured as P
    p = scenario.params
    fmt = p["fmt"]
    if "raw" in p:
        content, value = D.SEMI_RAW[p["raw"]], None
    else:
        value = D.SEMI_VARIANTS[p["variant"]][0]
        content = D.semi_bytes(fmt, value)
        if p.get("truncate"):
            content = D.truncated(content)
    name = f"regression.{fmt}"
    rec.generated(format=fmt, size_bytes=len(content), head=content[:160].decode("utf-8", "replace"))
    rec.step(f"SemiStructuredWalker().ingest_file(org, <{len(content)} bytes>, {name!r})")
    try:
        repo, res = await ingest_default(monkeypatch, content, name)
    except P.ParseError as e:
        rec.result({"outcome": "rejected", "reason": str(e)})
        rec.verify("outcome matches expectation", scenario.expected == "reject", f"rejected: {e}")
        rec.verify("rejection names a reason", bool(str(e).strip()), str(e))
        return
    src = uuid.UUID(res["source_id"])
    rec.result({"outcome": "ingested", "nodes": res["nodes"], "edges": res["edges"], "records": res["records"]})
    rec.verify("outcome matches expectation", scenario.expected in ("accept", "accept_partial"),
               f"ingested {res['nodes']} nodes; expected {scenario.expected}")
    rec.verify("status ok", res["status"] == "ok", res["status"])
    rec.verify("graph written", len(repo.nodes) > 1, f"{len(repo.nodes)} nodes")
    probs = graph_problems(repo, src)
    rec.verify("provenance and parents", not probs, probs)

    parsed = P.parse(content, name)
    exp_nodes, exp_edges = oracle_plain_walk(walked_data(parsed))
    rec.verify("node count equals the walker rules", res["nodes"] == exp_nodes, f"{res['nodes']} vs oracle {exp_nodes}")
    rec.verify("edge count equals the walker rules", res["edges"] == exp_edges, f"{res['edges']} vs oracle {exp_edges}")

    if value is not None and scenario.expected == "accept":
        text = repo.text_of_all()
        missing = [v for v in {s.strip() for s in D.scalar_leaves(value, max_depth=5)} if v not in text]
        rec.verify("every value within depth survives", not missing, missing[:5])

    again = MemRepo()
    await ingest_default(monkeypatch, content, name, repo=again)
    rec.verify("deterministic ids", set(again.nodes) == set(repo.nodes), f"{len(again.nodes)} vs {len(repo.nodes)}")
    before = (len(repo.nodes), len(repo.edges))
    await ingest_default(monkeypatch, content, name, repo=repo)
    rec.verify("re-ingest is idempotent", (len(repo.nodes), len(repo.edges)) == before,
               f"{before} -> {(len(repo.nodes), len(repo.edges))}")
