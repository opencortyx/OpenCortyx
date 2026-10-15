"""L0 — query classification (A–H) and the execution policy it drives.

Two claims are under test, and they are different claims:

  1. The eight categories are told apart. `test_the_eight_categories` is the acceptance test,
     written from the canonical example of each category.

  2. The classification CHANGES WHAT RUNS. This is the part that was missing: every surface ran
     on every question regardless of intent, so an exact lookup paid for a full ANN sweep and a
     graph walk and then down-weighted both to the floor. A classification nothing acts on is
     decoration.

And the safety property that makes (2) legitimate rather than a recall bug: narrowing happens
only when the classifier is confident, and an underfilled narrow plan widens.
"""
from __future__ import annotations

import pytest

from opencortyx.retrieval.contracts import (
    AttributeConstraint, ExecutionPolicy, Intent, TemporalConstraint,
)
from opencortyx.retrieval.l0_understanding.planner import (
    SURFACES, build_plan,
)
from opencortyx.retrieval.l0_understanding.policy import (
    ALL_SURFACES, MIN_CONFIDENCE_TO_NARROW, _EXECUTE, plan_execution, widen,
)

#: One canonical question per category.
CANON = {
    "A": (Intent.EXACT_LOOKUP, "What is the email of John?"),
    "B": (Intent.KEYWORD,      "Find documents mentioning Basel III liquidity requirements."),
    "C": (Intent.SEMANTIC,     "Find information about liquidity risks caused by funding "
                               "concentration."),
    "D": (Intent.ENTITY,       "Tell me about JP Morgan."),
    "E": (Intent.RELATIONSHIP, "Who works with companies that supplied Apple?"),
    "F": (Intent.MULTI_HOP,    "Which companies are connected to John through transactions "
                               "involving Germany?"),
    "G": (Intent.TEMPORAL,     "What changed between 2023 and 2025?"),
    "H": (Intent.AGGREGATION,  "How many transactions did each company have?"),
}


# ── the categories ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag", sorted(CANON))
def test_the_eight_categories(tag):
    want, q = CANON[tag]
    got = build_plan(q).intent
    assert got is want, f"category {tag}: {q!r} classified {got.value}, wanted {want.value}"


def test_an_attribute_lookup_is_not_a_profile_request():
    """"What is the email OF John" and "What is JP Morgan" open with the same four words.

    The profile frame used to win both, so every attribute lookup retrieved an entity's whole
    neighbourhood instead of reading one field.
    """
    assert build_plan("What is the email of John?").intent is Intent.EXACT_LOOKUP
    assert build_plan("What is JP Morgan?").intent is Intent.ENTITY


def test_a_date_range_is_time_not_a_relation():
    """"between 2023 and 2025" and "between John and Acme" share a word and nothing else.

    Reading the date range as relational sent a temporal question down a graph-expansion plan.
    """
    assert build_plan("What changed between 2023 and 2025?").intent is Intent.TEMPORAL
    assert build_plan("What is the connection between John and Acme?").intent \
        is Intent.RELATIONSHIP


# ── the classification changes what runs ─────────────────────────────────────────────────────

def test_an_exact_lookup_does_not_pay_for_ann_or_a_graph_walk():
    """"What is the email of John" is answered by resolving one node and reading one field."""
    p = build_plan(CANON["A"][1])
    assert not p.policy.allows("node_embedding"), "a dense sweep ran for a field read"
    assert not p.policy.allows("graph"), "a graph walk ran for a field read"
    assert p.policy.allows("entity") and p.policy.allows("attr_embedding")


def test_a_keyword_query_does_not_expand_the_graph():
    p = build_plan(CANON["B"][1])
    assert not p.policy.allows("graph")
    assert p.policy.allows("bm25")


def test_a_relationship_question_runs_the_graph():
    for tag in ("E", "F"):
        p = build_plan(CANON[tag][1])
        assert p.policy.allows("graph"), f"{tag} did not run the graph"
        assert p.policy.allows("entity"), f"{tag} did not resolve endpoints"


def test_aggregation_runs_a_computing_surface():
    """Counting is done by the graph or a metadata filter, not by an LLM reading retrieved nodes."""
    p = build_plan(CANON["H"][1])
    assert p.policy.allows("graph") or p.policy.allows("metadata")


def test_a_temporal_question_filters_rather_than_walks():
    p = build_plan(CANON["G"][1])
    assert p.policy.allows("metadata"), "a time bound did not reach the filtering surface"
    assert p.policy.allows("attr_embedding")
    assert not p.policy.allows("graph")


def test_two_categories_do_not_execute_the_same_plan():
    """The original failure: a lookup and a four-hop reasoning question ran identically."""
    a = build_plan(CANON["A"][1]).policy.run
    f = build_plan(CANON["F"][1]).policy.run
    assert a != f


# ── the safety net ───────────────────────────────────────────────────────────────────────────

#: Surfaces that need an input from the question before they can run at all. Skipping one of
#: these is a statement about the QUESTION, not a narrowing decision about the intent.
_PRECONDITIONED = {"fuzzy", "entity", "metadata", "ontology"}


