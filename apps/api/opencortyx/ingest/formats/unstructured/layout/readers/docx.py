"""Word (.docx/.docm) with python-docx: what the author marked is what is read. Paragraph styles give
the title and heading levels, numbering gives list items, tables their grids, the section headers and
footers the page_header/page_footer elements. Body paragraphs and tables in document order."""
from __future__ import annotations

import io
import re
from typing import Optional

from ..assembler import LayoutAssembler
from ..engine import grid_text

_HEADING = re.compile(r"^heading\s*(\d)$", re.I)


class DocxReader:
    name = "docx"
    extensions = ("docx", "docm", "dotx")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        doc = docx.Document(io.BytesIO(content))

        seen: set[str] = set()
        for section in doc.sections:                    # running headers, once each
            for p in section.header.paragraphs:
                text = p.text.strip()
                if text and text not in seen:
                    seen.add(text)
                    asm.add_element(raw_label="page_header", text=text)

        for block in doc.element.body.iterchildren():
            tag = block.tag.rsplit("}", 1)[-1]
            if tag == "p":
                self._paragraph(Paragraph(block, doc), asm)
            elif tag == "tbl":
                table = Table(block, doc)
                grid = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                grid = [_dedupe_merged(r) for r in grid if any(r)]
                if grid:
                    asm.add_element(raw_label="table", text=grid_text(grid), grid=grid)

        seen.clear()
        for section in doc.sections:                    # running footers, once each
            for p in section.footer.paragraphs:
                text = p.text.strip()
                if text and text not in seen:
                    seen.add(text)
                    asm.add_element(raw_label="page_footer", text=text)

    @staticmethod
    def _paragraph(p, asm: LayoutAssembler) -> None:
        text = p.text.strip()
        style = (p.style.name if p.style is not None else "") or ""
        if not text:
            for blip in p._p.xpath(".//pic:cNvPr"):        # an image paragraph: its description
                descr = (blip.get("descr") or blip.get("name") or "").strip()
                asm.add_element(raw_label="picture", text=descr)
            return
        if style.lower() == "title":
            asm.add_element(raw_label="title", text=text, level=0)
            return
        m = _HEADING.match(style)
        if m:
            asm.add_element(raw_label="section_header", text=text, level=max(1, int(m.group(1))))
            return
        numbered = p._p.pPr is not None and p._p.pPr.numPr is not None
        if numbered or style.lower().startswith("list"):
            asm.add_element(raw_label="list_item", text=text)
        elif style.lower() == "caption":
            asm.add_element(raw_label="caption", text=text)
        else:
            asm.add_element(raw_label="text", text=text)


def _dedupe_merged(row: list[str]) -> list[str]:
    """python-docx repeats a merged cell's text in every column it spans; keep it once."""
    out: list[str] = []
    for i, cell in enumerate(row):
        out.append("" if i and cell and cell == row[i - 1] else cell)
    return out
