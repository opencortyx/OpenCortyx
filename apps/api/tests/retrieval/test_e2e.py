"""End-to-end: the full L0→L4 workflow against the real graph.

Two tiers here too. The first group runs with stubbed surfaces and proves the ORCHESTRATION
properties — degradation on failure, per-layer tracing, budget adherence. The second runs against
the live database and proves the pipeline actually retrieves.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.retrieval.orchestrator import (
    RetrievalWorkflow, WorkflowConfig, format_trace,
)
from opencortyx.core.inference.embedding import embed, embed_batch

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


# ── orchestration properties, no database ────────────────────────────────────────────────────

async def _embed(text):
    return [1.0, 0.0, 0.0] if text else []


async def _embed_batch(texts):
    return [[1.0, 0.0, 0.0] for _ in texts]


def _wf(**kw):
    return RetrievalWorkflow(_embed, _embed_batch, WorkflowConfig(**kw))


class _BoomSurface:
    async def retrieve(self, *a, **k):
        raise RuntimeError("surface exploded")


@pytest.mark.anyio
async def test_a_failing_surface_degrades_it_does_not_empty_the_result():
    """The previous engine had filters that came back short silently; the symptom was
    indistinguishable from 'there is no such data'."""
    wf = _wf(enable_graph=False)

    good = Candidate(node_id=uuid.uuid4(), node_type="Turn", text="a relevant statement")
    good.add(Evidence("node_embedding", 0, 0.8, "cosine"))

    class _Good:
        async def retrieve(self, *a, **k):
            return [good]

    wf.embedding = _Good()
    wf.bm25 = _BoomSurface()
    wf.identifier = _BoomSurface()

    res = await wf.run(ORG, "a relevant statement")
    assert res.blocks, "one failing surface emptied the whole result"
    stages = [t.stage for t in res.trace]
    assert any("FAILED" in (t.detail or "") for t in res.trace)
    assert "fuse" in stages


@pytest.mark.anyio
async def test_every_layer_reports():
    wf = _wf(enable_graph=False)

    class _Empty:
        async def retrieve(self, *a, **k):
            return []

    wf.embedding = wf.bm25 = wf.identifier = wf.fuzzy = wf.metadata = _Empty()
    res = await wf.run(ORG, "what is joanna allergic to?")
    layers = {t.layer for t in res.trace}
    assert {"L0", "L1", "L3", "L4"} <= layers
    assert format_trace(res)


@pytest.mark.anyio
async def test_abstains_when_nothing_resembles_the_question():
    wf = _wf(enable_graph=False)

    class _Empty:
        async def retrieve(self, *a, **k):
            return []

    wf.embedding = wf.bm25 = wf.identifier = wf.fuzzy = wf.metadata = _Empty()
    res = await wf.run(ORG, "a question about nothing in this corpus")
    assert res.abstained is True
    assert res.blocks == []


@pytest.mark.anyio
async def test_budget_is_respected_end_to_end():
    wf = _wf(enable_graph=False, budget_tokens=120)
    cands = []
    for i in range(60):
        c = Candidate(node_id=uuid.uuid4(), node_type="Turn", text=f"statement number {i} " * 12)
        c.add(Evidence("node_embedding", i, 0.9 - i * 0.001, "cosine"))
        cands.append(c)

    class _Many:
        async def retrieve(self, *a, **k):
            return cands

    wf.embedding = _Many()

    class _Empty:
        async def retrieve(self, *a, **k):
            return []

    wf.bm25 = wf.identifier = wf.fuzzy = wf.metadata = _Empty()
    res = await wf.run(ORG, "statement")
    assert res.tokens <= 120
    assert res.blocks


# ── live graph ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.anyio
async def test_full_pipeline_against_the_real_graph():

    wf = RetrievalWorkflow(embed, embed_batch,
                           WorkflowConfig(candidates_per_surface=60, budget_tokens=6000))
    res = await wf.run(ORG, "What is Joanna allergic to?")

    print("\n" + format_trace(res))
    assert res.plan is not None
    assert not res.abstained, "abstained on a question the corpus answers"
    assert res.blocks, "no context produced"
    assert res.tokens <= 6000

    # Provenance must survive to the output — this is what makes an answer explainable.
    assert any(b.retrieved_by for b in res.blocks)
    # Layers must be independently measurable.
    assert {"L0", "L1", "L3", "L4"} <= {t.layer for t in res.trace}


@pytest.mark.integration
@pytest.mark.anyio
async def test_temporal_question_uses_the_date_attribute_surface():
    """The capability the previous engine structurally lacked: a date lives only in a property."""

    wf = RetrievalWorkflow(embed, embed_batch,
                           WorkflowConfig(candidates_per_surface=60, budget_tokens=4000))
    res = await wf.run(ORG, "What did Nate do on 25 May, 2022?")

    print("\n" + format_trace(res))
    assert res.plan.temporal, "no temporal constraint parsed"
    assert res.plan.temporal[0].start == "2022-05-25"
    tags = {tag for b in res.blocks for tag in b.retrieved_by}
    assert any("attr_embedding" in t for t in tags), f"attribute surface never fired: {tags}"


@pytest.mark.integration
@pytest.mark.anyio
async def test_multi_hop_question_produces_paths():

    wf = RetrievalWorkflow(embed, embed_batch,
                           WorkflowConfig(candidates_per_surface=60, budget_tokens=6000))
    res = await wf.run(ORG, "What nickname does Nate use for Joanna?")

    print("\n" + format_trace(res))
    l2 = [t for t in res.trace if t.layer == "L2"]
    assert l2, "graph layer never ran"
    scored = [t for t in l2 if t.stage == "edge_scoring"]
    if scored:
        assert scored[0].n_out > 0, "no relations were scored"
