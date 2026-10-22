"""HTML by its tags: h1 is the title, h2-h6 section headers one level down, p text, li list items,
pre code, table a grid, img a picture, figcaption a caption. The body is walked in document order;
a text-level element is read whole and not descended into, so no text is read twice."""
from __future__ import annotations

from typing import Optional

from ..assembler import LayoutAssembler
from ..engine import grid_text

_SKIP = {"script", "style", "noscript", "template", "head", "svg", "button", "form", "select", "iframe"}
_HEADINGS = {"h1": 0, "h2": 1, "h3": 2, "h4": 3, "h5": 4, "h6": 5}
_TEXT_BLOCKS = {"p": "text", "blockquote": "text", "dt": "text", "dd": "text", "address": "text",
                "figcaption": "caption", "caption": "caption", "pre": "code"}


class HTMLTagReader:
    name = "html"
    extensions = ("html", "htm", "xhtml", "xht")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        self.read_html(content, asm)

    def read_html(self, html: bytes | str, asm: LayoutAssembler, page_no: int = 1) -> None:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        root = soup.body or soup
        _walk(root, asm, page_no)


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def _walk(el, asm: LayoutAssembler, page_no: int) -> None:
    from bs4 import NavigableString, Tag
    for child in el.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in ("Comment", "Doctype", "Declaration", "ProcessingInstruction"):
                continue
            text = " ".join(str(child).split())
            if text and el.name not in ("ul", "ol", "table", "tr", "thead", "tbody"):
                asm.add_element(raw_label="text", text=text, page_no=page_no)
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in _SKIP:
            continue
        if name in _HEADINGS:
            text = _text(child)
            if text:
                level = _HEADINGS[name]
                asm.add_element(raw_label="title" if level == 0 else "section_header", text=text,
                                page_no=page_no, level=level)
        elif name in _TEXT_BLOCKS:
            text = child.get_text("\n") if name == "pre" else _text(child)
            if text.strip():
                asm.add_element(raw_label=_TEXT_BLOCKS[name], text=text.strip(), page_no=page_no)
        elif name == "li":
            nested = [c for c in child.find_all(["ul", "ol"], recursive=False)]
            for n in nested:
                n.extract()
            text = _text(child)
            if text:
                asm.add_element(raw_label="list_item", text=text, page_no=page_no)
            for n in nested:
                _walk(n, asm, page_no)
        elif name == "table":
            grid = [[_text(c) for c in row.find_all(["th", "td"], recursive=False)]
                    for row in child.find_all("tr")]
            grid = [r for r in grid if any(r)]
            if grid:
                asm.add_element(raw_label="table", text=grid_text(grid), page_no=page_no, grid=grid)
        elif name == "img":
            alt = (child.get("alt") or child.get("title") or "").strip()
            asm.add_element(raw_label="picture", text=alt, page_no=page_no,
                            asset_reference=child.get("src") or None)
        elif name == "br" or name == "hr":
            continue
        else:
            _walk(child, asm, page_no)
