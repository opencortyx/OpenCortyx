"""The layout AST and the reader for each document format.

Every reader adds elements with the same labels (title, section_header with a level, text,
list_item, table with its grid, picture, page_header, page_footer), so a document becomes the same
shape of graph whatever its format. The assembler tests pin that shape; the reader tests pin what
each format yields.
"""
import io
import json
import zipfile

import pytest

from opencortyx.ingest.formats.unstructured.layout import engine as layout_engine
from opencortyx.ingest.formats.unstructured.layout.assembler import LayoutAssembler
from opencortyx.ingest.formats.unstructured.layout.engine import LayoutEngine
from opencortyx.ingest.formats.unstructured.layout.readers import reader_for
from opencortyx.ingest.formats.unstructured.layout.schemas import layout_element_types, to_pascal_case
from opencortyx.ingest.walkers.unstructured import UnstructuredWalker
from tests.pdfwriter import make_pdf


def _rows(a):
    return {r["text"]: r for r in a.to_dict_list()}


# ── the AST ──────────────────────────────────────────────────────────────────────────────────────
def test_to_pascal_case_dynamic_labels():
    assert to_pascal_case("section_header") == "SectionHeader"
    assert to_pascal_case("page_footer") == "PageFooter"
    assert to_pascal_case("custom_complex_label_123") == "CustomComplexLabel123"


def test_the_node_types_are_the_ones_graphs_already_have():
    """Replacing Docling must not rename a node type: existing graphs, the ontology and the Mapper
    all use these."""
    assert {"Title", "SectionHeader", "Text", "ListItem", "Table", "Picture", "Caption", "Code",
            "PageHeader", "PageFooter", "Footnote", "Formula", "Paragraph"} <= layout_element_types()


def test_assembler_hierarchy_and_next_flow():
    a = LayoutAssembler(document_id="doc-42")
    h1 = a.add_element(label="section_header", text="Introduction", level=1)
    p1 = a.add_element(label="paragraph", text="Opening paragraph.")
    h2 = a.add_element(label="section_header", text="Architecture", level=2)
    t1 = a.add_element(label="table", text="A | B", grid=[["A", "B"], ["1", "2"]], page_no=2)
    assert h1.parent_id == "doc-42" and p1.parent_id == h1.id and h2.parent_id == h1.id
    assert t1.parent_id == h2.id and t1.grid == [["A", "B"], ["1", "2"]]
    assert [n.next_id for n in a.build()] == [p1.id, h2.id, t1.id, None]


def test_only_headings_open_a_section():
    """An element carrying a level (a list item, a caption) must not adopt what follows it."""
    a = LayoutAssembler(doc_id="doc")
    h = a.add_element(raw_label="section_header", text="Scope", level=1)
    li = a.add_element(raw_label="list_item", text="one", level=3)
    after = a.add_element(raw_label="text", text="after the list")
    assert li.parent_id == h.id and li.level is None and after.parent_id == h.id


def test_markdown_export_keeps_the_structure():
    a = LayoutEngine.convert(b"# Handbook\n\n## Leave\n\n- 25 days\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
                             filename="h.md", doc_id="d")
    md = a.to_markdown()
    assert md.startswith("# Handbook") and "## Leave" in md and "- 25 days" in md and "| 1 | 2 |" in md


def test_unstructured_walker_layout_introspection():
    sample = ("# Overview\n\nHere is a paragraph describing OpenCortyx.\n\n"
              "| Service | Port |\n| --- | --- |\n| API | 8000 |\n\n## Submodule\n- Item 1\n- Item 2\n")
    result = UnstructuredWalker().introspect(sample, strategy="layout")
    assert {"SectionHeader", "Text", "Table"} <= set(result["types"]) and "Paragraph" not in result["types"]
    rules = result["edge_rules"]
    assert any(e["type"] == "CONTAINS" for e in rules)
    nxt = [e for e in rules if e["type"] == "NEXT"]
    assert nxt and nxt[0]["to"]["ref"] == "next_id"


