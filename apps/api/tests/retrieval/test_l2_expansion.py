"""L2 — graph expansion awareness: query-directed, temporal, node, attribute, edge.

Expansion scored a route by the product of its EDGE relevances and nothing else. Three of the
five dimensions the architecture calls for were therefore absent, and each is tested here on its
own before the pipeline check at the bottom:

  temporal   — time was parsed at L0 and applied only in `MetadataSurface` and as an L3 feature.
               Traversal walked straight through out-of-range nodes and spent fanout on them.
  node       — a hop to the answering statement and a hop to an unrelated node scored the same,
               provided both used the same relation. The beam's pruning could not tell the routes
               it was choosing between apart.
  attribute  — nothing read a node's fields, so "what are Joanna's hobbies" could not prefer
               walking to a node that actually carries a `hobbies` field.

The two that already worked (query-directed, edge-aware) are covered in `test_l2_graph.py`.
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.contracts import (
    Path, PathStep, TemporalConstraint,
)
from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l2_graph.beam import score_path
from opencortyx.retrieval.l2_graph.expansion import (
    NODE_FLOOR, ExpansionContext, content_terms, node_date,
)


def step(edge_score=0.9, node_score=None):
    return PathStep(from_id=uuid.uuid4(), edge_type="MENTIONS", to_id=uuid.uuid4(),
                    direction="out", edge_score=edge_score, node_score=node_score)


# ── reading the question ─────────────────────────────────────────────────────────────────────

def test_content_terms_keep_what_the_question_is_about():
    t = content_terms("What is Joanna allergic to?")
    assert "joanna" in t
    assert any(x.startswith("allerg") for x in t), t
    assert "what" not in t and "is" not in t and "to" not in t


def test_terms_are_stems_so_an_adjective_reaches_a_noun():
    """"allergic" in the question must reach "allergies" on a node."""
    assert content_terms("allergic") == content_terms("allergies")


def test_a_context_from_a_plain_question_still_reads_its_words():
    ctx = ExpansionContext.from_plan(build_plan("What is Joanna allergic to?"))
    assert ctx.terms and ctx.active
    assert not ctx.temporal


def test_an_empty_plan_makes_an_inactive_context():
    ctx = ExpansionContext.from_plan(None)
    assert not ctx.active
    assert ctx.accepts({"date_time": "anything"}) is True
    assert ctx.affinity("x", {}) == 1.0, "an inactive context must not dampen anything"


# ── temporal awareness ───────────────────────────────────────────────────────────────────────

def test_a_node_outside_the_bound_is_rejected():
    ctx = ExpansionContext(temporal=(TemporalConstraint("after", start="2023-01-01"),))
    assert ctx.accepts({"date_time": "2:01 pm on 23 January, 2024"}) is True
    assert ctx.accepts({"date_time": "2:01 pm on 23 January, 2021"}) is False


def test_an_unreadable_date_passes():
    """The filter removes what is known to be out of range, never what we failed to parse.

    Rejecting on an unparseable date deletes evidence whenever a corpus spells its dates
    unusually — and this corpus spells them "2:01 pm on 23 January, 2022".
    """
    ctx = ExpansionContext(temporal=(TemporalConstraint("after", start="2023-01-01"),))
    assert ctx.accepts({"date_time": "sometime last spring"}) is True
    assert ctx.accepts({"text": "no date at all"}) is True
    assert ctx.accepts({}) is True
    assert ctx.accepts(None) is True


def test_no_constraint_accepts_everything():
    ctx = ExpansionContext(terms=frozenset({"x"}))
    assert ctx.accepts({"date_time": "1999-01-01"}) is True


def test_a_date_is_found_under_whichever_key_carries_it():
    assert node_date({"date_time": "23 January, 2022"}) == "2022-01-23"
    assert node_date({"occurred_at": "2024-05-01"}) == "2024-05-01"
    assert node_date({"unrelated": "hello"}) is None
    assert node_date({}) is None


def test_every_bound_must_be_satisfied_not_merely_one():
    ctx = ExpansionContext(temporal=(
        TemporalConstraint("after", start="2023-01-01"),
        TemporalConstraint("before", end="2023-06-30"),
    ))
    assert ctx.accepts({"date_time": "2023-03-01"}) is True
    assert ctx.accepts({"date_time": "2023-09-01"}) is False


# ── node awareness ───────────────────────────────────────────────────────────────────────────

def test_a_node_repeating_the_question_scores_above_one_that_does_not():
    ctx = ExpansionContext(terms=frozenset(content_terms("Joanna allergic reptiles")))
    strong = ctx.affinity("", {"text": "Joanna has severe allergies to reptiles"})
    weak = ctx.affinity("", {"text": "the weather was pleasant on Tuesday"})
    assert strong > weak
    assert 0.0 <= weak <= strong <= 1.0


def test_affinity_is_normalised_by_the_question_not_by_the_node():
    """A long node must not become relevant by containing many words, only by containing the
    question's words."""
    ctx = ExpansionContext(terms=frozenset(content_terms("allergic")))
    short = ctx.affinity("", {"text": "allergies"})
    padded = ctx.affinity("", {"text": "allergies " + "filler words here " * 40})
    assert short == padded


