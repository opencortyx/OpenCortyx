"""Read a document into the layout AST with the reader for its format.

One entry point for every caller (ingestion, the Add data preview, the Mapper, text extraction):
`LayoutEngine.convert(content, filename)`. The reader is chosen by extension (layout/readers/); a
reader that fails hands the document to the flat structural reader, and the assembler records which
one read it and why, so a document whose graph looks different can be traced to its reader.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from opencortyx.core.observability import get_logger

from .assembler import LayoutAssembler

log = get_logger(__name__)

#: Containers, not text: decoding their bytes as UTF-8 produced one node per line of file syntax
#: ("xref", "endobj") for a one-page PDF.
_BINARY_EXTS = (".pdf", ".docx", ".doc", ".docm", ".pptx", ".ppt", ".pptm", ".xlsx", ".xls", ".odt",
                ".odp", ".ods", ".rtf", ".epub", ".pages", ".key", ".numbers")


class LayoutEngine:
    """Reads documents into a `LayoutAssembler`, one reader per format."""

    @classmethod
    def convert(cls, content: bytes | str, filename: str = "document.md", doc_id: Optional[str] = None,
                max_pages: Optional[int] = None) -> LayoutAssembler:
        """Read `content` with the reader for `filename`'s format.

        `max_pages` reads only the first pages of a paged format, for a preview, which must not cost
        what reading a 300-page PDF costs. Ingestion leaves it unset and reads everything."""
        import hashlib

        from .readers import reader_for
        if not doc_id:
            raw = content if isinstance(content, bytes) else content.encode("utf-8")
            doc_id = f"doc_{hashlib.sha256(raw).hexdigest()[:12]}"
        filename = effective_filename(content, filename)
        reader = reader_for(filename)
        assembler = LayoutAssembler(doc_id=doc_id, max_chars=_max_chars())
        try:
            reader.read(_as_bytes(content), filename, assembler, max_pages=max_pages)
            if not assembler.nodes and not isinstance(content, str) and _looks_binary(_as_bytes(content)):
                raise ValueError("no text could be read from the file")
            assembler.engine = reader.name
            return assembler
        except Exception as exc:                                  # noqa: BLE001 — the fallback still reads it
            log.warning("layout.reader_failed_falling_back", filename=filename, reader=reader.name,
                        error=f"{type(exc).__name__}: {exc}"[:300])
            from .readers.flat import FlatReader
            fallback = LayoutAssembler(doc_id=doc_id, max_chars=_max_chars())
            fallback.fallback_reason = f"{reader.name}: {type(exc).__name__}: {exc}"[:300]
            text = content if isinstance(content, str) else _readable_text(content, filename, reader.name)
            FlatReader().read_text(text, fallback, markdown=True)
            fallback.engine = "fallback"
            return fallback


def _max_chars() -> int:
    """The Chunk size setting: the longest paragraph kept as one node."""
    from opencortyx.core.config import runtime
    return runtime.chunk_window_chars()


def _as_bytes(content: bytes | str) -> bytes:
    return content if isinstance(content, bytes) else content.encode("utf-8")


def _looks_binary(content: bytes) -> bool:
    return content.startswith(b"%PDF") or content.startswith(b"PK\x03\x04") or b"\x00" in content[:4096]


def effective_filename(content: bytes | str, filename: str) -> str:
    """A name whose extension says what the content is. A sample string from the Mapper's live preview
    has no name; it is Markdown, not a PDF."""
    if os.path.splitext(filename or "")[1]:
        return filename
    base = filename or "document"
    if isinstance(content, str) or not _looks_binary(content):
        return base + ".md"
    return base + ".pdf"


def _readable_text(content: bytes, filename: str, failed_reader: str) -> str:
    """Text for the fallback when a reader failed: never the container bytes of a binary format."""
    if os.path.splitext(filename or "")[1].lower() in _BINARY_EXTS or _looks_binary(content):
        raise ValueError(f"the {failed_reader} reader could not read '{filename}', and its bytes are not text")
    return content.decode("utf-8", errors="ignore")


def grid_text(grid: list[list[str]]) -> str:
    """A table's text, row by row: what a table node is embedded and matched by."""
    return "\n".join(" | ".join(str(c) for c in row) for row in grid if row)


#: A section number at the start of a heading: "2", "2.", "2.3", "2.3.1 ".
_SECTION_NUMBER = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")


def heading_levels(heads: list[tuple[str, int]], has_title: bool) -> list[int]:
    """Levels for a document's headings, given as (text, level the reader found) in order.

    A paged format has no heading levels of its own; a reader infers them from font sizes, which
    put "1.1 First week" beside "1. Onboarding" rather than inside it whenever the two are set alike.
    Where the reader found level 1:

      - a numbered heading takes its depth from the number ("2" is 1, "2.3" is 2, "2.3.1" is 3);
      - an unnumbered heading that opens a document whose sections are numbered, with no title of
        its own, is the title (level 0).

    Anything else keeps its level."""
    numbered = [i for i, (text, _) in enumerate(heads) if _SECTION_NUMBER.match(text or "")]
    out: list[int] = []
    for i, (text, given) in enumerate(heads):
        level = given
        if given == 1:
            m = _SECTION_NUMBER.match(text or "")
            if m:
                level = len(m.group(1).split("."))
            elif i == 0 and not has_title and numbered and numbered[0] > 0:
                level = 0
        out.append(level)
    return out
