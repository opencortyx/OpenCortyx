"""A document's own metadata, read from its opening, and the links the resolution engine makes from it."""
import uuid

from opencortyx.ingest.formats.unstructured.layout.metadata import document_metadata, header_fields
from opencortyx.ingest.formats.unstructured.layout.readers.flat import FlatReader
from opencortyx.ingest.formats.unstructured.layout.assembler import LayoutAssembler
from opencortyx.resolution.engine import link_references


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
    """The identity index and the graph's nodes, in memory."""
    def __init__(self, nodes):
        self.nodes, self.rows, self.edges, self.spec_map = {n.id: n for n in nodes}, set(), [], {}

    async def document(self, ws, sid):
        return self.nodes.get(sid)

    async def index(self, ws, rows):
        self.rows |= set(rows)

    async def lookup(self, ws, node_type, keys, exclude):
        return {k: n for t, k, n in sorted(self.rows, key=str) if t == node_type and k in keys and n not in exclude}

    async def referrers(self, ws, prefix, keys, exclude):
        return [(t[len(prefix):], n, k) for t, k, n in self.rows if t.startswith(prefix) and k in keys and n not in exclude]

    async def link(self, ws, links):
        new = [x for x in links if x not in self.edges]
        self.edges += new
        return len(new)

    async def specs(self, ws):
        return self.spec_map

    async def node_ids(self, ws, type_name):
        return [i for i, n in self.nodes.items() if n.type == type_name]

    async def nodes_by_id(self, ws, ids):
        return {i: self.nodes[i] for i in ids}


def _node(type_, props=None, label="", base=None):
    from opencortyx.stores.graph import Node
    p = dict(props or {})
    if base:
        p["__base_type"] = base
    return Node(id=uuid.uuid4(), workspace_id=uuid.uuid4(), type=type_, label=label, properties=p)


async def test_documents_are_linked_whichever_is_resolved_first():
    new = _node("Source", {"identifiers": ["rfc 9304"], "__references": [["OBSOLETES", "rfc 8113"]]})
    old = _node("Contract", {"identifiers": ["rfc 8113"]}, base="Source")     # a mapped root type
    for order in ([new, old], [old, new]):
        store = _Store([new, old])
        made = [await link_references(uuid.uuid4(), n.id, {}, [], store) for n in order]
        assert store.edges == [(new.id, "OBSOLETES", old.id)] and sum(made) == 1


async def test_a_foreign_key_links_records_across_files_in_either_order():
    from opencortyx.resolution.spec import ResolutionSpec
    specs = {"Order": ResolutionSpec.parse({"references": [{"attribute": "customer_id", "target_type": "Customer",
                                                            "target_attribute": "id", "relation": "placed by"}]})}
    assert specs["Order"].links and not specs["Order"].active   # no identity fields: links only
    orders_file, customers_file = _node("Source"), _node("Source")
    order = _node("Order", {"customer_id": "C-7"})
    customer, other = _node("Customer", {"id": "C-7"}), _node("Customer", {"id": "C-8"})
    files = {orders_file.id: [order], customers_file.id: [customer, other]}
    for order_first in (True, False):
        store = _Store([orders_file, customers_file, order, customer, other])
        sequence = [orders_file, customers_file] if order_first else [customers_file, orders_file]
        for f in sequence:
            await link_references(uuid.uuid4(), f.id, specs, files[f.id], store)
        assert store.edges == [(order.id, "PLACED_BY", customer.id)]


async def test_header_references_can_be_switched_off():
    from opencortyx.resolution.spec import ResolutionSpec
    new = _node("Source", {"identifiers": ["pep 809"], "__references": [["REQUIRES", "pep 703"]]})
    old = _node("Source", {"identifiers": ["pep 703"]})
    store = _Store([new, old])
    specs = {"Source": ResolutionSpec.parse({"header_references": False})}
    for n in (old, new):
        await link_references(uuid.uuid4(), n.id, specs, [], store)
    assert store.edges == []


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


# ── what each format carries outside its text ──────────────────────────────────────────────────────

