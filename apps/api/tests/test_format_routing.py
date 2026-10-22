"""Routing + per-format parser tests.

Two things are guarded here:

  * **Routing** — records go to the walker, tables to the structured path, prose to the chunker.
    This is what stops a 100k-row export being chunked as if it were an essay.
  * **Parsers** — every format has its OWN parser that models that format, not a shared converter
    that flattens everything to a dict. The assertions below are about the semantics each parser is
    responsible for preserving: XML attributes and mixed content, JSONL line provenance, YAML
    multi-document streams.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencortyx.ingest.formats import catalog as F
from opencortyx.ingest.detection import routing                       # noqa: E402
from opencortyx.ingest.formats import semistructured as P                       # noqa: E402

JSONL_BYTES = b'{"a":1}\n{"a":2}\nNOT JSON\n{"a":3}\n'
NDJSON_CLEAN = b'{"a":1}\n{"a":2}\n{"a":3}\n'
RSS_BYTES = (b'<?xml version="1.0"?><rss><channel><title>F</title>'
             b"<item><t>A</t></item><item><t>B</t></item></channel></rss>")


# ── routing by extension ─────────────────────────────────────────────────────
@pytest.mark.parametrize("name,expected", [
    ("a.json", F.SEMISTRUCTURED), ("a.jsonl", F.SEMISTRUCTURED),
    ("a.ndjson", F.SEMISTRUCTURED), ("a.xml", F.SEMISTRUCTURED),
    ("a.yaml", F.SEMISTRUCTURED), ("a.yml", F.SEMISTRUCTURED),
    ("a.toml", F.SEMISTRUCTURED), ("a.rss", F.SEMISTRUCTURED),
    ("a.csv", F.STRUCTURED), ("a.tsv", F.STRUCTURED),
    ("a.parquet", F.STRUCTURED), ("a.xlsx", F.STRUCTURED), ("a.orc", F.STRUCTURED),
    ("a.txt", F.DOCUMENT), ("a.pdf", F.DOCUMENT), ("a.docx", F.DOCUMENT),
    ("a.html", F.DOCUMENT), ("a.eml", F.DOCUMENT),
])
def test_route_by_extension(name, expected):
    assert routing.route_for(name) == expected


def test_records_never_route_to_the_document_chunker():
    """The regression that motivated all of this: a big JSONL must not be chunked as prose."""
    assert routing.route_for("events.jsonl") == F.SEMISTRUCTURED
    assert routing.route_for("ledger.csv") == F.STRUCTURED


def test_explicit_override_wins():
    assert routing.route_for("a.jsonl", override=F.DOCUMENT) == F.DOCUMENT
    assert routing.route_for("a.txt", override=F.SEMISTRUCTURED) == F.SEMISTRUCTURED
    assert routing.route_for("a.jsonl", override="nonsense") == F.SEMISTRUCTURED


@pytest.mark.parametrize("name,blob,expected", [
    ("x.vendorexport", b'{"records":[{"id":1}]}', F.SEMISTRUCTURED),
    ("x.dat", NDJSON_CLEAN, F.SEMISTRUCTURED),
    ("x.bin", b"<root><item>1</item></root>", F.SEMISTRUCTURED),
    ("x.conf", b"service: api\nreplicas: 3\nlimits:\n  cpu: 2\n", F.SEMISTRUCTURED),
    ("settings", b'[tool]\nname = "x"\nversion = 2\n', F.SEMISTRUCTURED),
    ("x.out", b"a,b,c\n1,2,3\n4,5,6\n7,8,9\n", F.STRUCTURED),
    # Source code whose first lines happen to be JSON scalars is still prose to read, not JSON
    # Lines: jinja2's idtracking.py was routed to the record walker.
    ("idtracking.py", b'1\n"param"\nimport typing as t\nVAR_LOAD_PARAMETER = "param"\n', F.DOCUMENT),
])
def test_unknown_extensions_are_identified_by_content(name, blob, expected):
    assert routing.route_for(name, blob) == expected


@pytest.mark.parametrize("blob", [
    b"Marie Curie was a physicist born in Warsaw, Poland in 1867.",
    b"Once upon a time there was a long story about many things.",
    b"Dear Ana,\n\nPlease review the attached report.\n\nRegards,\nBo\n",
])
def test_prose_is_never_stolen_from_the_document_path(blob):
    assert routing.route_for("mystery", blob) == F.DOCUMENT


def test_html_is_a_document_not_xml():
    html = b"<!DOCTYPE html><html><body><p>hello</p></body></html>"
    assert routing.route_for("page", html) == F.DOCUMENT


# ── one parser per format ────────────────────────────────────────────────────
def test_each_format_is_claimed_by_its_own_parser():
    """The point of the registry: a real parser per format, never a generic converter."""
    for blob, name, expected in [
        (b'{"a":1}', "x.json", "json"),
        (NDJSON_CLEAN, "x.jsonl", "jsonl"),
        (b"<r><a>1</a></r>", "x.xml", "xml"),
        (b"a: 1\nb: 2\n", "x.yaml", "yaml"),
        (b'[t]\nk = "v"\n', "x.toml", "toml"),
    ]:
        assert P.parse(blob, name).parser == expected, name


def test_unknown_extension_is_parsed_by_the_owning_format_parser():
    """A vendor export named .acmedata that is XML must be parsed BY THE XML PARSER."""
    r = P.parse(b"<root><row>1</row><row>2</row></root>", "weird.acmedata")
    assert r.parser == "xml" and r.record_count == 2


def test_registry_has_no_priority_ties():
    """Ties make which parser wins depend on dict order — a real source of flaky behaviour."""
    prios = [p["priority"] for p in P.describe()]
    assert len(prios) == len(set(prios)), f"duplicate priorities: {sorted(prios)}"


def test_every_semistructured_extension_has_an_owning_parser():
    owned = P.extensions()
    for ext, fmt in F.FORMATS.items():
        if fmt.route == F.SEMISTRUCTURED:
            assert ext in owned, f".{ext} routes to the walker but no parser claims it"


# ── JSONL: a stream of records, line number is identity ──────────────────────
def test_jsonl_records_carry_line_provenance():
    r = P.parse(JSONL_BYTES, "x.jsonl")
    assert r.parser == "jsonl"
    assert [rec.data["a"] for rec in r.records] == [1, 2, 3]
    assert [rec.location for rec in r.records] == ["line 1", "line 2", "line 4"]
    assert r.metadata["lines_malformed"] == 1
    assert r.warnings and "line 3" in r.warnings[0]


def test_jsonl_with_no_valid_lines_is_an_error():
    with pytest.raises(P.ParseError, match="no valid JSON lines"):
        P.parse(b"nope\nalso nope\n", "x.jsonl", parser="jsonl")


def test_mislabelled_extension_still_ingests():
    """A .json file that is really NDJSON is common in real exports."""
    r = P.parse(NDJSON_CLEAN, "data.json")
    assert r.parser == "jsonl" and r.record_count == 3


# ── XML: attributes, mixed content, namespaces, nested records ───────────────
def test_xml_repeated_tags_become_a_list():
    r = P.parse(b"<r><item>a</item><item>b</item><item>c</item></r>", "x.xml")
    assert r.data["r"]["item"] == ["a", "b", "c"], "repeated tags must not clobber each other"
    assert r.record_count == 3, "a repeated element is the document's record collection"


def test_xml_keeps_attributes_and_mixed_content_and_strips_namespaces():
    r = P.parse(b'<r xmlns="urn:x"><m unit="kg">5</m></r>', "x.xml")
    assert r.data["r"]["m"]["@unit"] == "kg", "attributes are data, not decoration"
    assert r.data["r"]["m"]["#text"] == "5", "mixed content must survive"
    assert "urn:x" not in str(r.data), "namespace URIs must not leak into keys"
    assert r.metadata["namespaces"] == ["urn:x"], "but must be recorded"


def test_xml_finds_records_nested_below_the_root():
    """RSS puts <item> under <channel>; a root-only search finds nothing."""
    r = P.parse(RSS_BYTES, "feed.rss")
    assert r.parser == "xml" and r.record_count == 2
    assert r.metadata["record_path"] == "/rss/channel/item"


# ── YAML / TOML ──────────────────────────────────────────────────────────────
def test_yaml_multi_document_stream_is_a_record_collection():
    r = P.parse(b"---\nname: a\n---\nname: b\n", "x.yaml")
    assert r.parser == "yaml" and r.record_count == 2


def test_json_is_not_claimed_by_yaml():
    """YAML is a near-superset of JSON, so priority must keep JSON documents with the JSON parser."""
    assert P.parse(b'{"a": 1, "b": [1,2,3]}', "mystery").parser == "json"


# ── failure modes ────────────────────────────────────────────────────────────
def test_parsers_reject_scalars():
    """`yaml.safe_load('hello')` returns a string — that is not a record tree."""
    with pytest.raises(P.ParseError):
        P.parse(b"just a sentence of prose", "mystery")


def test_empty_file_is_a_clear_error():
    with pytest.raises(P.ParseError, match="empty"):
        P.parse(b"", "a.json")


def test_malformed_json_names_the_location():
    with pytest.raises(P.ParseError, match="line"):
        P.parse(b'{"a": ', "a.json", parser="json")


def test_forced_parser_does_not_silently_fall_back():
    """Naming a parser is a statement about the format; switching silently would hide a mistake."""
    with pytest.raises(P.ParseError):
        P.parse(b"<r><a>1</a></r>", "x.xml", parser="json")


def test_unknown_parser_name_is_rejected():
    with pytest.raises(P.ParseError, match="unknown parser"):
        P.parse(b'{"a":1}', "a.json", parser="nope")
