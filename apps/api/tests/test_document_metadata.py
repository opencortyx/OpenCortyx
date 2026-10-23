"""A document's own metadata, read from its opening, and the links the resolution engine makes from it."""
import uuid

from opencortyx.ingest.formats.unstructured.layout.metadata import document_metadata, header_fields
from opencortyx.ingest.formats.unstructured.layout.readers.flat import FlatReader
from opencortyx.ingest.formats.unstructured.layout.assembler import LayoutAssembler
from opencortyx.resolution.engine import DOC_ID, DOC_REF, link_document


def _read_text(text: str, filename: str) -> dict:
    asm = LayoutAssembler(doc_id="d")
    FlatReader().read(text.encode(), filename, asm)
    return document_metadata([n.text for n in asm.nodes], [n.label for n in asm.nodes], filename)


PEP = """PEP: 809
Title: Stable ABI for the Future
Author: Steve Dower <steve.dower@python.org>
Status: Draft
Type: Standards Track
Requires: 703, 793, 697
Created: 19-Sep-2025

Abstract
========

The stable ABI. Note: this is prose, not a header field.
"""

RFC = """Internet Engineering Task Force (IETF)                      M. Boucadair
Request for Comments: 8113                                        Orange
Updates: 6830                                               C. Jacquenet
Category: Experimental                                            Orange
ISSN: 2070-1721                                               March 2017


       Locator/ID Separation Protocol (LISP): Shared Extension Message

Abstract

   This document specifies a shared message type.
"""


def test_a_header_preamble_is_read_line_by_line():
    md = _read_text(PEP, "pep-0809.rst")
    assert md["title"] == "Stable ABI for the Future"
    assert md["identifiers"] == ["pep 809"]
    assert md["references"] == [["REQUIRES", "pep 703"], ["REQUIRES", "pep 793"], ["REQUIRES", "pep 697"]]
    assert md["fields"]["status"] == "Draft" and md["published"] == "19-Sep-2025"
    assert "note" not in md["fields"]                      # the body's "Note:" is not a header field


def test_a_two_column_header_keeps_each_value_to_its_column():
    md = _read_text(RFC, "rfc8113.txt")
    assert md["identifiers"] == ["rfc 8113"]                 # "Request for Comments" -> rfc
    assert md["references"] == [["UPDATES", "rfc 6830"]]     # not the author or the date beside it
    assert md["fields"]["category"] == "Experimental"
    assert md["published"] == "March 2017"
    assert md["title"] == "Locator/ID Separation Protocol (LISP): Shared Extension Message"


def test_a_definition_list_and_a_table_header_are_read_too():
    # An HTML page's <dl>: each term and its value are elements of their own.
    texts = ["RFC 9304", "RFC:", "9304", "Obsoletes:", "8113", "Published:", "October 2022", "RFC 9304",
             "Locator/ID Separation Protocol (LISP)", "Abstract"]
    labels = ["table", "text", "text", "text", "text", "text", "text", "title", "title", "section_header"]
    md = document_metadata(texts, labels, "rfc9304.html")
    assert md["references"] == [["OBSOLETES", "rfc 8113"]] and md["published"] == "October 2022"
    assert md["title"] == "Locator/ID Separation Protocol (LISP)"   # not the bare "RFC 9304"
    # A PDF's header as table rows; the series alias is the document's, not a reference.
    rows = ["RFC: | 9110\nSTD: | 97\nObsoletes: | 2818, 7230\nPublished: | June 2022"]
    md = document_metadata(rows, ["table"], "rfc9110.pdf")
    assert md["identifiers"] == ["rfc 9110", "std 97"]
    assert md["references"] == [["OBSOLETES", "rfc 2818"], ["OBSOLETES", "rfc 7230"]]


def test_one_field_is_prose_and_no_header_means_no_metadata_fields():
    assert header_fields(["Note: this is a sentence."]) == {"Note": "this is a sentence."}
    md = document_metadata(["Note: this is a sentence.", "More text."], ["text", "text"], "notes.md")
    assert md["fields"] == {} and md["identifiers"] == [] and md["references"] == []


class _Store:
    """The identity index and the graph's documents, in memory."""
    def __init__(self, docs):
        self.docs, self.rows, self.edges = docs, set(), []

    async def document(self, ws, sid):
        return self.docs.get(sid)

    async def index(self, ws, rows):
        self.rows |= set(rows)

    async def lookup(self, ws, node_type, keys, exclude):
        return {k: n for t, k, n in sorted(self.rows, key=str) if t == node_type and k in keys and n not in exclude}

    async def referrers(self, ws, keys, exclude):
        return [(t[len(DOC_REF):], n) for t, k, n in self.rows if t.startswith(DOC_REF) and k in keys and n not in exclude]

    async def link(self, ws, links):
        self.edges += links


class _Doc:
    def __init__(self, props):
        self.properties = props


async def test_documents_are_linked_whichever_is_resolved_first():
    ws, new, old = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    docs = {new: _Doc({"identifiers": ["rfc 9304"], "__references": [["OBSOLETES", "rfc 8113"]]}),
            old: _Doc({"identifiers": ["rfc 8113"]})}
    for order in ([new, old], [old, new]):
        store = _Store(docs)
        made = [await link_document(ws, sid, store) for sid in order]
        assert store.edges == [(new, "OBSOLETES", old)] and sum(made) == 1
        assert any(t == DOC_ID for t, _, _ in store.rows)


def test_a_document_without_text_shows_its_metadata_in_the_context():
    from opencortyx.retrieval.contracts import Candidate
    from opencortyx.retrieval.l4_context.builder import _block_text
    doc = Candidate(node_id=uuid.uuid4(), node_type="Source", label="rfc8113.txt", text="",
                    properties={"filename": "rfc8113.txt", "checksum": "ab12", "title": "LISP Shared Extension",
                                "identifiers": ["rfc 8113"], "published": "March 2017",
                                "category": "Experimental", "__references": [["UPDATES", "rfc 6830"]]})
    text = _block_text(doc, None)
    assert text.startswith("rfc8113.txt — ") and "published: March 2017" in text and "category: Experimental" in text
    assert "checksum" not in text and "__references" not in text
    plain = Candidate(node_id=uuid.uuid4(), node_type="Person", label="Ada", text="", properties={"role": "x"})
    assert _block_text(plain, None) == "Ada"                 # nodes without document metadata are unchanged