def _convert(content: bytes, filename: str):
    from opencortyx.ingest.formats.unstructured.layout import LayoutEngine
    return LayoutEngine.convert(content, filename=filename, doc_id="d")


def test_word_core_properties_are_read():
    import io
    import docx
    d = docx.Document()
    d.core_properties.title, d.core_properties.author = "Leave Policy", "People Team"
    d.core_properties.keywords = "leave, holidays"
    d.add_paragraph("Employees get 25 days.")
    buf = io.BytesIO(); d.save(buf)
    asm = _convert(buf.getvalue(), "policy.docx")
    assert asm.metadata["title"] == "Leave Policy" and asm.metadata["author"] == "People Team"
    assert asm.metadata["keywords"] == "leave, holidays"


def test_html_meta_front_matter_rst_fields_notebook_and_rtf_are_read():
    html = (b"<html><head><title>JSON tools</title><meta name='author' content='Ada'>"
            b"<meta name='viewport' content='width=device-width'><meta name='DC.date' content='2024-05-01'>"
            b"</head><body><h1>json</h1><p>Encoders.</p></body></html>")
    meta = _convert(html, "page.html").metadata
    assert meta["title"] == "JSON tools" and meta["author"] == "Ada" and meta["date"] == "2024-05-01"
    assert "viewport" not in meta

    md = b"---\ntitle: Release Notes\nauthor: Ops\ntags: [a, b]\n---\n# Notes\n\nShipped.\n"
    asm = _convert(md, "notes.md")
    assert asm.metadata == {"title": "Release Notes", "author": "Ops", "tags": "a, b"}
    assert "title: Release Notes" not in " ".join(n.text for n in asm.nodes)   # not read as text

    rst = b":Author: Grace\n:Date: 2023-02-01\n\nIntro\n=====\n\nBody text.\n"
    assert _convert(rst, "guide.rst").metadata == {"author": "Grace", "date": "2023-02-01"}

    nb = b'{"metadata": {"title": "Churn", "kernelspec": {"display_name": "Python 3"}}, "cells": [{"cell_type": "markdown", "source": "# Churn"}]}'
    assert _convert(nb, "churn.ipynb").metadata == {"title": "Churn", "kernel": "Python 3"}

    rtf = rb"{\rtf1\ansi{\info{\title Board Minutes}{\author Secretary}}Minutes of the meeting.}"
    assert _convert(rtf, "minutes.rtf").metadata == {"title": "Board Minutes", "author": "Secretary"}


def test_an_epub_package_is_read():
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        z.writestr("OEBPS/book.opf", '<package><metadata><dc:title>The Guide</dc:title><dc:creator>Lin</dc:creator>'
                   '<dc:identifier>urn:isbn:9780000000001</dc:identifier></metadata>'
                   '<manifest><item id="c1" href="c1.xhtml"/></manifest><spine><itemref idref="c1"/></spine></package>')
        z.writestr("OEBPS/c1.xhtml", "<html><head><title>Chapter 1</title></head><body><h1>Start</h1><p>Text.</p></body></html>")
    meta = _convert(buf.getvalue(), "guide.epub").metadata
    assert meta["title"] == "The Guide" and meta["author"] == "Lin"         # not the chapter's <title>
    assert meta["identifier"] == "urn:isbn:9780000000001"


def test_native_fields_fill_gaps_and_never_override_the_documents_own_text():
    md = document_metadata(["Leave Policy", "Employees get 25 days."], ["title", "text"], "policy.docx",
                           native={"title": "Document1", "author": "People Team", "created": "2024-01-02T09:00:00",
                                   "identifier": "HR-2024-07"})
    assert md["title"] == "Leave Policy"                     # the document's own title, not "Document1"
    assert md["fields"]["author"] == "People Team" and md["published"] == "2024-01-02"
    assert "hr-2024-07" in md["identifiers"]


