import io

p = "tests/test_docling_ast.py"
s = io.open(p, encoding="utf-8").read().rstrip() + '''


# ── regressions: each of these shipped broken once ─────────────────────────────────────────────
def _fallback_only(monkeypatch):
    """Force the structural fallback, as in any process where Docling cannot load."""
    monkeypatch.setattr(DoclingEngine, "_converter_instance", None)
    monkeypatch.setattr(DoclingEngine, "unavailable_reason", "forced by test")


def test_the_title_contains_the_sections_instead_of_standing_beside_them(monkeypatch):
    """Title and top-level sections were both level 1, so every section became the title's
    sibling and the title contained only the text before the first section."""
    _fallback_only(monkeypatch)
    a = DoclingEngine.convert(b"# Handbook\\n\\nIntro.\\n\\n## Leave\\n\\n25 days.\\n\\n### Sick\\n\\nTen.\\n",
                              filename="handbook.md", doc_id="doc")
    by_text = {n.text: n for n in a.nodes}
    title, leave, sick = by_text["Handbook"], by_text["Leave"], by_text["Sick"]
    assert title.type == "Title" and title.parent_id == "doc"
    assert leave.parent_id == title.id, "a level-1 section belongs under the title"
    assert sick.parent_id == leave.id
    assert by_text["Ten."].parent_id == sick.id


def test_a_pdf_the_fallback_reads_never_becomes_pdf_syntax(monkeypatch):
    """Decoding PDF bytes as text produced 318 nodes reading "xref", "endobj", "%%EOF"."""
    _fallback_only(monkeypatch)
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Payment terms are thirty days.", fontsize=12)
    a = DoclingEngine.convert(doc.tobytes(), filename="terms.pdf", doc_id="doc")
    texts = " ".join(n.text for n in a.nodes)
    assert "thirty days" in texts
    for syntax in ("endobj", "xref", "%%EOF", "/Type"):
        assert syntax not in texts, f"PDF syntax {syntax!r} leaked into the graph"
    assert a.engine == "fallback" and a.fallback_reason


def test_a_converter_that_crashes_on_load_falls_back_instead_of_failing_the_ingest(monkeypatch):
    """docling under pydantic < 2.10 raises AttributeError, not ImportError, at import. Only
    ImportError was caught, so the error escaped and the whole document failed."""
    monkeypatch.setattr(DoclingEngine, "_converter_instance", None)
    monkeypatch.setattr(DoclingEngine, "unavailable_reason", None)

    def _boom(*_a, **_kw):
        raise AttributeError("force_full_page_ocr")
    monkeypatch.setattr("builtins.__import__", _import_raising("docling", _boom))
    a = DoclingEngine.convert(b"# T\\n\\nbody\\n", filename="t.md", doc_id="doc")
    assert a.engine == "fallback"
    assert "AttributeError" in DoclingEngine.unavailable_reason
    assert [n.type for n in a.nodes] == ["Title", "Text"]


def test_a_table_without_text_is_given_its_grid_as_text():
    """Docling leaves TableItem.text empty; an empty-text node is never embedded or retrieved."""
    assert docling_engine._grid_text([["Item", "Limit"], ["Hotel", "150"]]) == \\
        "Item | Limit\\nHotel | 150"


def test_a_sample_with_no_extension_is_not_sent_to_docling_as_a_pdf():
    """The Mapper previews a text sample with no filename; `.pdf` was assumed, so it always failed."""
    assert docling_engine._effective_filename("# hi", "").endswith(".md")
    assert docling_engine._effective_filename(b"%PDF-1.7 ...", "").endswith(".pdf")
    assert docling_engine._effective_filename(b"x", "keep.docx") == "keep.docx"


def test_only_headings_open_a_section():
    """An element carrying a level (a list item, a caption) must not adopt what follows it."""
    a = DynamicDoclingAssembler(doc_id="doc")
    h = a.add_element(raw_label="section_header", text="Scope", level=1)
    li = a.add_element(raw_label="list_item", text="one", level=3)
    after = a.add_element(raw_label="text", text="after the list")
    assert li.parent_id == h.id and li.level is None
    assert after.parent_id == h.id, "the list item must not have become a heading"


def _import_raising(prefix, fn):
    real = __import__

    def _imp(name, *args, **kwargs):
        if name == prefix or name.startswith(prefix + "."):
            return fn()
        return real(name, *args, **kwargs)
    return _imp
'''
io.open(p, "w", encoding="utf-8", newline="\n").write(s + "\n")
print("appended")
