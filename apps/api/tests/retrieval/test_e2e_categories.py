"""End-to-end: every query category through the whole workflow, on the real graph.

`test_e2e.py` proves the pipeline runs and degrades safely. This proves the thing that was
missing until now — that the CLASSIFICATION reaches the bottom of the stack and changes what
happens, rather than being computed, traced, and ignored.

Each category is checked on three axes:

  1. it classifies                      — the question is understood
  2. the plan is ACTED ON               — the trace shows the surfaces the category implies, and
                                          shows the ones it does not, skipped with a reason
  3. the result is usable               — evidence came back, or the engine said honestly that it
                                          could not answer

Negative flows get equal weight, because every category's failure mode is to return something
confident and wrong: an aggregation over types the corpus lacks, a binding for a mention that
names nothing, a narrow plan that finds too little and never widens.

Marked `integration`: needs the populated graph and the embedding service.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import Intent
from opencortyx.retrieval.orchestrator import (
    RetrievalWorkflow, WorkflowConfig,
)

pytestmark = pytest.mark.integration

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")
EMPTY_ORG = uuid.UUID("00000000-0000-4000-8000-000000000000")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def _embed(text: str):
    from opencortyx.core.inference.embedding import embed
    return await embed(text)


async def _embed_batch(texts):
    from opencortyx.core.inference.embedding import embed_batch
    return await embed_batch(texts)


def _wf():
    return RetrievalWorkflow(_embed, _embed_batch,
                             WorkflowConfig(candidates_per_surface=40, budget_tokens=4000))


async def run(query: str, org: uuid.UUID = ORG):
    from opencortyx.core.db import V2ReadSession
    async with V2ReadSession() as db:
        return await _wf().run(db, org, query)


def stages(result) -> set[str]:
    return {f"{tr.layer}:{tr.stage}" for tr in result.trace}


def ran_surface(result, name: str) -> bool:
    return f"L1:surface:{name}" in stages(result)


# ── the pipeline still holds together ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_every_run_reports_every_layer():
    """A pipeline that reports one number cannot tell a recall problem from a ranking problem."""
    r = await run("What is Joanna allergic to?")
    layers = {tr.layer for tr in r.trace}
    assert {"L0", "L1", "L3", "L4"} <= layers, f"missing layers: {layers}"
    assert "L0:vocabulary" in stages(r) and "L0:plan" in stages(r)


# ── A. exact lookup ──────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_A_exact_lookup_resolves_and_does_not_pay_for_ann_or_graph():
    r = await run("What is the date_time of Session 4?")
    assert r.plan.intent is Intent.EXACT_LOOKUP
    assert not ran_surface(r, "embedding") or not r.plan.policy.allows("node_embedding")
    assert "graph" in r.plan.policy.skipped, "a graph walk was planned for a field read"
    assert "L2:skipped" in stages(r) or "L2:beam" not in stages(r)


# ── D. entity ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_D_entity_question_resolves_then_walks_its_neighbourhood():
    r = await run("Tell me about Joanna")
    assert r.plan.intent is Intent.ENTITY
    assert "L1:resolve" in stages(r), "an entity question never resolved anything"
    bound = [e for e in r.plan.entities if e.resolved]
    assert bound, "the mention 'Joanna' bound to nothing on a graph that contains her"
    assert bound[0].confidence >= 0.9 and "exact" in bound[0].matched_by
    assert r.plan.policy.allows("graph"), "a profile is its neighbourhood"


@pytest.mark.anyio
async def test_a_resolved_entity_seeds_the_graph_ahead_of_a_text_match():
    """Fusion ranks by how well something matched the question's WORDS; resolution establishes
    that a node IS the thing the question named. The walk must start from the latter."""
    r = await run("Tell me about Joanna")
    assert "L2:seeds" in stages(r)
    seed_trace = next(tr for tr in r.trace if tr.stage == "seeds")
    assert "resolved entity seed" in seed_trace.detail


# ── E/F. relationship and multi-hop ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_EF_relational_questions_run_the_graph_and_skip_lexical():
    r = await run("Who did Joanna talk to about her allergies?")
    assert r.plan.intent in (Intent.RELATIONSHIP, Intent.MULTI_HOP, Intent.ENTITY)
    if r.plan.intent in (Intent.RELATIONSHIP, Intent.MULTI_HOP):
        assert r.plan.policy.allows("graph")
        assert "bm25" in r.plan.policy.skipped


# ── G. temporal ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_G_temporal_question_reaches_the_filtering_surface():
    r = await run("What did Nate do on 25 May, 2022?")
    assert r.plan.temporal, "a date in the question produced no constraint"
    assert r.plan.policy.allows("metadata"), "a time bound never reached the filtering surface"
    assert r.blocks


# ── H. aggregation ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_H_aggregation_is_computed_not_stuffed():
    r = await run("How many turns are there?")
    assert r.plan.intent is Intent.AGGREGATION
    assert r.aggregation is not None, "a counting question went down the retrieval path"
    assert r.aggregation.scalar and r.aggregation.scalar > 1000
    assert len(r.blocks) == 1, "a computed answer was padded with a sample to count"
    assert not ran_surface(r, "embedding"), "a dense sweep ran for a count"


@pytest.mark.anyio
async def test_H_grouped_aggregation_covers_every_group():
    r = await run("How many turns did each session have?")
    assert r.aggregation is not None
    assert r.aggregation.groups_total > 100, "this looks like a sample, not a computation"


# ── negative flows ───────────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_an_aggregation_the_corpus_cannot_ground_falls_back_to_retrieval():
    """"How many transactions per company" against a conversation graph. Returning 0 would be a
    measured-looking answer to a question this corpus cannot address."""
    r = await run("How many transactions did each company have?")
    assert r.aggregation is None, "an ungroundable count produced a computed answer"
    assert any("not grounded" in tr.detail for tr in r.trace), \
        "the fallback happened without saying why"
    assert r.blocks or r.abstained, "fell back to retrieval and then returned nothing at all"


@pytest.mark.anyio
async def test_unconnected_types_fall_back_rather_than_report_zero():
    """`Fact` reaches `Session` only through `Turn`."""
    r = await run("How many facts per session?")
    assert r.aggregation is None
    assert any("inconclusive" in tr.detail for tr in r.trace)


@pytest.mark.anyio
async def test_a_mention_that_names_nothing_does_not_bind():
    r = await run("Tell me about Zzzzznotathing")
    bound = [e for e in r.plan.entities if e.resolved]
    assert not bound, f"an invented name bound to {bound and bound[0].label!r}"
    assert not any("FAILED" in tr.detail for tr in r.trace), "resolution failure crashed a surface"


@pytest.mark.anyio
async def test_an_empty_query_is_safe():
    r = await run("")
    assert r.plan is not None and r.plan.strategies
    assert not any("FAILED" in tr.detail for tr in r.trace)


@pytest.mark.anyio
async def test_an_unrecognised_question_widens_instead_of_narrowing():
    r = await run("Something entirely unlike the training patterns")
    assert r.plan.confidence <= 0.5
    # Precondition-gated surfaces are skipped for lack of INPUT, not for lack of confidence:
    # no mention to vary, no constraint to filter, no type hypothesis to search within.
    narrowed = set(r.plan.policy.skipped) - {"fuzzy", "entity", "metadata", "ontology"}
    assert not narrowed, f"an unconfident plan narrowed away {sorted(narrowed)}"


@pytest.mark.anyio
async def test_an_org_with_no_graph_returns_empty_without_crashing():
    """Every layer must degrade. An empty org exercises all of them at once."""
    r = await run("What is Joanna allergic to?", org=EMPTY_ORG)
    assert r.blocks == [] or r.abstained
    assert not any("FAILED" in tr.detail for tr in r.trace), \
        [tr.detail for tr in r.trace if "FAILED" in tr.detail]


@pytest.mark.anyio
async def test_no_surface_ever_fails_on_a_normal_question():
    """The resolver's bind and trigram defects both surfaced as a silently FAILED surface."""
    for q in ("What is Joanna allergic to?", "Tell me about Nate",
              "What did Nate do on 25 May, 2022?"):
        r = await run(q)
        failures = [tr.detail for tr in r.trace if "FAILED" in tr.detail]
        assert not failures, f"{q!r}: {failures}"


# ── the classification actually changes the run ──────────────────────────────────────────────

@pytest.mark.anyio
async def test_two_categories_do_not_execute_the_same_pipeline():
    """The original failure: a lookup and a reasoning question ran identical plans."""
    lookup = await run("What is the date_time of Session 4?")
    profile = await run("Tell me about Joanna")
    assert lookup.plan.policy.run != profile.plan.policy.run
    assert stages(lookup) != stages(profile)
