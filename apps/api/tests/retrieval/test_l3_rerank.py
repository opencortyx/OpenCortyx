"""L3 tests — features, intent-shaped scoring, diversity, abstention.

The failures being guarded against are all measured ones: a single score that anything could
overwrite, an abstain gate reading that same score, and a context window filled with twenty copies
of one facet.
"""
import uuid

from opencortyx.retrieval.contracts import (
    Candidate, Evidence, Intent, Path, PathStep, QueryPlan,
)
from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l3_rerank.features import (
    FEATURE_NAMES, extract,
)
from opencortyx.retrieval.l3_rerank.reranker import (
    diversify, rerank, score_features, should_abstain, weights_for,
)


def mk(text="some text", ntype="Turn", props=None, evidence=(), paths=()):
    c = Candidate(node_id=uuid.uuid4(), node_type=ntype, text=text, properties=props or {})
    for e in evidence:
        c.add(e)
    c.paths = list(paths)
    return c


def path(*scores):
    a = uuid.uuid4(); steps = []; prev = a
    for s in scores:
        nxt = uuid.uuid4()
        steps.append(PathStep(from_id=prev, edge_type="R", to_id=nxt, direction="out",
                              edge_score=s))
        prev = nxt
    from opencortyx.retrieval.l2_graph.beam import score_path
    p = Path(seed_id=a, steps=steps)
    p.score = score_path(p)
    return p


# ── features ─────────────────────────────────────────────────────────────────────────────────

def test_every_always_assessed_feature_is_produced():
    """Reading a missing key as 0.0 is how a signal disappears unnoticed — so every feature that
    is always measured must actually be present.

    The exception is deliberate and narrow: a feature computed for only part of the pool (the
    cost-bounded cross-encoder) is ABSENT rather than zero, and `score_features` leaves its
    weight out of the normalising mass. Otherwise "outside the budget" would read as "scored
    zero", turning a cost bound into a ranking decision.
    """
    from opencortyx.retrieval.l3_rerank.features import (
        _ONLY_WHEN_ASSESSED,
    )
    f = extract(mk())
    assert set(f) == set(FEATURE_NAMES) - _ONLY_WHEN_ASSESSED
    assert all(0.0 <= v <= 1.0 for v in f.values())
    for name in _ONLY_WHEN_ASSESSED:
        assert name not in f, f"{name} was produced as a value when it was never measured"


def test_semantic_never_reads_a_non_cosine_scale():
    """A path score of 0.9 must not present as semantic similarity."""
    c = mk(evidence=[Evidence("graph", 0, 0.9, "path")])
    assert extract(c).get("semantic", 0.0) == 0.0
    c2 = mk(evidence=[Evidence("node_embedding", 0, 0.9, "cosine")])
    assert extract(c2).get("semantic", 0.0) == 0.9


def test_agreement_saturates():
    one = mk(evidence=[Evidence("bm25", 0, 1, "bm25")])
    two = mk(evidence=[Evidence("bm25", 0, 1, "bm25"),
                       Evidence("node_embedding", 0, 0.5, "cosine")])
    four = mk(evidence=[Evidence("bm25", 0, 1, "bm25"),
                        Evidence("node_embedding", 0, 0.5, "cosine"),
                        Evidence("attr_embedding", 0, 0.5, "cosine", "text"),
                        Evidence("fuzzy", 0, 0.5, "ratio")])
    assert extract(one)["agreement"] == 0.0
    assert 0 < extract(two)["agreement"] < extract(four)["agreement"]
    assert extract(four)["agreement"] == 1.0


def test_attr_targeted_fires_only_for_the_requested_field():
    plan = build_plan("What are Joanna's hobbies?")          # answer_type_hint = "hobbies"
    hit = mk(evidence=[Evidence("attr_embedding", 0, 0.8, "cosine", "hobbies")])
    miss = mk(evidence=[Evidence("attr_embedding", 0, 0.8, "cosine", "speaker")])
    assert extract(hit, plan)["attr_targeted"] == 1.0
    # An attribute surface fired and matched the WRONG field: that is a measured 0.0, not an
    # absent measurement — the two must stay distinguishable.
    assert extract(miss, plan)["attr_targeted"] == 0.0


def test_temporal_fit_distinguishes_known_from_unknown_dates():
    plan = build_plan("What did Nate do on 25 May, 2022?")
    inrange = mk(props={"date_time": "2:01 pm on 25 May, 2022"})
    outrange = mk(props={"date_time": "2:01 pm on 25 May, 2021"})
    undated = mk(props={})
    assert extract(inrange, plan)["temporal_fit"] == 1.0
    assert extract(outrange, plan)["temporal_fit"] == 0.0
    # Unknown passes but earns no bonus: absence of evidence is not evidence of fit.
    assert extract(undated, plan)["temporal_fit"] == 0.5