def test_a_node_sharing_nothing_is_dampened_never_deleted():
    """The answer to "what is she allergic to" may be "reptiles and anything with fur", which
    repeats none of the question's content words. A gate here would delete exactly that."""
    ctx = ExpansionContext(terms=frozenset(content_terms("allergic")))
    p = Path(steps=[step(edge_score=0.9, node_score=ctx.affinity("", {"text": "unrelated"}))])
    assert score_path(p) > 0, "a lexically dissimilar node was scored out of existence"
    assert score_path(p) >= 0.9 * NODE_FLOOR - 1e-9


# ── attribute awareness ──────────────────────────────────────────────────────────────────────

def test_a_node_carrying_the_field_asked_for_is_preferred():
    """"What are Joanna's hobbies" should prefer a node that HAS a hobbies field. That is a fact
    about the node's payload, never about its type."""
    plan = build_plan("What are Joanna's hobbies?")
    assert plan.answer_type_hint == "hobbies"
    ctx = ExpansionContext.from_plan(plan)
    assert ctx.attribute_targets

    carries = ctx.affinity("", {"hobbies": "cycling and baking"})
    lacks = ctx.affinity("", {"unrelated_field": "cycling and baking"})
    assert carries > lacks
    assert carries == 1.0


def test_the_field_is_matched_as_text_not_looked_up():
    """A plural in the question must reach a singular field name, and vice versa."""
    ctx = ExpansionContext(attribute_targets=frozenset({"hobb"}))
    assert ctx.affinity("", {"hobby": "x"}) == 1.0
    assert ctx.affinity("", {"hobbies": "x"}) == 1.0


def test_signals_are_taken_at_their_maximum_not_summed():
    """A node must not become confident by scoring weakly on two independent signals."""
    ctx = ExpansionContext(terms=frozenset({"joanna"}),
                           attribute_targets=frozenset({"hobb"}))
    both = ctx.affinity("Joanna", {"hobbies": "x", "text": "Joanna"})
    assert both <= 1.0


# ── how awareness enters the path score ──────────────────────────────────────────────────────

def test_an_unassessed_node_contributes_no_term_at_all():
    """`None` means NOT ASSESSED and must not be silently read as a neutral or maximal value.

    "Unknown defaults to maximum" is how an unregistered surface came to outvote every planned
    one; the same mistake here would make unassessed routes the strongest.
    """
    assessed = Path(steps=[step(edge_score=0.9, node_score=1.0)])
    unassessed = Path(steps=[step(edge_score=0.9, node_score=None)])
    assert score_path(unassessed) == pytest.approx(0.9)
    assert score_path(assessed) == pytest.approx(0.9)     # affinity 1.0 -> full node term
    poor = Path(steps=[step(edge_score=0.9, node_score=0.0)])
    assert score_path(poor) < score_path(unassessed)


def test_the_node_term_orders_routes_that_edges_alone_cannot_separate():
    """The whole point: two hops over the SAME relation, distinguished only by where they land."""
    good = Path(steps=[step(edge_score=0.8, node_score=0.9)])
    bad = Path(steps=[step(edge_score=0.8, node_score=0.05)])
    assert score_path(good) > score_path(bad)


