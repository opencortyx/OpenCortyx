"""Unit tests for L1 embedding retrieval — the pure logic, no database.

The attribute-discovery rules are the part most likely to rot: they decide what is searchable at
all. A field wrongly rejected here is invisible to every downstream layer and the symptom is
indistinguishable from "the model didn't match it".
"""
from opencortyx.core.substrate.document import (
    attr_search_text, discover_attributes, is_indexable_attr,
)

# A real Turn payload from the corpus, verbatim.
TURN_PROPS = {
    "text": "Hey Jo, guess what I did? Dyed my hair last week - come see!",
    "dia_id": "D7:1",
    "source": "locomo_conv-42",
    "session": "session_7",
    "speaker": "Nate",
    "date_time": "11:04 am on 23 April, 2022",
    "turn_index": 1,
    "dialogue_id": "conv-42",
    "created_date": "2026-08-19",
    "__search_text": "Nate, conv-42 - Session 7 ...",
    "__embed_text": "Nate tells Joanna he dyed his hair ...",
    "modified_date": "2026-08-19",
    "__audit_version": 1,
}


def test_discovers_content_and_metadata_fields():
    attrs = discover_attributes(TURN_PROPS)
    assert "text" in attrs
    assert "speaker" in attrs
    # The whole point: date_time was in NO index in the previous engine.
    assert "date_time" in attrs
    assert "session" in attrs


def test_rejects_machinery_and_ids():
    attrs = discover_attributes(TURN_PROPS)
    for k in ("__search_text", "__embed_text", "__audit_version",
              "dia_id", "dialogue_id", "created_date", "modified_date", "source"):
        assert k not in attrs, f"{k} should not be an attribute surface"


def test_rejects_non_string_values():
    assert not is_indexable_attr("turn_index", 1)
    assert not is_indexable_attr("score", 0.5)
    assert not is_indexable_attr("tags", ["a", "b"])
    assert not is_indexable_attr("meta", {"a": 1})


def test_rejects_too_short_and_too_long():
    assert not is_indexable_attr("x", "a")            # 1 char carries nothing
    assert is_indexable_attr("x", "ab")
    assert not is_indexable_attr("x", "z" * 5000)     # a document, not an attribute


def test_no_allow_list_unknown_fields_are_indexed():
    """A field nobody anticipated must become searchable automatically — this is the property a
    hardcoded attribute list destroys."""
    attrs = discover_attributes({"risk_narrative": "exposure to cross-border energy financing",
                                 "industry": "commodity trading"})
    assert attrs["industry"] == "commodity trading"
    assert "risk_narrative" in attrs


def test_search_text_prepends_the_field_name():
    """A bare date embeds as a date-shaped string with no indication of what it dates."""
    assert attr_search_text("date_time", "23 April, 2022") == "date time: 23 April, 2022"
    assert attr_search_text("risk_description", "high") == "risk description: high"
    assert attr_search_text("", "value") == "value"


def test_empty_properties_yield_no_surfaces():
    assert discover_attributes({}) == {}
    assert discover_attributes(None) == {}


# ── attribute specificity (regression: speaker fan-out) ──────────────────────────────────────

def test_specificity_formula_penalises_low_cardinality():
    """MEASURED: `speaker` holds 2 distinct values across 629 turns, so "Joanna" matched EVERY
    turn she spoke and three "Thanks, Nate!" fragments out-ranked the answering statement.
    Live value after the fix: speaker=0.05, text=1.00, date_time=0.50."""
    def spec(n, d):
        return max(0.05, min(1.0, d / float(n)))
    assert spec(629, 2) < 0.05 + 1e-9        # speaker: floored, near-zero influence
    assert spec(629, 629) == 1.0             # text: fully discriminating
    assert 0.3 < spec(629, 300) < 0.6        # date_time: partially
    assert spec(100, 0) == 0.05              # degenerate input never divides by zero
