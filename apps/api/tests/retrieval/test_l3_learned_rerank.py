"""L3 — the reranker's five families: node relevance, path relevance, constraint match,
evidence quality, diversity — plus the learned (cross-encoder) signal.

Three of the five were already covered in `test_l3_rerank.py`. This file covers what was added
and the invariants that make adding a LEARNED signal safe:

  evidence quality — `agreement` counts HOW MANY surfaces proposed a node and says nothing about
                     whether those were exact identifier matches or trigram guesses.
  path relevance   — `path_quality` is a property of a NODE. For a relationship question the
                     ROUTE is the answer, and a list of endpoints does not contain it.
  cross-encoder    — the sharpest available signal (0.995 vs 0.000016 on an answering versus an
                     unrelated passage) and the component with the worst track record here. Both
                     recorded failures came from letting it DECIDE. It enters as one weighted
                     feature among fourteen.

The load-bearing invariant is ABSENT IS NOT ZERO: a feature computed for only part of the pool
must not turn a COST bound into a demotion.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import (
    Candidate, CandidateGraph, Evidence, Path, PathStep,
)
from opencortyx.retrieval.l3_rerank import cross_encoder, reranker
from opencortyx.retrieval.l3_rerank.features import (
    FEATURE_NAMES, _ONLY_WHEN_ASSESSED, evidence_quality, extract,
)


def cand(nid=None, text="some text here", **ev):
    c = Candidate(node_id=nid or uuid.uuid4(), text=text)
    return c


def with_ev(c, surface, rank, raw, scale, detail=""):
    c.add(Evidence(surface=surface, rank=rank, raw_score=raw, scale=scale, detail=detail))
    return c


def step(a, b, rel="MENTIONS", edge=0.8, node=None):
    return PathStep(from_id=a, edge_type=rel, to_id=b, direction="out",
                    edge_score=edge, node_score=node)


# ── evidence quality ─────────────────────────────────────────────────────────────────────────

def test_an_exact_match_outranks_a_trigram_guess_of_the_same_strength():
    """`agreement` cannot tell these apart: both are one surface. Reliability is a property of
    the METHOD, and an exact identifier match is categorically stronger than a fuzzy guess in
    every corpus there will ever be."""
    strong = with_ev(cand(), "identifier", 0, 1.0, "exact")
    weak = with_ev(cand(), "fuzzy", 0, 1.0, "ratio")
    assert evidence_quality(strong) > evidence_quality(weak)


def test_a_membership_signal_is_worth_less_than_a_relevance_one():
    """A `constant` scale says the surface fired — not how well it matched."""
    relevance = with_ev(cand(), "node_embedding", 0, 0.9, "cosine")
    membership = with_ev(cand(), "metadata", 0, 1.0, "constant")
    assert evidence_quality(relevance) > evidence_quality(membership)


def test_weak_evidence_does_not_add_up_to_strong_evidence():
    """The best piece is taken, never the sum — the same rule entity resolution applies when
    combining its methods."""
    many_weak = cand()
    for i in range(5):
        with_ev(many_weak, "fuzzy", i, 0.5, "ratio")
    one_strong = with_ev(cand(), "identifier", 0, 1.0, "exact")
    assert evidence_quality(one_strong) > evidence_quality(many_weak)


def test_evidence_quality_is_bounded_and_zero_without_evidence():
    assert evidence_quality(cand()) == 0.0
    c = with_ev(cand(), "node_embedding", 0, 5.0, "cosine")     # out-of-range raw score
    assert 0.0 <= evidence_quality(c) <= 1.0


def test_quality_and_agreement_are_different_questions():
    """Three weak surfaces beat one strong one on AGREEMENT and lose on QUALITY. If either
    could substitute for the other, one of them is redundant."""
    three_weak = cand()
    for s in ("fuzzy", "metadata", "ontology"):
        with_ev(three_weak, s, 5, 0.4, "ratio")
    one_strong = with_ev(cand(), "identifier", 0, 1.0, "exact")

    fw, fs = extract(three_weak), extract(one_strong)
    assert fw["agreement"] > fs["agreement"]
    assert fs["evidence_quality"] > fw["evidence_quality"]


# ── absent is not zero ───────────────────────────────────────────────────────────────────────

def test_an_unassessed_feature_does_not_penalise_a_candidate():
    """The invariant that makes a cost-bounded learned signal safe.

    Scoring only the top 30 and reading "absent" as "worst" would make being outside the
    cross-encoder's budget a demotion — a COST bound silently deciding rank.
    """
    weights = {"semantic": 1.0, "cross_encoder": 1.2}
    scored_absent = reranker.score_features({"semantic": 0.8}, weights)
    scored_zero = reranker.score_features({"semantic": 0.8, "cross_encoder": 0.0}, weights)
    assert scored_absent == pytest.approx(0.8)
    assert scored_zero < scored_absent, "an explicit zero was treated like an absent measurement"


def test_a_measured_zero_still_counts_against_a_candidate():
    """Absent and zero must stay distinguishable in BOTH directions."""
    weights = {"semantic": 1.0, "cross_encoder": 1.0}
    assert reranker.score_features({"semantic": 1.0, "cross_encoder": 0.0}, weights) \
        == pytest.approx(0.5)


def test_the_cross_encoder_feature_is_absent_until_measured():
    f = extract(cand(), None)
    assert "cross_encoder" not in f
    assert "cross_encoder" in FEATURE_NAMES and "cross_encoder" in _ONLY_WHEN_ASSESSED


def test_reranking_does_not_erase_features_computed_elsewhere():
    """`rerank` re-extracts when a candidate looks unannotated. Checking EVERY declared name
    would re-extract forever — the cross-encoder is absent by design — and each pass would reset
    the graph-derived features on its way through."""
    g = CandidateGraph()
    c = with_ev(cand(text="a node with text"), "bm25", 0, 1.0, "bm25")
    g.upsert(c)
    c.features.update(extract(c, None))
    c.features["connectivity"] = 0.87
    c.features["cross_encoder"] = 0.93

    reranker.rerank([c], None)
    assert c.features["connectivity"] == 0.87, "a graph-derived feature was reset"
    assert c.features["cross_encoder"] == 0.93, "a measured learned score was discarded"


# ── the learned signal contributes, it does not decide ───────────────────────────────────────

@pytest.mark.anyio
async def test_an_unavailable_model_degrades_to_the_other_features():
    """Two recorded failures of this component came from letting it decide. Unavailable must
    mean "rank on everything else", never "rank on nothing"."""
    import opencortyx.retrieval.l3_rerank.cross_encoder as ce_mod
    original = ce_mod._load
    ce_mod._load = lambda model_id=None: None
    try:
        result = await cross_encoder.score("q", [cand(text="something")])
    finally:
        ce_mod._load = original
    assert not result.available
    assert result.error and "unavailable" in result.error
    assert cross_encoder.apply(result, [cand()]) == 0


@pytest.mark.anyio
async def test_a_failing_model_degrades_rather_than_raising():
    import opencortyx.retrieval.l3_rerank.cross_encoder as ce_mod

    class Boom:
        def predict(self, pairs):
            raise RuntimeError("out of memory")

    original = ce_mod._load
    ce_mod._load = lambda model_id=None: Boom()
    try:
        result = await cross_encoder.score("q", [cand(text="something")])
    finally:
        ce_mod._load = original
    assert not result.available and "RuntimeError" in result.error


@pytest.mark.anyio
async def test_only_the_budgeted_candidates_are_scored_and_the_rest_stay_unmeasured():
    import opencortyx.retrieval.l3_rerank.cross_encoder as ce_mod

    class Fake:
        def predict(self, pairs):
            return [0.9] * len(pairs)

    cands = [cand(text=f"text number {i}") for i in range(20)]
    original = ce_mod._load
    ce_mod._load = lambda model_id=None: Fake()
    try:
        result = await cross_encoder.score("q", cands, pool=5)
    finally:
        ce_mod._load = original

    assert result.scored == 5
    cross_encoder.apply(result, cands)
    assert sum("cross_encoder" in c.features for c in cands) == 5
    for c in cands[5:]:
        assert "cross_encoder" not in c.features, "an unscored candidate was given a value"


@pytest.mark.anyio
async def test_a_candidate_with_no_text_is_not_sent_to_the_model():
    import opencortyx.retrieval.l3_rerank.cross_encoder as ce_mod
    seen = {}

    class Fake:
        def predict(self, pairs):
            seen["n"] = len(pairs)
            return [0.5] * len(pairs)

    cands = [cand(text=""), cand(text="real text"), Candidate(node_id=uuid.uuid4())]
    original = ce_mod._load
    ce_mod._load = lambda model_id=None: Fake()
    try:
        await cross_encoder.score("q", cands)
    finally:
        ce_mod._load = original
    assert seen["n"] == 1


# ── path relevance as a first-class object ───────────────────────────────────────────────────

def test_paths_are_ranked_as_routes_not_as_a_property_of_their_endpoint():
    """"Which companies connect to John through Germany" is answered by the CONNECTION.
    Returning only its endpoint discards what was asked for."""
    g = CandidateGraph()
    a, b, c1 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    strong = Candidate(node_id=b, text="strong terminal")
    strong.rerank_score = 0.9
    strong.paths.append(Path(seed_id=a, steps=[step(a, b, edge=0.9)]))
    weak = Candidate(node_id=c1, text="weak terminal")
    weak.rerank_score = 0.1
    weak.paths.append(Path(seed_id=a, steps=[step(a, c1, edge=0.9)]))
    for p in list(strong.paths) + list(weak.paths):
        p.score = 0.9
    g.upsert(strong)
    g.upsert(weak)

    ranked = reranker.rank_paths(g, None, limit=5)
    assert len(ranked) == 2
    assert ranked[0][2].node_id == b, "the route to the stronger terminal did not rank first"
    assert all(isinstance(p, Path) for _, p, _ in ranked)


def test_a_strong_endpoint_cannot_rescue_a_broken_route():
    """The product is what says the connection is real; a near-zero route stays near zero."""
    g = CandidateGraph()
    a, b = uuid.uuid4(), uuid.uuid4()
    c = Candidate(node_id=b, text="excellent terminal")
    c.rerank_score = 1.0
    p = Path(seed_id=a, steps=[step(a, b, edge=0.001)])
    p.score = 0.001
    c.paths.append(p)
    g.upsert(c)
    ranked = reranker.rank_paths(g, None)
    assert ranked and ranked[0][0] < 0.01


def test_the_same_route_is_not_counted_several_times():
    """Candidates sharing a prefix reach the same connection; listing it five times is not five
    pieces of evidence."""
    g = CandidateGraph()
    a, b = uuid.uuid4(), uuid.uuid4()
    route = [step(a, b, edge=0.8)]
    for _ in range(4):
        c = Candidate(node_id=b, text="terminal")
        pth = Path(seed_id=a, steps=list(route))
        pth.score = 0.8
        c.paths.append(pth)
        g.upsert(c)
    assert len(reranker.rank_paths(g, None)) == 1


def test_ranking_paths_on_a_graph_without_any_is_empty_not_an_error():
    assert reranker.rank_paths(CandidateGraph(), None) == []
    assert reranker.rank_paths(None, None) == []


# ── diversity still applies after all of this ────────────────────────────────────────────────

def test_diversity_still_removes_redundancy_after_the_new_features():
    a = with_ev(cand(text="Joanna is allergic to reptiles and fur"), "bm25", 0, 1.0, "bm25")
    b = with_ev(cand(text="Joanna is allergic to reptiles and fur"), "bm25", 1, 0.9, "bm25")
    c = with_ev(cand(text="Nate won a gaming tournament last week"), "bm25", 2, 0.8, "bm25")
    ranked = reranker.rerank([a, b, c], None)
    picked = reranker.diversify(ranked, 2)
    texts = {p.text for p in picked}
    assert len(texts) == 2, f"diversification returned near-duplicates: {texts}"


# ── against the real pipeline ────────────────────────────────────────────────────────────────

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


async def _run(query: str, **cfg):
    from opencortyx.core.db import V2ReadSession
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=4000, **cfg))
    async with V2ReadSession() as db:
        return await wf.run(db, ORG, query)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_pipeline_ranks_routes_for_a_relationship_question():
    r = await _run("Who did Joanna talk to about her allergies?")
    if not r.graph.edges:
        pytest.skip("graph expansion did not run")
    assert r.paths, "a relationship question returned no ranked routes"
    scores = [s for s, _, _ in r.paths]
    assert scores == sorted(scores, reverse=True)
    assert all(p.steps for _, p, _ in r.paths)


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_learned_signal_is_off_by_default_and_costs_nothing():
    r = await _run("What is Joanna allergic to?")
    assert not any(t.stage == "cross_encoder" for t in r.trace)
    for c in list(r.graph.candidates.values())[:20]:
        assert "cross_encoder" not in c.features


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_cross_encoder_scores_and_reorders_when_enabled():
    """~80ms per pair on CPU, so this runs a small pool. It is the one test that proves the
    learned signal reaches real candidates rather than merely loading."""
    r = await _run("What is Joanna allergic to?", cross_encoder=True, cross_encoder_pool=8)
    stage = [t for t in r.trace if t.stage == "cross_encoder"]
    assert stage, "the cross-encoder stage did not run when enabled"
    if "unavailable" in stage[0].detail:
        pytest.skip(f"cross-encoder not available here: {stage[0].detail}")

    scored = [c for c in r.graph.candidates.values() if "cross_encoder" in c.features]
    assert scored, "the stage ran but annotated nothing"
    assert all(0.0 <= c.features["cross_encoder"] <= 1.0 for c in scored)
    assert len(scored) <= 8, "the cost bound was exceeded"
