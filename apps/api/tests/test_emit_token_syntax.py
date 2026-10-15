"""One `$` syntax, every slot.

`emit.attributes[].set` with `on: "source"` is resolved by `_resolve`; every other slot (edge
endpoints, node labels, `type_field`, attribute merges onto a resolved node) is resolved by
`_fill_field`. A bare `$normalized_date` used to mean `record["normalized_date"]` in one and "" in
the other — so the identical token silently did nothing in one place and worked in the rest. These
tests pin that the two now agree, and that `$llm.` stays a working alias rather than a requirement
on extractors that have no model.
"""
from opencortyx.ingest.extraction.templates import _resolve, _fill_field

PARSED = {"normalized_date": "2022-03-03", "phrase": "3 March 2022", "kind": "explicit"}
CTX = {"text": "I finished my first screenplay on 3 March 2022.", "speaker": "Nate",
       "normalized_date": "SHOULD NOT WIN"}


def test_bare_token_agrees_with_the_other_resolver():
    """The whole point: the same token, the same answer, whichever slot it sits in."""
    assert _resolve("$normalized_date", PARSED, CTX) == _fill_field("$normalized_date", PARSED)
    assert _resolve("$normalized_date", PARSED, CTX) == "2022-03-03"


def test_bare_token_reads_the_mechanism_output_not_the_node():
    """A python extractor's `extract()` returned it; the node also has a key of that name."""
    assert _resolve("$normalized_date", PARSED, CTX) == "2022-03-03"


def test_llm_prefix_is_still_an_alias():
    for tok in ("$llm.normalized_date", "$out.normalized_date", "$record.normalized_date"):
        assert _resolve(tok, PARSED, CTX) == "2022-03-03", tok


def test_source_prefix_still_reads_the_node():
    assert _resolve("$source.speaker", PARSED, CTX) == "Nate"
    assert _resolve("$source.normalized_date", PARSED, CTX) == "SHOULD NOT WIN"


def test_whole_object_aliases():
    for tok in ("$llm", "$record", "$out"):
        assert _resolve(tok, PARSED, CTX) == PARSED, tok


def test_unknown_field_is_empty_not_the_whole_context():
    """The bug the old '' was defending against: a miss must not hand back all of ctx."""
    out = _resolve("$no_such_field", PARSED, CTX)
    assert out == "", f"expected '', got {type(out).__name__} {out!r}"
    assert _resolve("$llm.no_such_field", PARSED, CTX) == ""


def test_placeholders_and_literals_are_untouched():
    assert _resolve("{speaker} said so", PARSED, CTX) == "Nate said so"
    assert _resolve("a literal", PARSED, CTX) == "a literal"
    assert _resolve(7, PARSED, CTX) == 7


def test_non_dict_output_does_not_explode():
    assert _resolve("$normalized_date", None, CTX) == ""
    assert _resolve("$normalized_date", ["not", "a", "dict"], CTX) == ""


PRODUCTION = {"records": [{"phrase": "3 March 2022", "normalized_date": "2022-03-03"}]}


def test_every_spelling_reaches_into_the_production():
    for tok in ("$normalized_date", "$llm.normalized_date",
                "$out.normalized_date", "$record.normalized_date"):
        assert _resolve(tok, PRODUCTION, CTX) == "2022-03-03", tok


def test_top_level_still_wins_over_the_first_record():
    parsed = {"normalized_date": "TOP", "records": [{"normalized_date": "NESTED"}]}
    assert _resolve("$normalized_date", parsed, CTX) == "TOP"


def test_a_field_in_neither_place_is_still_empty():
    assert _resolve("$no_such_field", PRODUCTION, CTX) == ""


def test_empty_or_malformed_records_do_not_raise():
    for parsed in ({"records": []}, {"records": "not a list"}, {"records": [None]}, {}):
        assert _resolve("$normalized_date", parsed, CTX) == "", repr(parsed)