def test_date_field_is_discovered_not_hardcoded():
    plan = build_plan("what happened in 2022")
    odd = mk(props={"event_date": "14 March, 2022"})
    assert extract(odd, plan)["temporal_fit"] == 1.0


def test_path_features_reward_strong_and_short():
    strong_short = mk(paths=[path(0.9)])
    weak_long = mk(paths=[path(0.9, 0.05)])
    a, b = extract(strong_short), extract(weak_long)
    assert a["path_quality"] > b["path_quality"]
    assert a["path_brevity"] > b["path_brevity"]


# ── scoring ──────────────────────────────────────────────────────────────────────────────────

def test_weights_shift_with_intent_not_with_node_type():
    lookup = weights_for(QueryPlan(raw_query="q", intent=Intent.EXACT_LOOKUP))
    hop = weights_for(QueryPlan(raw_query="q", intent=Intent.MULTI_HOP))
    assert lookup["exact"] > hop["exact"]
    assert hop["path_quality"] > lookup["path_quality"]


def test_multi_hop_prefers_path_evidence_over_raw_similarity():
    plan = QueryPlan(raw_query="q", intent=Intent.MULTI_HOP)
    pathy = mk(evidence=[Evidence("graph", 0, 0.8, "path")], paths=[path(0.95)])
    similar = mk(evidence=[Evidence("node_embedding", 0, 0.72, "cosine")])
    out = rerank([pathy, similar], plan)
    assert out[0] is pathy


def test_exact_lookup_prefers_a_literal_match_over_similarity():
    plan = QueryPlan(raw_query="q", intent=Intent.EXACT_LOOKUP)
    literal = mk(evidence=[Evidence("identifier", 0, 1.0, "exact", "REC-890998")])
    fuzzyish = mk(evidence=[Evidence("node_embedding", 0, 0.85, "cosine")])
    assert rerank([fuzzyish, literal], plan)[0] is literal


def test_score_is_normalised_by_weight_mass():
    w = {"semantic": 1.0}
    assert abs(score_features({"semantic": 1.0}, w) - 1.0) < 1e-9
    assert abs(score_features({"semantic": 0.5}, w) - 0.5) < 1e-9
    assert score_features({}, {}) == 0.0


def test_rerank_computes_features_when_absent():
    c = mk(evidence=[Evidence("node_embedding", 0, 0.6, "cosine")])
    assert not c.features
    rerank([c])
    assert c.rerank_score is not None and "semantic" in c.features


# ── diversity ────────────────────────────────────────────────────────────────────────────────

def test_diversify_breaks_up_near_duplicates():
    """Twenty copies of one facet is individually correct and collectively useless."""
    dupes = [mk(text="Joanna is allergic to reptiles and animals with fur") for _ in range(5)]
    other = mk(text="Nate dyed his hair purple last week")
    for c in dupes:
        c.rerank_score = 0.9
    other.rerank_score = 0.6
    picked = diversify(dupes + [other], limit=3)
    assert other in picked, "a distinct facet was crowded out by duplicates"


def test_diversify_penalises_shared_DERIVATION_even_when_wording_differs():
    """Two nodes written FROM the same source are the same evidence twice, however differently
    they are phrased."""
    src = "turn-abc"
    a = mk(text="completely different wording here", props={"source_ref": src})
    b = mk(text="nothing lexically in common at all", props={"source_ref": src})
    c = mk(text="unrelated third statement entirely", props={"source_ref": "turn-xyz"})
    for x, s in ((a, 0.9), (b, 0.88), (c, 0.62)):
        x.rerank_score = s
    picked = diversify([a, b, c], limit=2)
    assert c in picked, "same-derivation evidence counted twice"


def test_shared_container_is_only_a_light_nudge():
    """Two turns of one conversation are routinely DIFFERENT evidence. Collapsing by parent is
    what the previous engine did, and it discarded distinct material."""
    a = mk(text="joanna adopted a dog from the shelter", props={"session": "session_7"})
    b = mk(text="nate won a video game tournament", props={"session": "session_7"})
    for x, s in ((a, 0.9), (b, 0.86)):
        x.rerank_score = s
    picked = diversify([a, b], limit=2)
    assert len(picked) == 2 and a in picked and b in picked


def test_diversify_is_a_noop_without_a_limit():
    xs = [mk() for _ in range(4)]
    for x in xs:
        x.rerank_score = 0.5
    assert diversify(xs, limit=0) == xs


# ── abstention ───────────────────────────────────────────────────────────────────────────────

def test_abstain_reads_semantic_only():
    """A strong graph score must never keep a groundless answer alive, and must never suppress
    a grounded one — the two directions of the same measured bug."""
    graphy = mk(evidence=[Evidence("graph", 0, 0.99, "path")])
    rerank([graphy])
    assert should_abstain([graphy]) is True

    grounded = mk(evidence=[Evidence("node_embedding", 0, 0.71, "cosine")])
    rerank([grounded])
    assert should_abstain([grounded]) is False


def test_abstain_on_empty_input():
    assert should_abstain([]) is True
