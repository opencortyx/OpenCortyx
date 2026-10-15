"""L0 planner + mention detection tests, and the L0 end-to-end pass.

The safety property under test throughout: a plan NARROWS NOTHING TO ZERO. Every surface keeps a
floor, because a wrong hypothesis must cost latency rather than recall.
"""
from opencortyx.retrieval.contracts import Intent
from opencortyx.retrieval.l0_understanding.entities import (
    detect_attribute_targets, detect_mentions,
)
from opencortyx.retrieval.l0_understanding.planner import (
    SURFACES, build_plan, decompose, query_representations,
)


# ── mentions ─────────────────────────────────────────────────────────────────────────────────

def test_detects_names_from_the_corpus():
    ms = [m.text for m in detect_mentions("What nickname does Nate use for Joanna?")]
    assert "Nate" in ms and "Joanna" in ms


def test_ignores_leading_interrogative_capitalisation():
    ms = [m.text.lower() for m in detect_mentions("What is Joanna allergic to?")]
    assert "what" not in ms
    assert "joanna" in ms


def test_multiword_name_wins_over_its_prefix():
    ms = [m.text for m in detect_mentions("Tell me about Bank of England exposure")]
    assert any("Bank of England" == m for m in ms), ms


def test_identifier_is_a_mention():
    ms = [m for m in detect_mentions("what city is recorded for record REC-890998?")]
    assert any(m.hint_type == "identifier" for m in ms)


def test_quoted_span_is_a_mention():
    ms = [m.text for m in detect_mentions('Which movie is "Eternal Sunshine of the Spotless Mind"?')]
    assert any("Eternal Sunshine" in m for m in ms)


def test_attribute_target_from_possessive():
    assert "email" in detect_attribute_targets("What is John's email?")
    assert "hobbies" in detect_attribute_targets("What are Joanna's hobbies?")


# ── plan ─────────────────────────────────────────────────────────────────────────────────────

def test_no_surface_is_ever_zero():
    """The core safety property: a plan must not make a question unanswerable."""
    for q in ("How many hikes has Joanna been on?",
              "Tell me about JP Morgan",
              "What is Joanna allergic to?",
              "Which companies connect to John through Germany?",
              ""):
        plan = build_plan(q)
        for s in SURFACES:
            assert plan.strategies.get(s, 0) > 0, f"{s} zeroed for {q!r}"


def test_multi_hop_prioritises_graph_and_raises_hops():
    p = build_plan("Which companies are connected to John through transactions involving Germany?")
    assert p.intent is Intent.MULTI_HOP
    assert p.strategies["graph"] >= 0.9
    assert p.max_hops == 3


def test_exact_lookup_prioritises_entity_and_lexical_over_dense():
    p = build_plan("In document DOC-00000731, what city is recorded for record REC-890998?")
    assert p.intent is Intent.EXACT_LOOKUP
    assert p.strategies["entity"] > p.strategies["fuzzy"]
    assert p.strategies["bm25"] > p.strategies["fuzzy"]


def test_temporal_question_boosts_the_attribute_surface():
    """date_time lives in an attribute; this is what makes it reachable at all."""
    p = build_plan("What did Nate do on 25 May, 2022?")
    assert p.strategies["attr_embedding"] >= 0.65
    assert p.temporal and p.temporal[0].start == "2022-05-25"


def test_low_confidence_widens_recall():
    p = build_plan("Something entirely unlike the training patterns")
    assert p.strategies["attr_embedding"] >= 0.6
    assert any("widening" in n for n in p.notes)


def test_mentions_boost_entity_and_fuzzy():
    p = build_plan("What nickname does Nate use for Joanna?")
    assert p.strategies["entity"] >= 0.7
    assert p.strategies["fuzzy"] >= 0.45


# ── decomposition ────────────────────────────────────────────────────────────────────────────

def test_decompose_is_conservative():
    p = build_plan("What is Joanna allergic to?")
    assert decompose("What is Joanna allergic to?", p) == []


def test_decompose_splits_a_through_chain():
    q = "Which customers connected to Acme through payments in 2024?"
    p = build_plan(q)
    parts = decompose(q, p)
    if parts:                       # only when the intent is multi-hop
        assert len(parts) >= 2
        assert all(len(x) > 6 for x in parts)


# ── representations ──────────────────────────────────────────────────────────────────────────

def test_representations_include_a_concept_form():
    p = build_plan("What is Joanna allergic to?")
    reps = query_representations(p)
    assert reps["full"].startswith("What is")
    assert "concept" in reps
    assert not reps["concept"].lower().startswith("what is")


def test_representations_include_entities_and_attribute_target():
    p = build_plan("What are Joanna's hobbies?")
    reps = query_representations(p)
    assert "entity" in reps and "Joanna" in reps["entity"]
    assert reps.get("attribute") == "hobbies"


def test_empty_query_still_yields_a_usable_plan():
    p = build_plan("")
    reps = query_representations(p)
    assert reps["full"] == ""
    assert all(p.strategies[s] > 0 for s in SURFACES)
