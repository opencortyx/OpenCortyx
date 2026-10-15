import io

p = "opencortyx/ingest/unstructured/docling/engine.py"
s = io.open(p, encoding="utf-8").read()


def swap(old, new):
    global s
    assert old in s, old[:80]
    s = s.replace(old, new, 1)


# 1. converter init: any failure disables Docling once, with the reason kept.
start = s.index("    _converter_instance = None")
end = s.index("    @classmethod\n    def convert(")
s = s[:start] + '''    _converter_instance = None
    #: Why Docling is unavailable in this process, once found to be. Cached so a worker does not
    #: pay a failing model load on every document, and exposed so a run can say which engine read
    #: it instead of silently producing a differently-shaped graph.
    unavailable_reason: Optional[str] = None

    @classmethod
    def get_converter(cls):
        """Lazy-initialize the Docling DocumentConverter singleton, or None if it cannot load.

        ANY exception disables it, not only ImportError. The failures seen in practice are not
        import errors: docling >= 2.1xx raises AttributeError at import under pydantic < 2.10, and
        OpenCV raises ImportError('libGL.so.1') only on the first PDF. Catching one type let the
        other escape `convert()` and fail the whole ingest instead of reaching the fallback.
        """
        if cls._converter_instance is None and cls.unavailable_reason is None:
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                from opencortyx.core.config import settings

                pdf = PdfPipelineOptions()
                pdf.do_ocr = bool(settings.DOCLING_OCR)
                pdf.do_table_structure = bool(settings.DOCLING_TABLE_STRUCTURE)
                cls._converter_instance = DocumentConverter(
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf)})
            except Exception as e:                   # noqa: BLE001 — see docstring
                cls.unavailable_reason = f"{type(e).__name__}: {e}"[:300]
                log.warning("docling.unavailable", error=cls.unavailable_reason,
                            action="using the built-in structural parser")
        return cls._converter_instance

''' + s[end:]

# 2. convert: record which engine read it; fall back cleanly.
start = s.index("        assembler = DynamicDoclingAssembler(doc_id=doc_id)\n        converter")
end = s.index("    @classmethod\n    def _convert_with_docling(")
s = s[:start] + '''        assembler = DynamicDoclingAssembler(doc_id=doc_id)
        filename = _effective_filename(content, filename)
        converter = cls.get_converter()

        if converter is not None:
            try:
                result = cls._convert_with_docling(converter, content, filename, assembler)
                result.engine = "docling"
                return result
            except Exception as exc:                 # noqa: BLE001 — the fallback still reads it
                log.warning("docling.conversion_error_falling_back", filename=filename,
                            error=f"{type(exc).__name__}: {exc}"[:300])
                assembler = DynamicDoclingAssembler(doc_id=doc_id)   # discard partial elements
                assembler.fallback_reason = f"{type(exc).__name__}: {exc}"[:300]
        else:
            assembler.fallback_reason = cls.unavailable_reason or "docling is not installed"

        result = cls._convert_fallback(content, filename, assembler)
        result.engine = "fallback"
        return result

''' + s[end:]

# 3. heading levels
swap('''                # Level for headings
                level = getattr(item, "level", None)
                if label == "title" and level is None:
                    level = 1
                elif label == "section_header" and level is None:
                    level = 2
''', '''                # Heading level. Docling numbers section headers from 1 and gives the title no
                # level; putting the title at 1 too made every top-level section its SIBLING, so the
                # title contained only the text before the first section. The title is 0.
                level = None
                if label == "title":
                    level = 0
                elif label == "section_header":
                    level = int(getattr(item, "level", None) or 1)
''')

# 4. tables searchable
swap('''                if label == "table":
                    grid = cls._extract_table_grid(item)
''', '''                if label == "table":
                    grid = cls._extract_table_grid(item)
                    if not text.strip() and grid:
                        # Docling leaves TableItem.text empty. Without this a table is a node with
                        # no text: never embedded, never matched, invisible to retrieval.
                        text = _grid_text(grid)
''')

# 5. fallback reads binary formats through the ingest parser
swap('''        text = content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content or "")
        lines = text.splitlines()''', '''        text = _readable_text(content, filename)
        lines = text.splitlines()''')

swap('''            else:
                # Standard paragraph
                assembler.add_element(
                    raw_label="paragraph",''', '''            else:
                # Prose, labelled "text" — Docling's own label — so a document read by the
                # fallback yields the same node types as one read by Docling.
                assembler.add_element(
                    raw_label="text",''')

s = s.rstrip() + '''


#: Containers, not text. Decoding their bytes as UTF-8 produced one node per line of PDF syntax —
#: 318 `Paragraph` nodes reading "xref", "endobj", "%%EOF" for a one-page contract.
_BINARY_EXTS = (".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".odt", ".rtf", ".epub")


def _looks_binary(content: bytes) -> bool:
    return content.startswith(b"%PDF") or content.startswith(b"PK\\x03\\x04") or b"\\x00" in content[:4096]


def _effective_filename(content: bytes | str, filename: str) -> str:
    """A name whose extension tells Docling what it holds.

    With no extension (a sample string from the Mapper's live preview) the old default was `.pdf`,
    so Docling was handed markdown that claimed to be a PDF and always failed.
    """
    if os.path.splitext(filename or "")[1]:
        return filename
    base = filename or "document"
    if isinstance(content, str) or not _looks_binary(content):
        return base + ".md"
    return base + ".pdf"


def _readable_text(content: bytes | str, filename: str) -> str:
    """Text for the structural fallback — through the ingest parser for binary formats."""
    if isinstance(content, str):
        return content
    if os.path.splitext(filename or "")[1].lower() in _BINARY_EXTS or _looks_binary(content):
        from opencortyx.ingest.formats.unstructured.documents import parse_content
        return str(parse_content(content, filename=filename).get("raw_text") or "")
    return content.decode("utf-8", errors="ignore")


def _grid_text(grid: list[list[str]]) -> str:
    return "\\n".join(" | ".join(str(c) for c in row) for row in grid if row)
'''
io.open(p, "w", encoding="utf-8", newline="\n").write(s)

# assembler: engine attributes + title level 0
a = "opencortyx/ingest/unstructured/docling/assembler.py"
t = io.open(a, encoding="utf-8").read()
old = '''        self.nodes: list[DocumentASTNode] = []
        self._seq = 0'''
assert old in t
t = t.replace(old, '''        self.nodes: list[DocumentASTNode] = []
        self._seq = 0
        #: "docling" or "fallback" once converted, and why the fallback ran. Recorded on the Source
        #: so a graph whose shape differs from a colleague's can be traced to the engine that read it.
        self.engine: str = ""
        self.fallback_reason: str = ""''', 1)
old = '''        is_heading = norm_label in ("title", "section_header", "heading") or level is not None
        actual_level = level if level is not None else (1 if norm_label == "title" else 2 if is_heading else None)'''
assert old in t
t = t.replace(old, '''        # Only headings open a section. Treating ANY element with a level as a heading let a leveled
        # list item or caption push itself onto the stack and adopt everything after it.
        is_heading = norm_label in ("title", "section_header", "heading")
        if is_heading:
            # Title is 0 so level-1 sections nest UNDER it rather than beside it.
            actual_level = level if level is not None else (0 if norm_label == "title" else 1)
        else:
            actual_level = None''', 1)
io.open(a, "w", encoding="utf-8", newline="\n").write(t)
print("patched")
