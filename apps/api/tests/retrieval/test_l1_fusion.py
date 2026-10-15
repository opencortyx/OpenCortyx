"""L1 fusion tests — rank fusion, evidence preservation, and per-surface contribution.

The properties under test are the ones whose absence caused measured failures: scores from
different surfaces must never be summed as raw numbers, evidence must survive fusion intact, and
duplication within one surface must not out-vote agreement across surfaces.
"""
import uuid

from opencortyx.retrieval.contracts import Candidate, Evidence
from opencortyx.retrieval.l1_candidates.fusion import (
    RRF_K, cut, merge, rrf, surface_report,
)
from opencortyx.retrieval.l1_candidates.lexical import query_terms


def c(nid, surface, rank, score, scale, detail=""):
    cand = Candidate(node_id=nid)
    cand.add(Evidence(surface=surface, rank=rank, raw_score=score, scale=scale, detail=detail))
    return cand


def test_merge_unions_and_accumulates_evidence():
    a, b = uuid.uuid4(), uuid.uuid4()
    g = merge([
        [c(a, "bm25", 0, 12.5, "bm25"), c(b, "bm25", 1, 9.0, "bm25")],
        [c(a, "node_embedding", 4, 0.71, "cosine")],
    ])
    assert len(g) == 2
    assert g.candidates[a].surfaces == {"bm25", "node_embedding"}
    assert len(g.candidates[a].evidence) == 2


def test_rrf_never_sums_raw_scores():
    """A bm25 score of 30 must not beat a cosine of 0.9 by arithmetic."""
    big, small = uuid.uuid4(), uuid.uuid4()
    g = merge([
        [c(big, "bm25", 5, 30.0, "bm25")],          # huge raw number, poor rank
        [c(small, "node_embedding", 0, 0.9, "cosine")],  # small raw number, best rank
    ])
    rrf(g)
    assert g.candidates[small].fusion_score > g.candidates[big].fusion_score


def test_agreement_across_surfaces_beats_depth_within_one():
    agree, deep = uuid.uuid4(), uuid.uuid4()
    g = merge([
        [c(agree, "bm25", 10, 1.0, "bm25")],
        [c(agree, "node_embedding", 10, 0.5, "cosine")],
        [c(agree, "attr_embedding", 10, 0.5, "cosine", "description")],
        [c(deep, "node_embedding", 3, 0.95, "cosine")],
    ])
    rrf(g)
    assert g.candidates[agree].fusion_score > g.candidates[deep].fusion_score
    assert g.candidates[agree].features["n_surfaces"] == 3


def test_duplication_within_a_surface_does_not_multiply_the_vote():
    """Three hits from one surface must count once — otherwise a chatty retriever wins."""
    dup, single = uuid.uuid4(), uuid.uuid4()
    g = merge([[c(dup, "bm25", 0, 5.0, "bm25"),
                c(dup, "bm25", 1, 4.0, "bm25"),
                c(dup, "bm25", 2, 3.0, "bm25")],
               [c(single, "bm25", 0, 5.0, "bm25")]])
    rrf(g)
    assert abs(g.candidates[dup].fusion_score - g.candidates[single].fusion_score) < 1e-9


def test_weights_scale_votes_but_never_remove_them():
    """A plan is a hypothesis: a down-weighted surface still contributes."""
    x = uuid.uuid4()
    g = merge([[c(x, "fuzzy", 0, 0.4, "ratio")]])
    rrf(g, weights={"fuzzy": 0.15})
    assert g.candidates[x].fusion_score > 0
    expected = 0.15 / (RRF_K + 1)
    assert abs(g.candidates[x].fusion_score - expected) < 1e-9


def test_an_unplanned_surface_votes_but_never_outvotes_a_planned_one():
    """Silence about a surface must not be the loudest verdict.

    The default was 1.0 — higher than any weight the planner can assign — so `identifier`, which
    runs on every query and appears in no registry, outvoted every deliberately-weighted surface
    on every query. Influence decided by absence from a table is the bug this engine exists to
    remove; it does not become acceptable because the table is one of surfaces rather than types.
    """
    from opencortyx.retrieval.l1_candidates.fusion import (
        UNPLANNED_SURFACE_WEIGHT,
    )
    x, y = uuid.uuid4(), uuid.uuid4()
    g = merge([[c(x, "brand_new_surface", 0, 1.0, "cosine")],
               [c(y, "bm25", 0, 1.0, "bm25")]])
    rrf(g, weights={"bm25": 0.9})
    assert g.candidates[x].fusion_score > 0, "an unplanned surface lost its vote entirely"
    assert g.candidates[x].fusion_score < g.candidates[y].fusion_score
    assert abs(g.candidates[x].fusion_score - UNPLANNED_SURFACE_WEIGHT / (RRF_K + 1)) < 1e-9