# ── the engine ───────────────────────────────────────────────────────────────────────────────────
def test_a_sample_with_no_extension_is_read_as_markdown_not_a_pdf():
    assert layout_engine.effective_filename("# hi", "").endswith(".md")
    assert layout_engine.effective_filename(b"%PDF-1.7 ...", "").endswith(".pdf")
    assert layout_engine.effective_filename(b"x", "keep.docx") == "keep.docx"


def test_a_table_without_text_is_given_its_grid_as_text():
    assert layout_engine.grid_text([["Item", "Limit"], ["Hotel", "150"]]) == "Item | Limit\nHotel | 150"


def test_a_reader_that_fails_falls_back_instead_of_failing_the_ingest(monkeypatch):
    from opencortyx.ingest.formats.unstructured.layout.readers import markdown

    def boom(self, *a, **kw):
        raise RuntimeError("reader broke")
    monkeypatch.setattr(markdown.MarkdownReader, "read", boom)
    a = LayoutEngine.convert(b"# T\n\nbody\n", filename="t.md", doc_id="doc")
    assert a.engine == "fallback" and "reader broke" in a.fallback_reason
    assert [n.type for n in a.nodes] == ["Title", "Text"]


def test_an_unreadable_binary_never_becomes_its_file_syntax(monkeypatch):
    """Decoding PDF bytes as text produced 318 nodes reading "xref", "endobj", "%%EOF"."""
    from opencortyx.ingest.formats.unstructured.layout.readers import pdf

    def boom(self, *a, **kw):
        raise RuntimeError("no PDF engine")
    monkeypatch.setattr(pdf.PDFReader, "read", boom)
    with pytest.raises(ValueError, match="bytes are not text"):
        LayoutEngine.convert(make_pdf([[(72, 72, "Payment terms are thirty days.", 12, False)]]),
                             filename="terms.pdf", doc_id="doc")


def test_heading_levels_come_from_section_numbers():
    heads = [("Employee Handbook", 1), ("1. Onboarding", 1), ("1.1 First week", 1), ("2 Benefits", 1),
             ("Appendix", 1), ("1.1.1 Deep", 2)]
    assert layout_engine.heading_levels(heads, has_title=False) == [0, 1, 2, 1, 1, 2]
    assert layout_engine.heading_levels([("Intro", 1), ("1. A", 1)], has_title=True) == [1, 1]


def test_every_format_has_its_reader():
    expect = {"a.pdf": "pdf", "a.png": "image", "a.doc": "converted", "a.odt": "converted",
              "a.docx": "docx", "a.pptx": "pptx", "a.html": "html", "a.xhtml": "html", "a.md": "markdown",
              "a.rtf": "rtf", "a.epub": "epub", "a.ipynb": "ipynb", "a.svg": "image", "a.xlsx": "excel",
              "a.txt": "text", "a.rst": "text", "a.py": "text", "noextension": "text"}
    assert {f: reader_for(f).name for f in expect} == expect


# ── the readers ──────────────────────────────────────────────────────────────────────────────────
def test_markdown_title_contains_the_sections():
    a = LayoutEngine.convert(b"# Handbook\n\nIntro.\n\n## Leave\n\n25 days.\n\n### Sick\n\nTen.\n",
                             filename="handbook.md", doc_id="doc")
    by = {n.text: n for n in a.nodes}
    assert by["Handbook"].type == "Title" and by["Handbook"].parent_id == "doc"
    assert by["Leave"].parent_id == by["Handbook"].id and by["Sick"].parent_id == by["Leave"].id
    assert by["Ten."].parent_id == by["Sick"].id and a.engine == "markdown"