def test_a_mappings_chunk_size_decides_the_paragraph_split():
    from opencortyx.ingest.formats.unstructured.layout import LayoutEngine
    text = (" ".join(f"Sentence {i} explains one part of the policy." for i in range(30))).encode()
    small = LayoutEngine.convert(text, filename="p.txt", doc_id="d", max_chars=300)
    default = LayoutEngine.convert(text, filename="p.txt", doc_id="d")
    assert len(small.nodes) > len(default.nodes) and all(len(n.text) <= 300 for n in small.nodes)


# ── foreign keys declared in a mapping ─────────────────────────────────────────────────────────────

def _orders_mapping(direction="out"):
    """A semi-structured mapping's compiled join edge: Order.customer_id names Customer.id."""
    join = {"kind": "join", "type": "Customer", "field": "customer_id", "on": "id"}
    me = {"kind": "self", "type": "Order"}
    rule = {"type": "PLACED_BY", "path": "", "from": me, "to": join} if direction == "out" else \
        {"type": "PLACED", "path": "", "from": {**join, "field": "customer_id"}, "to": me}
    return {"structures": [{"name": "orders", "edge_rules": [rule]}]}


def test_a_mappings_join_edges_become_reference_rules():
    from opencortyx.resolution.spec import Reference, references_from_mapping
    assert references_from_mapping(_orders_mapping(), "mapping:1") == {
        "Order": [Reference("customer_id", "Customer", "id", "PLACED_BY", False, "mapping:1")]}
    # Drawn from the Customer into the Order that holds the key: the same rule, reversed.
    assert references_from_mapping(_orders_mapping("in"), "mapping:1") == {
        "Order": [Reference("customer_id", "Customer", "id", "PLACED", True, "mapping:1")]}
    assert references_from_mapping({"structures": [{"edge_rules": [
        {"type": "X", "from": {"kind": "self", "type": "A"}, "to": {"kind": "self", "type": "B"}}]}]}, "m") == {}


async def test_a_reversed_rule_is_drawn_into_the_record_and_existing_nodes_are_linked():
    from opencortyx.resolution.engine import link_existing
    from opencortyx.resolution.spec import ResolutionSpec, references_from_mapping
    refs = references_from_mapping(_orders_mapping("in"), "mapping:1")
    order, customer = _node("Order", {"customer_id": "C-7"}), _node("Customer", {"id": "C-7"})
    store = _Store([order, customer])
    store.spec_map = {t: ResolutionSpec(references=tuple(r)) for t, r in refs.items()}
    # The rule arrives after both records were read: linking what is already in the graph.
    made = await link_existing(uuid.uuid4(), ["Order"], _Existing(store))
    assert made == 1 and store.edges == [(customer.id, "PLACED", order.id)]


class _Existing:
    """`link_existing`'s view of the store: the engine Store's names over the in-memory one."""
    def __init__(self, store):
        self.s = store

    def __getattr__(self, name):
        return getattr(self.s, name)

    async def nodes(self, ws, ids):
        return await self.s.nodes_by_id(ws, ids)


async def test_a_mapping_replaces_only_its_own_reference_rules():
    from sqlalchemy import delete
    from opencortyx.core.db import V2Session
    from opencortyx.domain.ontology import TypeRegistry
    from opencortyx.resolution.spec import Reference, get_spec, save_references
    ws = uuid.uuid4()
    a = Reference("customer_id", "Customer", "id", "PLACED_BY", source="mapping:a")
    b = Reference("store_id", "Store", "id", "SOLD_AT", source="mapping:b")
    try:
        async with V2Session() as db:
            assert await save_references(db, ws, "mapping:a", {"Order": [a]}) == ["Order"]
            await save_references(db, ws, "mapping:b", {"Order": [b]})
            await db.commit()
        async with V2Session() as db:
            assert set((await get_spec(db, ws, "Order")).references) == {a, b}
            assert await save_references(db, ws, "mapping:a", {}) == ["Order"]   # a dropped its rule
            await db.commit()
        async with V2Session() as db:
            assert (await get_spec(db, ws, "Order")).references == (b,)
    finally:
        async with V2Session() as db:
            await db.execute(delete(TypeRegistry).where(TypeRegistry.workspace_id == ws))
            await db.commit()