def test_evidence_survives_fusion_untouched():
    x = uuid.uuid4()
    g = merge([[c(x, "attr_embedding", 2, 0.83, "cosine", "date_time")]])
    rrf(g, weights={"attr_embedding": 0.7})
    ev = g.candidates[x].evidence[0]
    assert ev.raw_score == 0.83 and ev.scale == "cosine" and ev.detail == "date_time"
    assert g.candidates[x].best("cosine") == 0.83


def test_surface_report_counts_unique_contribution():
    a, b, shared = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    g = merge([
        [c(a, "bm25", 0, 1, "bm25"), c(shared, "bm25", 1, 1, "bm25")],
        [c(b, "node_embedding", 0, 0.9, "cosine"), c(shared, "node_embedding", 1, 0.8, "cosine")],
    ])
    rep = surface_report(g)
    assert rep["bm25"]["unique"] == 1 and rep["bm25"]["overlap"] == 1
    assert rep["node_embedding"]["unique"] == 1


def test_cut_is_separate_from_fusion():
    g = merge([[c(uuid.uuid4(), "bm25", i, 1.0, "bm25")] for i in range(10)])
    rrf(g)
    assert len(g.ranked()) == 10        # fusion never truncates
    assert len(cut(g, 3)) == 3
    assert len(cut(g, 0)) == 10         # 0 means "no cut", not "nothing"


def test_query_terms_drops_stopwords_and_keeps_content():
    t = query_terms("What is Joanna allergic to?")
    assert "joanna" in t and "allergic" in t
    assert "what" not in t and "is" not in t and "to" not in t


def test_query_terms_empty_means_do_not_run():
    assert query_terms("what is the") == []
    assert query_terms("") == []


# ── dedup: identity, never content ───────────────────────────────────────────────────────────

def test_the_same_node_from_three_surfaces_becomes_one_candidate():
    """Dedup at fusion is by NODE IDENTITY. Three surfaces finding the same node is the
    strongest signal there is; it must not arrive as three candidates competing with each other
    for a context budget."""
    x = uuid.uuid4()
    g = merge([[c(x, "bm25", 0, 3.0, "bm25")],
               [c(x, "node_embedding", 5, 0.8, "cosine")],
               [c(x, "fuzzy", 2, 0.6, "ratio")]])
    assert len(g) == 1
    cand = g.candidates[x]
    assert cand.surfaces == {"bm25", "node_embedding", "fuzzy"}
    assert len(cand.evidence) == 3