def test_plain_text_reads_paragraphs_and_numbered_sections():
    text = (b"Internet Engineering Task Force\n\n1.  Introduction\n\n   This document says\n   one thing.\n\n"
            b"1.1.  Scope\n\n   It applies here.\n")
    a = LayoutEngine.convert(text, filename="rfc.txt", doc_id="d")
    by = {n.text: n for n in a.nodes}
    assert by["1.1.  Scope"].parent_id == by["1.  Introduction"].id
    assert "This document says one thing." in by and by["It applies here."].parent_id == by["1.1.  Scope"].id


def test_restructuredtext_underlined_headings():
    a = LayoutEngine.convert(b"Title\n=====\n\nIntro text.\n\nSection\n-------\n\nBody.\n", filename="a.rst", doc_id="d")
    by = {n.text: n for n in a.nodes}
    assert by["Section"].parent_id == by["Title"].id and by["Body."].parent_id == by["Section"].id


def test_html_by_its_tags():
    html = (b"<html><head><script>x()</script></head><body><h1>Guide</h1><p>Intro.</p><h2>Install</h2>"
            b"<ul><li>Download</li><li>Run</li></ul><pre>pip install x</pre>"
            b"<table><tr><th>OS</th><th>Ok</th></tr><tr><td>Linux</td><td>yes</td></tr></table>"
            b"<img src='a.png' alt='diagram'></body></html>")
    a = LayoutEngine.convert(html, filename="g.html", doc_id="d")
    labels = [(n.label, n.text) for n in a.nodes]
    assert ("title", "Guide") in labels and ("section_header", "Install") in labels
    assert ("list_item", "Run") in labels and ("code", "pip install x") in labels
    assert ("picture", "diagram") in labels and not any("x()" in t for _, t in labels)
    table = next(n for n in a.nodes if n.label == "table")
    assert table.grid == [["OS", "Ok"], ["Linux", "yes"]]


def test_word_styles_numbering_tables_headers_and_footers():
    docx = pytest.importorskip("docx")
    d = docx.Document()
    d.sections[0].header.paragraphs[0].text = "ACME Corp - Employee Handbook"
    d.sections[0].footer.paragraphs[0].text = "Confidential"
    d.add_heading("Employee Handbook", level=0)
    d.add_heading("1. Onboarding", level=1)
    d.add_paragraph("New hires complete IT setup on day one.")
    d.add_paragraph("Bring ID", style="List Bullet")
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text, t.cell(0, 1).text, t.cell(1, 0).text, t.cell(1, 1).text = "Item", "Limit", "Hotel", "150"
    buf = io.BytesIO()
    d.save(buf)
    a = LayoutEngine.convert(buf.getvalue(), filename="handbook.docx", doc_id="d")
    rows = _rows(a)
    assert rows["ACME Corp - Employee Handbook"]["label"] == "page_header"
    assert rows["Confidential"]["label"] == "page_footer"
    assert rows["Employee Handbook"]["label"] == "title"
    assert rows["1. Onboarding"]["parent_id"] == rows["Employee Handbook"]["id"]
    assert rows["Bring ID"]["label"] == "list_item"
    assert next(n for n in a.nodes if n.label == "table").grid == [["Item", "Limit"], ["Hotel", "150"]]


def test_powerpoint_slides():
    pptx = pytest.importorskip("pptx")
    prs = pptx.Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Quarterly review"
    s.placeholders[1].text_frame.text = "Revenue grew"
    buf = io.BytesIO()
    prs.save(buf)
    a = LayoutEngine.convert(buf.getvalue(), filename="deck.pptx", doc_id="d")
    rows = _rows(a)
    assert rows["Quarterly review"]["label"] == "title"
    assert rows["Revenue grew"]["parent_id"] == rows["Quarterly review"]["id"]
    assert rows["Revenue grew"]["metadata"]["page_number"] == 1


