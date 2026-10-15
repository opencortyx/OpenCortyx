"""Category H — aggregation is COMPUTED, not retrieved and counted by a model.

`Intent.AGGREGATION` was detected for a long time before anything acted on it: the question was
labelled a count and then sent down the same stuffing path as everything else. Retrieving the top
N nodes and asking a model to count them yields a confident number wrong by however much the
budget truncated, and nothing downstream can detect that — from the outside it looks exactly like
a successful retrieval.

The tests below cover the computation and, at least as importantly, the two ways it must REFUSE:

  - the corpus has no such type          -> fall back to retrieval  (never "0")
  - the types exist but nothing joins them -> fall back to retrieval  (never "0")
"""
from __future__ import annotations

import uuid

import pytest

from opencortyx.retrieval.l0_understanding.planner import build_plan
from opencortyx.retrieval.l2_graph import aggregate as A
from tests.retrieval.test_l0_vocabulary import REAL

ORG = uuid.UUID("e5954895-7b42-5a9b-93f6-911febc30bca")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def spec_for(q: str, v=REAL):
    return A.parse(build_plan(q, v), v)


# ── reading the computation off the question ─────────────────────────────────────────────────

def test_a_non_aggregation_question_produces_no_spec():
    assert A.parse(build_plan("Tell me about JP Morgan", REAL), REAL) is None
    assert A.parse(build_plan("What is the email of John?", REAL), REAL) is None


def test_target_and_group_are_read_from_the_question():
    s = spec_for("How many turns did each session have?")
    assert s.function == "count"
    assert s.target_type == "Turn" and s.group_type == "Session"
    assert s.grounded


def test_a_scalar_count_has_no_group():
    s = spec_for("How many turns are there?")
    assert s.grounded and s.target_type == "Turn" and s.group_type == ""


def test_per_and_each_and_by_all_name_the_group():
    for phrasing in ("How many turns per session?",
                     "How many turns for each session?",
                     "Count the turns by session"):
        s = spec_for(phrasing)
        assert s and s.group_type == "Session", f"{phrasing!r} -> {s and s.group_type!r}"


def test_the_function_is_read_not_assumed():
    assert spec_for("What is the total of turns per session?").function == "sum"
    assert spec_for("What is the average turns per session?").function == "avg"


# ── refusing, which is the important half ────────────────────────────────────────────────────

def test_a_type_the_corpus_does_not_have_is_not_aggregated():
    """"How many transactions per company" against a conversation graph grounds nothing.

    It must fall back to ordinary retrieval. Returning 0 would be a measured-looking answer to a
    question this corpus cannot address.
    """
    s = spec_for("How many transactions did each company have?")
    assert s is not None and not s.grounded
    assert "transaction" in s.why


def test_an_ungrounded_group_also_refuses():
    s = spec_for("How many turns did each transaction have?")
    assert s is not None and not s.grounded
    assert "transaction" in s.why


def test_a_countable_noun_is_required():
    s = spec_for("How many are there?")
    assert s is not None and not s.grounded


@pytest.mark.anyio
async def test_an_ungrounded_spec_executes_to_nothing_rather_than_zero():
    res = await A.execute(ORG, A.AggregationSpec(function="count", grounded=False))
    assert res.scalar is None and res.rows == []
    assert not res.usable, "an ungrounded spec produced a usable result"


# ── the shape of a result ────────────────────────────────────────────────────────────────────

def test_truncation_reports_that_everything_was_counted():
    """The listing is capped; the COUNT is not. A cost bound on output must never read as a
    smaller answer."""
    s = A.AggregationSpec(function="count", target_type="Turn", group_type="Session",
                          grounded=True)
    res = A.AggregationResult(spec=s, groups_total=272, truncated=True,
                              rows=[A.AggregationRow(key=f"s{i}", value=i) for i in range(50)])
    text = res.render()
    assert "272" in text
    assert "222 more not listed" in text and "all 272 were counted" in text


def test_a_scalar_renders_as_one_number():
    s = A.AggregationSpec(function="count", target_type="Turn", grounded=True)
    assert A.AggregationResult(spec=s, scalar=5882).render() == "count of Turn: 5882"


def test_an_inconclusive_result_is_not_usable():
    s = A.AggregationSpec(function="count", target_type="Fact", group_type="Session",
                          grounded=True)
    res = A.AggregationResult(spec=s, groups_total=0, inconclusive=True, note="nothing joins them")
    assert not res.usable


# ── against the real graph ───────────────────────────────────────────────────────────────────

async def _vocab():
    from opencortyx.retrieval.l0_understanding.vocabulary import (
        load_vocabulary,
    )
    return await load_vocabulary(ORG)


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_scalar_count_matches_the_table():
    from opencortyx.core.substrate import NodeRepo
    v = await _vocab()
    if not v:
        pytest.skip("org has no graph loaded")
    res = await A.execute(ORG, A.parse(build_plan("How many turns are there?", v), v))
    truth = await NodeRepo().count(ORG, exact_types=["Turn"], props_not={"superseded": "true"})
    assert res.scalar == float(truth), f"counted {res.scalar}, table holds {truth}"


@pytest.mark.integration
@pytest.mark.anyio
async def test_a_grouped_count_covers_every_group_not_a_retrieved_sample():
    v = await _vocab()
    if not v:
        pytest.skip("org has no graph loaded")
    res = await A.execute(ORG,
                          A.parse(build_plan("How many turns did each session have?", v), v))
    assert res.usable and res.groups_total > 100, \
        f"only {res.groups_total} group(s) — this looks like a sample, not a computation"
    assert sum(r.value for r in res.rows) > 0
    # The listing is capped at MAX_GROUPS; the group count is not.
    assert len(res.rows) <= A.MAX_GROUPS
    assert res.truncated == (res.groups_total > A.MAX_GROUPS)


@pytest.mark.integration
@pytest.mark.anyio
async def test_unconnected_types_are_inconclusive_not_zero():
    """`Fact` reaches `Session` only through `Turn`. Rendering that as "0 session(s)" states as a
    measured result something that is really "these are not adjacent here"."""
    v = await _vocab()
    if not v:
        pytest.skip("org has no graph loaded")
    res = await A.execute(ORG, A.parse(build_plan("How many facts per session?", v), v))
    assert res.groups_total == 0
    assert res.inconclusive and not res.usable
    assert "not a count of zero" in res.note
