"""Jupyter notebooks: Markdown cells read as Markdown, code cells as code, text outputs as text."""
from __future__ import annotations

import json
from typing import Optional

from ..assembler import LayoutAssembler
from .flat import FlatReader


class IPYNBReader:
    name = "ipynb"
    extensions = ("ipynb",)

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        nb = json.loads(content.decode("utf-8-sig", errors="ignore"))
        from .native import notebook_meta
        asm.metadata.update(notebook_meta(nb))
        flat = FlatReader()
        for cell in nb.get("cells") or []:
            source = cell.get("source") or ""
            source = "".join(source) if isinstance(source, list) else str(source)
            if not source.strip():
                continue
            if cell.get("cell_type") == "markdown":
                flat.read_text(source, asm, markdown=True)
            elif cell.get("cell_type") == "code":
                asm.add_element(raw_label="code", text=source.strip("\n"))
                for out in cell.get("outputs") or []:
                    text = out.get("text") or (out.get("data") or {}).get("text/plain") or ""
                    text = "".join(text) if isinstance(text, list) else str(text)
                    if text.strip():
                        asm.add_element(raw_label="text", text=text.strip()[:5000])
            else:
                asm.add_element(raw_label="text", text=source.strip())
