"""Markdown: headings, lists, pipe tables, fenced code and images (see flat.py, which reads both)."""
from __future__ import annotations

from typing import Optional

from ..assembler import LayoutAssembler
from .flat import FlatReader


class MarkdownReader:
    name = "markdown"
    extensions = ("md", "markdown", "mdx")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        from .native import front_matter
        meta, body = front_matter(content.decode("utf-8-sig", errors="ignore"))
        asm.metadata.update(meta)                       # front matter is metadata, not the first paragraph
        FlatReader().read_text(body, asm, markdown=True)