def test_epub_chapters_in_reading_order():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="O/c.opf"/></rootfiles></container>')
        z.writestr("O/c.opf", '<package><manifest><item id="a" href="2.xhtml"/><item id="b" href="1.xhtml"/>'
                              '</manifest><spine><itemref idref="b"/><itemref idref="a"/></spine></package>')
        z.writestr("O/1.xhtml", "<html><body><h1>One</h1><p>first</p></body></html>")
        z.writestr("O/2.xhtml", "<html><body><h1>Two</h1><p>second</p></body></html>")
    a = LayoutEngine.convert(buf.getvalue(), filename="book.epub", doc_id="d")
    assert [n.text for n in a.nodes] == ["One", "first", "Two", "second"]
    assert [n.metadata.page_number for n in a.nodes] == [1, 1, 2, 2]


def test_notebook_cells():
    nb = {"cells": [{"cell_type": "markdown", "source": ["# Analysis\n", "We load data."]},
                    {"cell_type": "code", "source": "x = 1", "outputs": [{"text": ["1\n"]}]}]}
    a = LayoutEngine.convert(json.dumps(nb).encode(), filename="n.ipynb", doc_id="d")
    assert [(n.label, n.text) for n in a.nodes] == [("title", "Analysis"), ("text", "We load data."),
                                                   ("code", "x = 1"), ("text", "1")]


def test_rtf_text():
    pytest.importorskip("striprtf")
    rtf = rb"{\rtf1\ansi{\fonttbl\f0 Helvetica;}\f0\pard Leave policy\par\par Employees get 25 days.\par}"
    a = LayoutEngine.convert(rtf, filename="p.rtf", doc_id="d")
    assert a.engine == "rtf" and any("25 days" in n.text for n in a.nodes)


def test_a_workbook_as_a_document():
    pd = pytest.importorskip("pandas")
    pytest.importorskip("openpyxl")
    buf = io.BytesIO()
    pd.DataFrame({"Item": ["Hotel"], "Limit": [150]}).to_excel(buf, index=False, sheet_name="Limits")
    a = LayoutEngine.convert(buf.getvalue(), filename="l.xlsx", doc_id="d")
    assert [n.label for n in a.nodes] == ["section_header", "table"]
    assert a.nodes[1].grid == [["Item", "Limit"], ["Hotel", "150"]]


def _handbook_pdf() -> bytes:
    pages = []
    for n, blocks in enumerate([[("Employee Handbook", 22), ("This handbook explains onboarding.", 10.5),
                                 ("1. Onboarding", 15), ("New hires complete IT setup on day one.", 10.5),
                                 ("1.1 First week", 12.5), ("Collect your laptop from the front desk.", 10.5)],
                                [("2. Leave", 15), ("Employees accrue 1.5 days of leave per month.", 10.5)]], 1):
        lines = [(50, 35, "ACME Corp - Employee Handbook", 8, False),
                 (50, 812, f"Confidential - Page {n}", 8, False)]
        y = 90
        for text, size in blocks:
            lines.append((50, y, text, size, size > 11))
            y += size * 2.2
        pages.append(lines)
    return make_pdf(pages)


def test_a_pdf_keeps_its_page_headers_footers_and_section_tree():
    """Running headers and footers belong to the document; numbered sections nest under the title
    and under each other."""
    pytest.importorskip("liteparse")
    a = LayoutEngine.convert(_handbook_pdf(), filename="handbook.pdf", doc_id="d")
    assert a.engine == "pdf", a.fallback_reason
    labels = [r["label"] for r in a.to_dict_list()]
    assert labels.count("page_header") == 2 and labels.count("page_footer") == 2
    assert all(r["parent_id"] == a.doc_id for r in a.to_dict_list() if r["label"] in ("page_header", "page_footer"))
    rows = _rows(a)
    title = rows["Employee Handbook"]
    assert title["label"] == "title" and title["level"] == 0
    assert rows["1. Onboarding"]["parent_id"] == title["id"]
    assert rows["1.1 First week"]["parent_id"] == rows["1. Onboarding"]["id"]
    assert rows["Collect your laptop from the front desk."]["parent_id"] == rows["1.1 First week"]["id"]
    assert rows["Employees accrue 1.5 days of leave per month."]["metadata"]["page_number"] == 2
