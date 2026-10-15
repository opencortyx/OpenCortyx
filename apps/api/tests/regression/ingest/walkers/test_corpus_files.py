"""Every real corpus file through the default walker (CORP)."""
from __future__ import annotations

import pathlib
import uuid

import pytest

from tests.regression.framework.matrix import CORPUS, read_capped, scenarios
from tests.regression.framework.support import graph_problems, ingest_default, MemRepo

pytestmark = pytest.mark.regression


def _load(scenario, rec) -> bytes:
    if not CORPUS:
        rec.block("OPENCORTYX_CORPUS is not mounted")
    p = pathlib.Path(CORPUS) / scenario.params["path"]
    content = read_capped(p)
    rec.generated(file=scenario.params["path"], size_bytes=len(content))
    return content


def _parse(content: bytes, name: str):
    from opencortyx.ingest.formats import semistructured as P
    return P.parse(content, name)


# ── CORP ─────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("scenario", scenarios("CORP") or [pytest.param(None, marks=pytest.mark.skip("no corpus"))], ids=str)
async def test_corpus_file(scenario, rec, monkeypatch):
    from opencortyx.ingest.formats import semistructured as P
    content = _load(scenario, rec)
    name = scenario.params["path"]
    rec.step("SemiStructuredWalker().ingest_file(real file)")
    try:
        repo, res = await ingest_default(monkeypatch, content, name)
    except P.ParseError as e:
        rec.result({"outcome": "rejected", "reason": str(e)[:300]})
        rec.verify("valid file not rejected", scenario.expected != "accept" or "holds no records" in str(e), str(e))
        return
    rec.result({"outcome": "ingested", "nodes": res["nodes"], "edges": res["edges"]})
    rec.verify("invalid file not accepted", scenario.expected != "reject", "accepted")
    probs = graph_problems(repo, uuid.UUID(res["source_id"]))
    rec.verify("provenance and parents", not probs, probs)
    again = MemRepo()
    await ingest_default(monkeypatch, content, name, repo=again)
    rec.verify("deterministic", set(again.nodes) == set(repo.nodes) and again.edges == repo.edges)
    before = (len(repo.nodes), len(repo.edges))
    await ingest_default(monkeypatch, content, name, repo=repo)
    rec.verify("idempotent", (len(repo.nodes), len(repo.edges)) == before)
