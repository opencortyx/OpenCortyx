"""Rich Text Format: the text through striprtf, then read as plain text paragraphs."""
from __future__ import annotations

from typing import Optional

from ..assembler import LayoutAssembler
from .flat import FlatReader


class RTFReader:
    name = "rtf"
    extensions = ("rtf",)

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        from striprtf.striprtf import rtf_to_text
        text = rtf_to_text(content.decode("utf-8", errors="ignore"), errors="ignore")
        FlatReader().read_text(text, asm, markdown=False)
