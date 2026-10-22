"""Plain text and Markdown, read line by line into the layout AST.

Two modes on one walk. Markdown: `#` headings, `-`/`*`/`1.` list items, pipe tables, fenced code,
images. Plain text: paragraphs (lines up to a blank line), numbered section headings ("4.2.  Title")
and reStructuredText headings (a line underlined with = - ~ ^ ...).
"""
from __future__ import annotations

import re
from typing import Optional

from ..assembler import LayoutAssembler
from ..engine import grid_text, heading_levels

_FENCE = re.compile(r"^\s*(```|~~~)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*)$")
_IMAGE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)]*)\)\s*$")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?$")
#: A numbered heading in plain text: "4.2.  Title" on its own, short, not a sentence.
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)\.?\s{1,4}([A-Z][^\n]{1,90})$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")


class FlatReader:
    name = "text"
    extensions = ("txt", "text", "log", "rst", "tex", "csl", "hl7", "py", "js", "ts", "java", "c", "cpp",
                  "h", "go", "rs", "rb", "php", "sql", "sh", "ps1")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        text = content.decode("utf-8-sig", errors="ignore")
        self.read_text(text, asm, markdown=False, rst=filename.lower().endswith(".rst"))

    def read_text(self, text: str, asm: LayoutAssembler, *, markdown: bool, rst: bool = False,
                  page_no: int = 1) -> None:
        blocks = _blocks(text, markdown=markdown, rst=rst)
        heads = [(b[1], b[2]) for b in blocks if b[0] == "heading"]
        has_title = any(b[0] == "heading" and b[2] == 0 for b in blocks)
        levels = iter(heading_levels(heads, has_title) if not markdown else [h[1] for h in heads])
        for kind, body, extra in blocks:
            if kind == "heading":
                level = next(levels)
                asm.add_element(raw_label="title" if level == 0 else "section_header", text=body,
                                page_no=page_no, level=level)
            elif kind == "table":
                asm.add_element(raw_label="table", text=grid_text(extra), page_no=page_no, grid=extra)
            elif kind == "picture":
                asm.add_element(raw_label="picture", text=body, page_no=page_no, asset_reference=extra)
            else:
                asm.add_element(raw_label=kind, text=body, page_no=page_no)


def _blocks(text: str, *, markdown: bool, rst: bool) -> list[tuple[str, str, object]]:
    """(kind, text, extra) in reading order: heading (extra = level), text, list_item, code,
    table (extra = grid), picture (extra = source)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[tuple[str, str, object]] = []
    para: list[str] = []
    rst_order: list[str] = []

    def flush():
        if para:
            body = " ".join(p.strip() for p in para) if not _looks_preformatted(para) else "\n".join(para)
            if body.strip():
                out.append(("text", body.strip(), None))
            para.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if markdown and _FENCE.match(line):                                 # fenced code
            flush()
            fence = _FENCE.match(line).group(1)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            out.append(("code", "\n".join(body).strip("\n"), None))
            i += 1
            continue
        if not stripped:
            flush()
            i += 1
            continue
        if markdown:
            m = _MD_HEADING.match(stripped)
            if m:
                flush()
                hashes = len(m.group(1))
                # `#` is the title, `##` a level-1 section, `###` level 2: how Markdown was always read.
                out.append(("heading", m.group(2), 0 if hashes == 1 else hashes - 1))
                i += 1
                continue
            m = _IMAGE.match(stripped)
            if m:
                flush()
                out.append(("picture", m.group(1), m.group(2)))
                i += 1
                continue
            if stripped.startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1].strip()):
                flush()
                rows: list[list[str]] = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    if not _TABLE_SEP.match(lines[i].strip()):
                        rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                    i += 1
                out.append(("table", "", rows))
                continue
            m = _LIST.match(line)
            if m:
                flush()
                out.append(("list_item", m.group(1).strip(), None))
                i += 1
                continue
        else:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if rst and nxt and _RST_UNDERLINE.match(nxt) and len(nxt) >= len(stripped) * 0.6 and not para:
                char = nxt[0]
                if char not in rst_order:
                    rst_order.append(char)
                out.append(("heading", stripped, rst_order.index(char) + 1))
                i += 2
                continue
            # A numbered heading stands alone: a blank line follows it. "1. When applying the guideline"
            # continued on the next line is a numbered list item. reStructuredText marks headings by
            # underlining, so its numbers are always list items.
            m = None if rst else _NUMBERED_HEADING.match(stripped)
            if m and not para and not nxt and not stripped.endswith("."):
                flush()
                out.append(("heading", stripped, 1))
                i += 1
                continue
            m = _LIST.match(line)
            if m and not para:
                out.append(("list_item", m.group(1).strip(), None))
                i += 1
                continue
        para.append(line)
        i += 1
    flush()
    return out


def _looks_preformatted(lines: list[str]) -> bool:
    """Code or an ASCII figure: keep its line breaks rather than reflowing it into one paragraph."""
    indented = sum(1 for ln in lines if ln.startswith(("    ", "\t")))
    symbols = sum(1 for ln in lines if re.search(r"[{};=<>|+]{2,}|^\s*[+|]-", ln))
    return len(lines) > 1 and (indented == len(lines) and symbols > 0 or symbols >= len(lines) / 2)
