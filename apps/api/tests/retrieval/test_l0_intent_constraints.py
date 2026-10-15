"""L0 unit tests — classification and constraint extraction.

Cases are drawn from the LoCoMo suite verbatim where possible, so a passing test means the
classifier works on the questions the system is actually asked, not on invented ones.
"""
from opencortyx.retrieval.contracts import Cardinality, Intent
from opencortyx.retrieval.l0_understanding.constraints import (
    extract_attribute_constraints, extract_temporal, parse_dates, to_iso, within,
)
from opencortyx.retrieval.l0_understanding.intent import (
    classify, is_relational, is_temporal,
)


# ── intent ───────────────────────────────────────────────────────────────────────────────────

def test_aggregation_beats_everything():
    for q in ("How many hikes has Joanna been on?",
              "How many children does Melanie have?",
              "What is the total number of transactions per company?"):
        assert classify(q).intent is Intent.AGGREGATION, q
        assert classify(q).cardinality is Cardinality.COUNT


def test_multi_hop_detected_from_chained_relation():
    c = classify("Which companies are connected to John through transactions involving Germany?")
    assert c.intent is Intent.MULTI_HOP
    assert c.confidence > 0.3


def test_relationship_language():
    c = classify("What do Jon and Gina both have in common?")
    assert c.intent in (Intent.RELATIONSHIP, Intent.MULTI_HOP)
    assert is_relational("What do Jon and Gina both have in common?")


def test_entity_profile():
    assert classify("Tell me about JP Morgan").intent is Intent.ENTITY
    assert classify("Who is Caroline?").intent in (Intent.ENTITY, Intent.EXACT_LOOKUP)


def test_identifier_forces_exact_lookup():
    c = classify("In document DOC-00000731, what city is recorded for record REC-890998?")
    assert c.intent is Intent.EXACT_LOOKUP
    assert "identifier-token" in c.signals


def test_temporal_only_when_nothing_stronger():
    assert classify("When did Joanna's laptop crash?").intent is Intent.TEMPORAL
    # A relation plus a date is still about the relation.
    c = classify("Which companies did John transact with through Germany in 2024?")
    assert c.intent in (Intent.MULTI_HOP, Intent.RELATIONSHIP)
    assert is_temporal("Which companies did John transact with through Germany in 2024?")


def test_unrecognised_question_defaults_to_semantic_not_empty():
    c = classify("Something entirely unlike the training patterns")
    assert c.intent is Intent.SEMANTIC
    assert c.confidence < 0.5          # low confidence → the planner should widen, not narrow


def test_empty_query_is_safe():
    c = classify("")
    assert c.intent is Intent.SEMANTIC and c.confidence == 0.0


def test_set_cardinality():
    assert classify("What activities does Melanie partake in?").cardinality is Cardinality.SET
    assert classify("Is it likely that Nate has friends besides Joanna?").cardinality \
        is Cardinality.SET


# ── dates ────────────────────────────────────────────────────────────────────────────────────

def test_parse_corpus_date_shapes():
    assert parse_dates("on 25 May, 2022")[0][0] == "2022-05-25"
    assert parse_dates("May 25, 2022")[0][0] == "2022-05-25"
    assert parse_dates("2022-05-25")[0][0] == "2022-05-25"


def test_bare_year_spans_the_whole_year():
    s, e, _ = parse_dates("what happened in 2022")[0]
    assert s == "2022-01-01" and e == "2022-12-31"


def test_month_spans_the_month():
    s, e, _ = parse_dates("in April 2022")[0]
    assert s == "2022-04-01" and e == "2022-04-30"


def test_relation_comes_from_the_words_around_the_date():
    assert extract_temporal("What happened to Acme after 2023")[0].relation == "after"
    assert extract_temporal("anything before 2021?")[0].relation == "before"
    assert extract_temporal("What did Nate do on 25 May, 2022?")[0].relation == "during"


def test_between_two_years():
    c = extract_temporal("What changed between 2023 and 2025?")[0]
    assert c.relation == "between"
    assert c.start == "2023-01-01" and c.end == "2025-12-31"


def test_no_date_no_constraint():
    assert extract_temporal("What is Joanna allergic to?") == []


def test_to_iso_handles_the_corpus_timestamp_format():
    """This exact shape sits on all 5,882 turns and was in no index."""
    assert to_iso("2:01 pm on 23 January, 2022") == "2022-01-23"
    assert to_iso("11:04 am on 23 April, 2022") == "2022-04-23"
    assert to_iso("") is None
    assert to_iso("not a date at all") is None


def test_unknown_dates_pass_the_filter():
    """Rejecting an unparseable date would delete evidence on a technicality."""
    c = extract_temporal("after 2023")[0]
    assert within(c, None) is True
    assert within(c, "2024-01-01") is True
    assert within(c, "2021-01-01") is False


# ── attribute constraints ────────────────────────────────────────────────────────────────────

def test_numeric_comparators_with_units():
    cs = extract_attribute_constraints("transactions with amount more than $1M")
    assert any(c.op == "gt" and c.value == 1_000_000 for c in cs)
    cs = extract_attribute_constraints("at least 500k")
    assert any(c.op == "gte" and c.value == 500_000 for c in cs)


def test_explicit_field_value():
    cs = extract_attribute_constraints("country = Germany")
    assert any(c.attr == "country" and c.op == "eq" and c.value == "Germany" for c in cs)


def test_no_constraints_in_plain_question():
    assert extract_attribute_constraints("What is Joanna allergic to?") == []
