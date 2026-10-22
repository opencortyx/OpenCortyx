"""One reader per document format, chosen by extension.

A reader adds a document's elements, in reading order, to a `LayoutAssembler`, using the shared
labels (layout/schemas.py: title, section_header with a level, text, list_item, table with its grid,
picture, caption, code, page_header, page_footer). Which reader read a file changes how well its
structure is found, never the shape of what comes out.

  PDFReader          .pdf                         LiteParse (PDFium; OCR for scanned pages)
  ImageReader        .png .jpg .tiff ...          LiteParse OCR
  ConvertedReader    .doc .ppt .odt .rtf* ...     LibreOffice to PDF, then LiteParse
  DocxReader         .docx .docm                  python-docx: styles, lists, tables, headers
  PptxReader         .pptx .pptm                  python-pptx: slide titles, text, tables
  HTMLTagReader      .html .htm .xhtml            BeautifulSoup: h1-h6, p, li, pre, table, img
  MarkdownReader     .md .markdown                Markdown structure
  RTFReader          .rtf                         striprtf, then the flat reader
  EpubReader         .epub                        each chapter through the HTML reader
  IPYNBReader        .ipynb                       Markdown cells, code cells, text outputs
  PandasExcelReader  .xlsx .xls (as a document)   each sheet a table
  FlatReader         .txt .log .rst .tex, code    paragraphs, numbered and underlined headings

XML is not here: it is records, read by the semi-structured walker (formats/semistructured/).
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

from ..assembler import LayoutAssembler


class Reader(Protocol):
    name: str
    extensions: tuple[str, ...]

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None: ...


def _readers() -> list:
    from .docx import DocxReader
    from .epub import EpubReader
    from .excel import PandasExcelReader
    from .flat import FlatReader
    from .html import HTMLTagReader
    from .ipynb import IPYNBReader
    from .markdown import MarkdownReader
    from .pdf import ConvertedReader, ImageReader, PDFReader
    from .pptx import PptxReader
    from .rtf import RTFReader
    return [PDFReader(), ImageReader(), ConvertedReader(), DocxReader(), PptxReader(), HTMLTagReader(),
            MarkdownReader(), RTFReader(), EpubReader(), IPYNBReader(), PandasExcelReader(), FlatReader()]


_BY_EXT: dict[str, Reader] = {}


def _index() -> dict[str, Reader]:
    if not _BY_EXT:
        for reader in _readers():
            for ext in reader.extensions:
                _BY_EXT.setdefault(ext, reader)
    return _BY_EXT


def reader_for(filename: str) -> Reader:
    """The reader for `filename`'s extension; the flat text reader for anything else."""
    ext = os.path.splitext(filename or "")[1].lower().lstrip(".")
    index = _index()
    return index.get(ext) or index["txt"]


def supported_extensions() -> set[str]:
    return set(_index())
