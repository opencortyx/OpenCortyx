"""EPUB: a zip of XHTML chapters. Read in the book's reading order (the OPF spine), each chapter
through the HTML reader, the chapter number as its page."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from typing import Optional

from ..assembler import LayoutAssembler
from .html import HTMLTagReader


class EpubReader:
    name = "epub"
    extensions = ("epub",)

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        html = HTMLTagReader()
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            from .native import opf_meta
            asm.metadata.update(opf_meta(_opf(z)))
            for page_no, name in enumerate(_spine(z), start=1):
                if max_pages and page_no > max_pages:
                    break
                html.read_html(z.read(name), asm, page_no=page_no)


def _opf(z: zipfile.ZipFile) -> str:
    """The package document's text, or "" without one."""
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore") if "META-INF/container.xml" in z.namelist() else ""
    m = re.search(r'full-path="([^"]+)"', container)
    return z.read(m.group(1)).decode("utf-8", "ignore") if m and m.group(1) in z.namelist() else ""


def _spine(z: zipfile.ZipFile) -> list[str]:
    """The chapter files in reading order, or every XHTML file in name order without an OPF."""
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore") if "META-INF/container.xml" in z.namelist() else ""
    m = re.search(r'full-path="([^"]+)"', container)
    if m and m.group(1) in z.namelist():
        opf_path = m.group(1)
        opf = z.read(opf_path).decode("utf-8", "ignore")
        items = dict(re.findall(r'<item\b[^>]*\bid="([^"]+)"[^>]*\bhref="([^"]+)"', opf))
        items.update({i: h for h, i in re.findall(r'<item\b[^>]*\bhref="([^"]+)"[^>]*\bid="([^"]+)"', opf)})
        base = posixpath.dirname(opf_path)
        order = [posixpath.normpath(posixpath.join(base, items[i]))
                 for i in re.findall(r'<itemref\b[^>]*\bidref="([^"]+)"', opf) if i in items]
        order = [n for n in order if n in z.namelist()]
        if order:
            return order
    return sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