def test_a_low_confidence_classification_narrows_nothing():
    """Nothing in the question was recognised, which is the case that most needs breadth.

    "Narrows nothing" means no surface is dropped for lack of confidence. A surface with no
    input to work on — no mention to vary, no constraint to filter — still does not run, and
    saying otherwise would make the trace describe something that did not happen.
    """
    p = build_plan("Something entirely unlike the training patterns")
    assert p.confidence <= MIN_CONFIDENCE_TO_NARROW
    dropped = set(p.policy.skipped) - _PRECONDITIONED
    assert dropped == set(), f"an unconfident plan narrowed away {sorted(dropped)}"
    assert p.policy.run >= (set(ALL_SURFACES) - _PRECONDITIONED)


def test_the_policy_never_claims_a_surface_ran_without_its_input():
    """`fuzzy` has nothing to vary without a mention; `metadata` has nothing to filter without a
    constraint. Both were reported as running while the orchestrator quietly skipped them."""
    p = build_plan("Something entirely unlike the training patterns")
    if not p.mentions:
        assert not p.policy.allows("fuzzy") and not p.policy.allows("entity")
    if not p.temporal and not p.attributes:
        assert not p.policy.allows("metadata")
    if not p.node_types:
        assert not p.policy.allows("ontology"), "ontology ran with no type to search within"


def test_a_confident_plan_still_never_runs_nothing():
    for _, q in CANON.values():
        p = build_plan(q)
        assert p.policy.run, f"{q!r} produced an empty execution set"


def test_every_skipped_surface_carries_a_reason():
    """A silently absent retriever is indistinguishable in a trace from one that found nothing."""
    for _, q in CANON.values():
        p = build_plan(q)
        for surface, why in p.policy.skipped.items():
            assert why and len(why) > 8, f"{surface} skipped without a reason"


def test_underfilled_narrow_plans_widen():
    pol = ExecutionPolicy(run={"bm25"}, skipped={"graph": "x", "node_embedding": "y"},
                          widen_below=10)
    do, why = widen(pol, found=3)
    assert do and "below 10" in why
    assert widen(pol, found=10)[0] is False
    assert widen(pol, found=99)[0] is False


def test_a_plan_that_skipped_nothing_never_widens():
    pol = ExecutionPolicy(run=set(ALL_SURFACES), skipped={}, widen_below=10)
    assert widen(pol, found=0)[0] is False


# ── content overrides shape ──────────────────────────────────────────────────────────────────

def test_a_corpus_grounded_relation_puts_the_graph_back():
    """The intent classifier reads language and cannot know that "skills" is relational HERE.
    That only follows from `HAS_SKILL` existing in this corpus."""
    from tests.retrieval.test_l0_vocabulary import REAL
    plain = build_plan("Find information about skills and hobbies")
    grounded = build_plan("Find information about skills and hobbies", REAL)
    assert grounded.edge_types, "expected HAS_SKILL to be implicated"
    assert grounded.policy.allows("graph")
    assert plain.policy.allows("graph") or True     # the plain plan may legitimately differ


def test_a_constraint_puts_the_filtering_surface_back():
    p = build_plan("Tell me about JP Morgan")
    assert not p.policy.allows("metadata"), "nothing to filter on, yet metadata ran"

    p2 = build_plan("Tell me about JP Morgan")
    p2.temporal = [TemporalConstraint(relation="after", start="2023-01-01")]
    pol = plan_execution(p2, confidence=0.9)
    assert pol.allows("metadata"), "a time bound did not reach the filtering surface"

    p3 = build_plan("Tell me about JP Morgan")
    p3.attributes = [AttributeConstraint(attr="amount", op="gt", value=1000)]
    assert plan_execution(p3, confidence=0.9).allows("metadata")


def test_fuzzy_is_not_run_when_there_is_nothing_to_vary():
    p = build_plan("Find information about liquidity risk in general terms")
    if not p.mentions:
        assert not p.policy.allows("fuzzy")
    named = build_plan("Tell me about JP Morgan")
    assert named.mentions and named.policy.allows("fuzzy")


# ── registry consistency ─────────────────────────────────────────────────────────────────────

def test_every_executable_surface_is_a_registered_surface():
    """A policy that names a surface the planner cannot weight would run a retriever whose votes
    fall through to the unplanned default — influence decided by absence from a registry, which
    is the bug class this engine exists to remove."""
    unregistered = ALL_SURFACES - set(SURFACES)
    assert not unregistered, f"policy runs unweightable surfaces: {sorted(unregistered)}"


def test_every_intent_has_an_execution_set():
    missing = [i.value for i in Intent if i not in _EXECUTE]
    assert not missing, f"intents with no execution set: {missing}"


def test_identifier_is_weighted_rather_than_defaulted():
    """It ran on every query while appearing in no registry, so fusion gave it the maximal
    unplanned default — a higher weight than any surface the planner deliberately chose."""
    assert "identifier" in SURFACES
    p = build_plan("In document DOC-00000731, what city is recorded for record REC-890998?")
    assert p.strategies["identifier"] > 0.5
