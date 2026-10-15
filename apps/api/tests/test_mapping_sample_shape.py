"""A mapping's saved sample is structured JSON — whichever page or client wrote it.

The Mapper's Materialized graph walks `payload.sample`. Five saved mappings stored that sample as a
JSON-encoded STRING: the older semi-structured page sent the uploaded file's text as-is, and nothing on
the way in normalised it. A string has no keys to walk, so the preview drew 0 nodes for a mapping whose
data had already ingested 10 dialogues and 6,079 turns.

Measured before the fix: all 5 of those strings were valid JSON (four 698-char objects pasted from a
Windows file, one 433,093-char list of 2 records). Nothing was truncated or malformed — the shape was
simply never decided in one place.

It is decided in `_prepare_mapping_payload`, because that is the one function every writer AND every
reader passes through: the create and update endpoints call it on save, and `_mapping_view` calls it
again on every read. So legacy rows are served correctly without rewriting stored data, and a new writer
cannot reintroduce the bug.

Pure unit tests of that function — no database.
"""
import json

from opencortyx.api.services.ingestion.schema_mappings import prepare_mapping_payload

RECORDS = [{"conversation": {"session_1": [{"text": "hi"}]}, "sample_id": "conv-1"},
           {"conversation": {"session_1": [{"text": "yo"}]}, "sample_id": "conv-2"}]
CUSTOMER = {"customer": {"customer_id": "C001", "name": "Ada"}, "order": {"status": "Completed"}}


def _prep(paradigm, payload):
    return prepare_mapping_payload(paradigm, "t", payload)


def test_a_json_string_sample_becomes_the_data_it_encodes():
    """THE bug: a list of records stored as text. It must come back as the list."""
    out = _prep("semistructured", {"sample": json.dumps(RECORDS)})
    assert out["sample"] == RECORDS


def test_a_windows_pasted_object_is_parsed_too():
    """The four other broken mappings: an object pasted with CRLF line endings."""
    text = json.dumps(CUSTOMER, indent=4).replace("\n", "\r\n")
    assert _prep("semistructured", {"sample": text})["sample"] == CUSTOMER


def test_a_sample_that_is_already_data_is_untouched():
    """The control: what the Mapper page itself saves must round-trip exactly."""
    assert _prep("semistructured", {"sample": RECORDS})["sample"] == RECORDS


def test_the_derived_mapping_early_return_does_not_skip_it():
    """`_prepare_mapping_payload` returns early for a derived, untouched editor. The sample must be
    normalised before that, or those mappings would keep serving a string."""
    payload = {"editor": {"__derived": True}, "sample": json.dumps(RECORDS)}
    assert _prep("semistructured", payload)["sample"] == RECORDS


def test_a_structured_mapping_sample_is_parsed():
    rows = {"orders": [{"id": 1}, {"id": 2}]}
    assert _prep("structured", {"sample": json.dumps(rows)})["sample"] == rows


def test_text_that_is_not_json_is_kept_as_the_author_wrote_it():
    """Never destroy what someone saved: text that does not parse is returned unchanged."""
    assert _prep("semistructured", {"sample": "not json {"})["sample"] == "not json {"


def test_an_unstructured_documents_text_is_never_reinterpreted():
    """A document sample is prose. Even prose that happens to parse as JSON stays text."""
    assert _prep("unstructured", {"sample": "42"})["sample"] == "42"


def test_a_payload_without_a_sample_gains_no_sample_key():
    assert "sample" not in _prep("semistructured", {})