def test_two_different_nodes_with_identical_text_stay_separate():
    """Fusion must NOT dedup on content.

    A `Fact` and the `Turn` it was extracted from can carry the same words and are not the same
    evidence — one is a claim, the other is where the claim came from, and collapsing them at
    fusion destroys the provenance an answer is defended with. Near-duplicate CONTENT is L4's
    problem, after ranking, where a budget decision is being made.
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    ca = Candidate(node_id=a, text="Joanna is allergic to reptiles", node_type="Fact")
    cb = Candidate(node_id=b, text="Joanna is allergic to reptiles", node_type="Turn")
    ca.add(Evidence(surface="bm25", rank=0, raw_score=1.0, scale="bm25"))
    cb.add(Evidence(surface="bm25", rank=1, raw_score=0.9, scale="bm25"))
    g = merge([[ca], [cb]])
    assert len(g) == 2, "fusion collapsed two distinct nodes because their text matched"


def test_merge_keeps_paths_and_fills_missing_fields_without_overwriting():
    x = uuid.uuid4()
    rich = Candidate(node_id=x, text="the full text", label="L", node_type="Fact",
                     properties={"k": "v"})
    rich.add(Evidence(surface="bm25", rank=0, raw_score=1.0, scale="bm25"))
    bare = Candidate(node_id=x, text="", label="", node_type="")
    bare.add(Evidence(surface="fuzzy", rank=1, raw_score=0.5, scale="ratio"))

    g = merge([[rich], [bare]])
    m = g.candidates[x]
    assert m.text == "the full text" and m.label == "L" and m.node_type == "Fact"
    assert m.properties == {"k": "v"}
    assert len(m.evidence) == 2


# ── evidence merge: entries must stay distinguishable ────────────────────────────────────────

def test_evidence_from_one_surface_stays_distinguishable():
    """Two entries identical in surface, rank AND detail cannot be told apart, so they cannot be
    audited: there is no way to know whether that is corroboration from two query framings or a
    bookkeeping duplicate.

    Measured: the embedding surface runs once per query representation, and `attr_ann` did not
    record which one fired, so `attr_embedding/rank 0/label` appeared twice on the same candidate.
    18 such entries on a single real query.
    """
    from opencortyx.retrieval.l1_candidates.embedding import (
        EmbeddingRetrieval,
    )
    import inspect
    sig = inspect.signature(EmbeddingRetrieval.attr_ann)
    assert "rep" in sig.parameters, "attribute evidence cannot record which representation fired"

    x = uuid.uuid4()
    a = Candidate(node_id=x)
    a.add(Evidence(surface="attr_embedding", rank=0, raw_score=0.9, scale="cosine",
                   detail="label|full"))
    b = Candidate(node_id=x)
    b.add(Evidence(surface="attr_embedding", rank=0, raw_score=0.9, scale="cosine",
                   detail="label|concept"))
    g = merge([[a], [b]])
    details = [e.detail for e in g.candidates[x].evidence]
    assert len(set(details)) == 2, f"indistinguishable evidence: {details}"


def test_duplicate_evidence_still_cannot_multiply_a_vote():
    """Even if identical entries did appear, RRF takes the best rank per surface."""
    x = uuid.uuid4()
    cand = Candidate(node_id=x)
    for _ in range(5):
        cand.add(Evidence(surface="bm25", rank=0, raw_score=1.0, scale="bm25"))
    g = merge([[cand]])
    rrf(g, weights={"bm25": 1.0})
    assert abs(g.candidates[x].fusion_score - 1.0 / (RRF_K + 1)) < 1e-9


# ── RRF called more than once ────────────────────────────────────────────────────────────────

def test_rrf_replaces_rather_than_accumulates_when_called_again():
    """The orchestrator fuses after L1 and again after L2, once graph evidence has arrived. If
    the second pass added to the first, every candidate's score would double for reasons having
    nothing to do with the question."""
    x = uuid.uuid4()
    g = merge([[c(x, "bm25", 0, 1.0, "bm25")]])
    rrf(g, weights={"bm25": 1.0})
    once = g.candidates[x].fusion_score
    rrf(g, weights={"bm25": 1.0})
    assert abs(g.candidates[x].fusion_score - once) < 1e-12, "scores accumulated across passes"


def test_fusion_is_independent_of_the_order_surfaces_arrive_in():
    """Surfaces run in whatever order the policy allows, and that order is not a ranking signal."""
    x, y = uuid.uuid4(), uuid.uuid4()
    l1 = [c(x, "bm25", 0, 1.0, "bm25"), c(y, "bm25", 1, 0.9, "bm25")]
    l2 = [c(y, "node_embedding", 0, 0.8, "cosine")]
    a = merge([l1, l2])
    rrf(a, weights={"bm25": 0.9, "node_embedding": 0.7})

    l1b = [c(x, "bm25", 0, 1.0, "bm25"), c(y, "bm25", 1, 0.9, "bm25")]
    l2b = [c(y, "node_embedding", 0, 0.8, "cosine")]
    b = merge([l2b, l1b])
    rrf(b, weights={"bm25": 0.9, "node_embedding": 0.7})

    scores_a = {str(k): round(v.fusion_score, 9) for k, v in a.candidates.items()}
    scores_b = {str(k): round(v.fusion_score, 9) for k, v in b.candidates.items()}
    assert scores_a and scores_a == scores_b


def test_ranked_order_follows_the_fused_score():
    ids = [uuid.uuid4() for _ in range(4)]
    g = merge([[c(nid, "bm25", i, 1.0, "bm25")] for i, nid in enumerate(ids)])
    rrf(g, weights={"bm25": 1.0})
    ranked = g.ranked()
    assert [x.node_id for x in ranked] == ids
    assert all(ranked[i].fusion_score >= ranked[i + 1].fusion_score
               for i in range(len(ranked) - 1))
