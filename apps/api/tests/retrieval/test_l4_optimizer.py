"""L4 — the context optimizer: dedup, compression, token budget, evidence coverage.

Each of the four is tested on its own, then together, because they trade against each other and a
combined result hides which one made the trade. Node count and per-node text are bought with the
same currency: a previous configuration drifted from ~119 tokens per node to ~84 while packing
more nodes, and node-identity recall rewarded that unconditionally — it asks whether the right
node arrived, never whether enough of it survived to answer from.

`evidence_coverage` is the answer to that. `evidence_density` says how much was packed; coverage
says whether the packing speaks to the whole question. A window filled with fifty blocks that all
address one facet is dense and uninformative, and no token count reveals it.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import ContextBlock
from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l4_context import optimizer
from opencortyx.retrieval.l4_context.builder import estimate_tokens


def blk(text, score=0.5, role="primary", by=("bm25",), nid=None):
    b = ContextBlock(node_id=nid or uuid.uuid4(), role=role, text=text,
                     retrieved_by=list(by), score=score)
    b.tokens = estimate_tokens(text)
    return b


# ── dedup ────────────────────────────────────────────────────────────────────────────────────

def test_the_same_node_appears_once_and_keeps_its_best_scoring_copy():
    nid = uuid.uuid4()
    low = blk("some evidence", score=0.2, nid=nid)
    high = blk("some evidence", score=0.9, nid=nid)
    out = optimizer.dedupe([low, high])
    assert len(out) == 1 and out[0].score == 0.9


def test_near_identical_blocks_merge_and_keep_both_provenances():
    """Two independent surfaces producing equivalent statements is evidence of agreement, and
    dropping one hides it."""
    a = blk("Joanna is allergic to reptiles and animals with fur", score=0.9, by=("bm25",))
    b = blk("Joanna is allergic to reptiles and animals with fur.", score=0.5,
            by=("node_embedding",))
    out = optimizer.merge_equivalents([a, b])
    assert len(out) == 1
    surfaces = {tag.split("(")[0] for tag in out[0].retrieved_by}
    assert surfaces == {"bm25", "node_embedding"}


def test_a_merge_never_lists_one_surface_twice():
    """Provenance is unioned by SURFACE. Unioning by tag string made one surface read as several
    agreeing sources."""
    a = blk("identical text here for merging", by=("node_embedding(entity)",))
    b = blk("identical text here for merging", by=("node_embedding(entity,concept)",))
    out = optimizer.merge_equivalents([a, b])
    names = [t.split("(")[0] for t in out[0].retrieved_by]
    assert len(names) == len(set(names)), out[0].retrieved_by


def test_genuinely_different_evidence_is_not_merged():
    a = blk("Joanna is allergic to reptiles")
    b = blk("Nate won a gaming tournament in May")
    assert len(optimizer.merge_equivalents([a, b])) == 2


# ── compression ──────────────────────────────────────────────────────────────────────────────

def test_compression_prefers_a_sentence_boundary():
    text = "First sentence is complete. Second sentence trails off into something much longer"
    out = optimizer.compress(text, 40)
    assert out.endswith("."), out
    assert "First sentence is complete." in out


def test_a_fragment_is_marked_when_no_boundary_is_usable():
    out = optimizer.compress("aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj", 20)
    assert out.endswith("…")
    assert len(out) <= 22


def test_text_within_budget_is_untouched():
    text = "short enough already"
    assert optimizer.compress(text, 500) == text


# ── token budget ─────────────────────────────────────────────────────────────────────────────

def test_the_budget_is_respected():
    blocks = [blk("word " * 40, score=1.0 - i / 20) for i in range(20)]
    out = optimizer.optimize(blocks, budget_tokens=120)
    assert sum(b.tokens for b in out) <= 120


def test_a_zero_budget_selects_nothing_rather_than_erroring():
    assert optimizer.optimize([blk("anything")], 0) == []


def test_no_role_is_starved_by_a_dominant_one():
    """A floor per ROLE (what a block contributes) is not a floor per TYPE (what a node is): it
    only guarantees that relationship evidence, if it exists, is not crowded out."""
    primary = [blk("primary evidence " * 8, score=0.99, role="primary") for _ in range(20)]
    rel = [blk("relationship evidence " * 8, score=0.30, role="relationship") for _ in range(3)]
    out = optimizer.optimize(primary + rel, budget_tokens=400)
    roles = {b.role for b in out}
    assert "relationship" in roles, "the lower-scoring role was crowded out entirely"


def test_a_stub_too_small_to_carry_anything_is_not_bought():
    """A three-token fragment costs budget and carries nothing."""
    blocks = [blk("word " * 30, score=0.9), blk("word " * 30, score=0.8)]
    out = optimizer.optimize(blocks, budget_tokens=32, min_block_tokens=12)
    assert all(b.tokens >= 5 for b in out)


# ── evidence coverage ────────────────────────────────────────────────────────────────────────

def test_coverage_reports_what_the_question_asked_and_nothing_answered():
    plan = build_plan("What is Joanna allergic to?")
    ctx = [blk("Joanna said something unrelated about the weather")]
    cov = optimizer.evidence_coverage(ctx, plan)
    assert cov["of"] > 0
    assert cov["term_coverage"] < 1.0
    assert any(u.startswith("allerg") for u in cov["uncovered"]), cov


def test_coverage_reaches_one_when_the_question_is_fully_spoken_to():
    plan = build_plan("What is Joanna allergic to?")
    ctx = [blk("Joanna has severe allergies to reptiles")]
    assert optimizer.evidence_coverage(ctx, plan)["term_coverage"] == 1.0


def test_coverage_of_nothing_is_zero_not_an_error():
    plan = build_plan("What is Joanna allergic to?")
    cov = optimizer.evidence_coverage([], plan)
    assert cov["term_coverage"] == 0.0 and cov["covered"] == 0


def test_source_spread_counts_surfaces_not_tags():
    ctx = [blk("x", by=("attr_embedding(label)",)), blk("y", by=("attr_embedding(text)",))]
    assert optimizer.evidence_coverage(ctx, None)["source_spread"] == 1


def test_density_and_coverage_answer_different_questions():
    """Fifty blocks on one facet is dense and uninformative; the token count cannot say so."""
    plan = build_plan("What is Joanna allergic to?")
    narrow = [blk("Joanna Joanna Joanna talking", score=0.9) for _ in range(12)]
    assert optimizer.evidence_density(narrow)["n_blocks"] == 12
    assert optimizer.evidence_coverage(narrow, plan)["term_coverage"] < 1.0


def test_selection_prefers_a_block_that_covers_new_ground():
    """Ordering purely by score fills the window with the strongest evidence for one facet and
    leaves the rest uncovered however much budget remains."""
    plan = build_plan("What is Joanna allergic to?")
    same = [blk("Joanna Joanna spoke again and again about nothing", score=0.90)
            for _ in range(6)]
    novel = blk("severe allergies to reptiles", score=0.80)
    out = optimizer.optimize(same + [novel], budget_tokens=40, plan=plan)
    assert any(b is novel or b.text == novel.text for b in out), \
        "the only block covering the rest of the question was left out"


def test_novelty_never_outranks_evidence_outright():
    """The failure in the other direction is a context of loosely-relevant material that covers
    every word of the question and answers none of it."""
    plan = build_plan("What is Joanna allergic to?")
    strong = blk("Joanna has severe allergies to reptiles and fur", score=0.95)
    weak = blk("allergies reptiles Joanna unrelated filler", score=0.05)
    order = optimizer._coverage_ordered([weak, strong], plan, optimizer.COVERAGE_BONUS)
    assert order[0] is strong


def test_coverage_ordering_without_a_plan_is_plain_score_order():
    a, b = blk("x", score=0.2), blk("y", score=0.9)
    assert optimizer._coverage_ordered([a, b], None, 0.25) == [b, a]


# ── the four together, on the real pipeline ──────────────────────────────────────────────────

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


async def _embed(text: str):
    from opencortyx.core.inference.embedding import embed
    return await embed(text)


async def _embed_batch(texts):
    from opencortyx.core.inference.embedding import embed_batch
    return await embed_batch(texts)


async def _run(query: str, budget=4000):
    from opencortyx.core.db import V2ReadSession
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=budget))
    async with V2ReadSession() as db:
        return await wf.run(db, ORG, query)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_real_budget_is_never_exceeded():
    for budget in (500, 2000, 6000):
        r = await _run("What is Joanna allergic to?", budget=budget)
        if not r.blocks:
            pytest.skip("org has no graph loaded")
        assert r.tokens <= budget, f"budget {budget} exceeded with {r.tokens}"


@pytest.mark.integration
@pytest.mark.anyio
async def test_no_two_selected_blocks_are_the_same_node():
    r = await _run("What is Joanna allergic to?")
    if not r.blocks:
        pytest.skip("org has no graph loaded")
    ids = [str(b.node_id) for b in r.blocks]
    assert len(ids) == len(set(ids))


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_pipeline_reports_coverage_alongside_density():
    r = await _run("What is Joanna allergic to?")
    stage = [t for t in r.trace if t.stage == "optimize"]
    if not stage:
        pytest.skip("org has no graph loaded")
    cov = stage[0].extra.get("coverage")
    assert cov and "term_coverage" in cov
    assert 0.0 <= cov["term_coverage"] <= 1.0
    assert "cover=" in stage[0].detail


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_tight_budget_degrades_gracefully_rather_than_emptying():
    r = await _run("What is Joanna allergic to?", budget=220)
    if not r.graph.candidates:
        pytest.skip("org has no graph loaded")
    assert r.blocks, "a tight budget returned nothing at all"
    assert r.tokens <= 220
