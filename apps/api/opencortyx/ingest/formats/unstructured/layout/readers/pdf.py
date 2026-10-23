"""Paged formats through LiteParse: PDFs natively (PDFium), images by OCR (Tesseract), and Office
or other formats it converts to PDF first (LibreOffice).

LiteParse classifies each page's text into blocks in reading order: headings (with a level), list
items (with a marker), tables (with cells), paragraphs. Mapped to the layout labels:

  heading            -> section_header, or title for the document's title (engine.heading_levels)
  marker             -> list_item
  rows               -> table, with its grid
  repeated top/bottom text -> page_header / page_footer
  anything else      -> text

Running headers and footers are not marked by LiteParse; text in the top or bottom band of the page
that recurs across pages (numbers ignored, so "Page 2" and "Page 3" match) is one.
"""
from __future__ import annotations

import os
import re
import tempfile
import threading
from typing import Optional

from opencortyx.core.observability import get_logger

from ..assembler import LayoutAssembler
from ..engine import grid_text, heading_levels

log = get_logger(__name__)

#: The share of the page height, top and bottom, searched for running headers and footers. Wide
#: (an RFC's footer sits at 86% of the page) because only text that recurs across pages counts.
_BAND = 0.15
_lock = threading.Lock()


def _parser(ocr: bool, max_pages: Optional[int]):
    from liteparse import LiteParse
    return LiteParse(ocr_enabled=ocr, extract_blocks=True, keep_headers_footers=True, quiet=True,
                     max_pages=max_pages or None)


def _ocr_enabled() -> bool:
    from opencortyx.core.config import runtime
    return runtime.pdf_ocr()


def read_paged(content: bytes, filename: str, asm: LayoutAssembler, *, max_pages: Optional[int],
               ocr: bool) -> None:
    """Parse `content` (named `filename`, whose extension LiteParse reads by) into `asm`."""
    suffix = os.path.splitext(filename)[1] or ".pdf"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        with _lock:                                    # PDFium serialises parses within a process
            result = _parser(ocr, max_pages).parse(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    _record_meta(result, asm)
    _emit(result.pages or [], asm)


def _record_meta(result, asm: LayoutAssembler) -> None:
    """The PDF's document information, when it has any (title, author, dates). The creating tool
    (`creator`, `producer`) says nothing about the document and is not kept."""
    from .native import put
    info = getattr(result, "doc_meta", None)
    if info is None:
        return
    for attr, name in (("title", "title"), ("author", "author"), ("subject", "subject"),
                       ("keywords", "keywords"), ("creation_date", "created"), ("mod_date", "modified")):
        put(asm.metadata, name, getattr(info, attr, None))


def _norm(text: str) -> str:
    return re.sub(r"\d+", "#", " ".join((text or "").lower().split()))


def _box(b) -> dict:
    r = getattr(b, "bbox", None)
    if r is None:
        return {}
    x, y = float(getattr(r, "x", 0) or 0), float(getattr(r, "y", 0) or 0)
    w, h = float(getattr(r, "width", 0) or 0), float(getattr(r, "height", 0) or 0)
    return {"x_min": x, "y_min": y, "x_max": x + w, "y_max": y + h}


def _furniture(pages) -> dict:
    """{(page index, block index): "page_header" | "page_footer"} for repeated band text."""
    seen: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for pi, page in enumerate(pages):
        height = float(getattr(page, "height", 0) or 0)
        if height <= 0:
            continue
        for bi, b in enumerate(page.blocks or []):
            box = _box(b)
            if not box or not (b.text or "").strip():
                continue
            band = ("page_header" if box["y_max"] <= height * _BAND
                    else "page_footer" if box["y_min"] >= height * (1 - _BAND) else None)
            if band:
                seen.setdefault((band, _norm(b.text)), []).append((pi, bi))
    need = max(2, int(0.3 * len(pages)))
    return {spot: band for (band, _), spots in seen.items() if len(spots) >= need for spot in spots}


def _emit(pages, asm: LayoutAssembler) -> None:
    furniture = _furniture(pages)
    blocks = [(pi, bi, page, b) for pi, page in enumerate(pages) for bi, b in enumerate(page.blocks or [])]
    is_heading = {(pi, bi): (b.level is not None and not b.marker and not b.rows
                             and (pi, bi) not in furniture and bool((b.text or "").strip()))
                  for pi, bi, _, b in blocks}
    heads = [((b.text or "").strip(), max(1, int(b.level))) for pi, bi, _, b in blocks if is_heading[(pi, bi)]]
    levels = iter(heading_levels(heads, has_title=False))
    for pi, bi, page, b in blocks:
        page_no = int(getattr(page, "page_num", pi + 1) or pi + 1)
        text = (b.text or "").strip()
        box = _box(b)
        if (pi, bi) in furniture:
            asm.add_element(raw_label=furniture[(pi, bi)], text=text, page_no=page_no, bbox=box)
        elif is_heading[(pi, bi)]:
            level = next(levels)
            asm.add_element(raw_label="title" if level == 0 else "section_header", text=text,
                            page_no=page_no, bbox=box, level=level)
        elif b.rows:
            grid = [[(getattr(c, "text", "") or "").strip() for c in row] for row in b.rows]
            grid = [r for r in grid if any(r)]
            if grid:
                asm.add_element(raw_label="table", text=grid_text(grid), page_no=page_no, bbox=box, grid=grid)
        elif not text:
            continue
        elif b.marker:
            asm.add_element(raw_label="list_item", text=text, page_no=page_no, bbox=box)
        elif str(getattr(b, "format", "") or "").lower() == "code":
            asm.add_element(raw_label="code", text=text, page_no=page_no, bbox=box)
        elif str(getattr(b, "format", "") or "").lower() in ("figure", "image", "picture", "chart"):
            asm.add_element(raw_label="picture", text=text, page_no=page_no, bbox=box)
        else:
            asm.add_element(raw_label="text", text=text, page_no=page_no, bbox=box)


class PDFReader:
    name = "pdf"
    extensions = ("pdf",)

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        read_paged(content, filename, asm, max_pages=max_pages, ocr=_ocr_enabled())


class ImageReader:
    """A scan or a photo of a page: its text by OCR, its layout from where the text sits."""
    name = "image"
    extensions = ("png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "webp", "svg")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        read_paged(content, filename, asm, max_pages=max_pages, ocr=True)


class ConvertedReader:
    """Formats LiteParse converts to PDF with LibreOffice before reading: legacy and OpenDocument
    office files, Apple iWork. Needs LibreOffice in the image (it is, in the release images)."""
    name = "converted"
    extensions = ("doc", "dot", "ppt", "pps", "odt", "ott", "odp", "ods", "pages", "key", "numbers", "wpd")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        try:
            read_paged(content, filename, asm, max_pages=max_pages, ocr=_ocr_enabled())
        except Exception as e:                                   # noqa: BLE001 — say what is missing
            if "libreoffice" in str(e).lower() or "soffice" in str(e).lower():
                raise RuntimeError(f"reading '{os.path.splitext(filename)[1]}' files needs LibreOffice, "
                                   "which this image does not have") from e
            raise
