"""Unit tests for the shared data model.

These are cheap and they matter: every layer above depends on evidence accumulating rather than
overwriting, and on scores never being mixed across scales. Both were real failures in the
previous engine — a graph-walk score silently replaced a cosine and buried a rank-4 node at
rank 1328.
"""
import uuid

from opencortyx.retrieval.contracts import (
    Candidate, CandidateGraph, Evidence, Intent, Path, PathStep, QueryPlan,
)


def _cand(**kw):
    return Candidate(node_id=kw.pop("node_id", uuid.uuid4()), **kw)


def test_evidence_accumulates_across_surfaces():
    c = _cand()
    c.add(Evidence(surface="node_embedding", rank=3, raw_score=0.79, scale="cosine"))
    c.add(Evidence(surface="bm25", rank=12, raw_score=14.2, scale="bm25"))
    c.add(Evidence(surface="attr_embedding", rank=1, raw_score=0.94, scale="cosine",
                   detail="description"))
    assert c.surfaces == {"node_embedding", "bm25", "attr_embedding"}
    assert len(c.evidence) == 3


def test_best_never_mixes_scales():
    """A bm25 score of 14.2 must never be returned as if it were a cosine."""
    c = _cand()
    c.add(Evidence(surface="bm25", rank=1, raw_score=14.2, scale="bm25"))
    c.add(Evidence(surface="node_embedding", rank=9, raw_score=0.62, scale="cosine"))
    assert c.best("cosine") == 0.62
    assert c.best("bm25") == 14.2
    assert c.best("ppr") == 0.0          # absent scale is zero, not the max of something else


def test_best_rank_is_min_and_surface_filterable():
    c = _cand()
    c.add(Evidence(surface="bm25", rank=88, raw_score=1.0, scale="bm25"))
    c.add(Evidence(surface="node_embedding", rank=4, raw_score=0.8, scale="cosine"))
    assert c.best_rank() == 4
    assert c.best_rank("bm25") == 88
    assert c.best_rank("fuzzy") is None


def test_final_score_prefers_rerank_when_present():
    c = _cand()
    c.fusion_score = 0.4
    assert c.final_score == 0.4
    c.rerank_score = 0.9
    assert c.final_score == 0.9
    c.rerank_score = 0.0                  # zero is a decision, not "unset"
    assert c.final_score == 0.0


def test_graph_upsert_merges_rather_than_replaces():
    """The failure this prevents: a second surface finding the same node and overwriting the
    first surface's evidence, so a node found three ways looks like a node found once."""
    g = CandidateGraph()
    nid = uuid.uuid4()
    a = Candidate(node_id=nid, text="hello", node_type="Turn")
    a.add(Evidence(surface="bm25", rank=2, raw_score=9.0, scale="bm25"))
    g.upsert(a)

    b = Candidate(node_id=nid, text="", node_type="")
    b.add(Evidence(surface="node_embedding", rank=5, raw_score=0.71, scale="cosine"))
    g.upsert(b)

    merged = g.candidates[nid]
    assert len(g) == 1
    assert merged.surfaces == {"bm25", "node_embedding"}
    assert merged.text == "hello"          # non-empty original preserved
    assert merged.node_type == "Turn"


def test_path_length_terminal_and_tuple():
    seed, mid, end = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    p = Path(seed_id=seed, steps=[
        PathStep(from_id=seed, edge_type="MENTIONS", to_id=mid, direction="out", edge_score=0.8),
        PathStep(from_id=mid, edge_type="EXTRACTED_FROM", to_id=end, direction="out",
                 edge_score=0.9),
    ])
    assert p.length == 2
    assert p.terminal_id == end
    t = p.as_tuple()
    assert t[0] == str(seed) and t[-1] == str(end)
    assert "MENTIONS" in t and "EXTRACTED_FROM" in t


def test_empty_path_terminal_is_the_seed():
    seed = uuid.uuid4()
    assert Path(seed_id=seed).terminal_id == seed


def test_plan_wants_is_threshold_aware():
    p = QueryPlan(raw_query="q", intent=Intent.MULTI_HOP,
                  strategies={"graph": 0.9, "bm25": 0.0, "fuzzy": 0.05})
    assert p.wants("graph")
    assert not p.wants("bm25")
    assert p.wants("fuzzy")                      # present at all
    assert not p.wants("fuzzy", threshold=0.1)   # but below a bar
    assert not p.wants("never_registered")


def test_ranked_orders_by_final_score():
    g = CandidateGraph()
    for s in (0.2, 0.9, 0.5):
        c = _cand()
        c.fusion_score = s
        g.upsert(c)
    scores = [c.final_score for c in g.ranked()]
    assert scores == sorted(scores, reverse=True)