def test_a_longer_route_to_the_same_evidence_still_scores_lower():
    one = Path(steps=[step(edge_score=0.9, node_score=0.8)])
    two = Path(steps=[step(edge_score=0.9, node_score=0.8), step(edge_score=0.9, node_score=0.8)])
    assert score_path(one) > score_path(two)


def test_expansion_consults_no_node_type_anywhere():
    """Rule 1, as a property. Affinity is computed from the node's TEXT and FIELD NAMES; the
    type string is never read, so a corpus naming its types differently behaves identically."""
    ctx = ExpansionContext(terms=frozenset({"joanna"}))
    props = {"text": "Joanna spoke"}
    assert ctx.affinity("", props) == ctx.affinity("", dict(props, type="Zorblat"))


# ── against the real graph ───────────────────────────────────────────────────────────────────

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


async def _run(query: str):
    from opencortyx.core.db import V2ReadSession
    from opencortyx.retrieval.orchestrator import (
        RetrievalWorkflow, WorkflowConfig,
    )
    wf = RetrievalWorkflow(_embed, _embed_batch,
                           WorkflowConfig(candidates_per_surface=40, budget_tokens=4000))
    async with V2ReadSession() as db:
        return await wf.run(db, ORG, query)


@pytest.mark.integration
@pytest.mark.anyio
async def test_out_of_range_nodes_are_rejected_during_traversal():
    """Measured: 247 nodes rejected mid-walk on this question. Before, every one of them was
    walked to and consumed the fanout budget."""
    r = await _run("Tell me about Joanna after 2023")
    beam = [t for t in r.trace if t.stage == "beam"]
    if not beam:
        pytest.skip("graph expansion did not run for this org")
    rejected = beam[0].extra.get("out_of_range", 0)
    assert rejected > 0, "a bound of 'after 2023' rejected nothing on a corpus spanning 2022-2023"
    assert "temporal" in beam[0].detail


@pytest.mark.integration
@pytest.mark.anyio
async def test_nothing_the_walk_returns_violates_the_time_bound():
    """The claim the rejection makes, checked on what actually came back."""
    from opencortyx.retrieval.l2_graph.expansion import node_date
    from opencortyx.retrieval.l0_understanding.constraints import within
    r = await _run("Tell me about Joanna after 2023")
    if not r.graph.candidates or not r.plan.temporal:
        pytest.skip("no expansion or no constraint")

    violations = []
    for cand in r.graph.candidates.values():
        if not any(e.surface == "graph" for e in cand.evidence):
            continue                      # only nodes the WALK produced are governed by this
        iso = node_date(cand.properties)
        if iso and not all(within(c, iso) for c in r.plan.temporal):
            violations.append((str(cand.node_id), iso))
    assert not violations, f"traversal returned out-of-range nodes: {violations[:3]}"


@pytest.mark.integration
@pytest.mark.anyio
async def test_the_trace_names_which_awareness_applied():
    """A dimension that fired silently cannot be told from one that is not implemented."""
    r = await _run("What are Joanna's hobbies?")
    beam = [t for t in r.trace if t.stage == "beam"]
    if not beam:
        pytest.skip("graph expansion did not run")
    detail = beam[0].detail
    assert "aware=" in detail
    assert "node(" in detail and "attr(" in detail, detail


@pytest.mark.integration
@pytest.mark.anyio
async def test_every_walked_step_records_the_node_it_landed_on():
    r = await _run("What is Joanna allergic to?")
    steps = [s for c in r.graph.candidates.values() for p in c.paths for s in p.steps]
    if not steps:
        pytest.skip("graph expansion did not run")
    assessed = [s for s in steps if s.node_score is not None]
    assert assessed, "no step recorded a node score on a question with content words"
    assert all(0.0 <= s.node_score <= 1.0 for s in assessed)
    # Edge and node relevance stay SEPARATE, so neither can be mistaken for the other.
    assert any(s.edge_score != s.node_score for s in assessed)
