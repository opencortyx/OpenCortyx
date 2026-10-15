"""Split a document's text into chunks: sliding window, sentences, paragraphs or markdown headings."""
import re as _re
from typing import Any, Dict, List

def chunk_text(text: str, chunk_size: int = 2000, overlap: int = 200) -> List[str]:
    """Sliding-window chunker (the v2 default)."""
    if not text:
        return []
    out, start, step = [], 0, max(1, chunk_size - overlap)
    while start < len(text):
        out.append(text[start:start + chunk_size])
        start += step
    return out


# heading line for markdown-heading chunking: '#'..'######' then text (ATX headings)
_MD_HEADING_RE = _re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", _re.MULTILINE)
_SENTENCE_RE = _re.compile(r"[^.!?]*[.!?]+(?:['\")\]]+)?|\S[^.!?]*$")


def _pack(units: List[str], chunk_size: int, sep: str = " ") -> List[str]:
    """Greedily pack whole units (sentences/paragraphs) into chunks up to chunk_size, never
    splitting a unit. A single unit longer than chunk_size becomes its own (over-long) chunk."""
    out, buf = [], ""
    for u in units:
        u = u.strip()
        if not u:
            continue
        if buf and len(buf) + len(sep) + len(u) > chunk_size:
            out.append(buf)
            buf = u
        else:
            buf = f"{buf}{sep}{u}" if buf else u
    if buf:
        out.append(buf)
    return out


def chunk_document(text: str, strategy: str = "sliding_window",
                   chunk_size: int = 2000, overlap: int = 200) -> List[Dict[str, Any]]:
    """Strategy-aware chunker. Returns a list of {"text", "attrs"} — `attrs` carries per-chunk
    metadata the strategy discovered (e.g. `heading` for markdown-heading), which ingestion can
    map onto declared chunk attributes. `chunk_text` remains the raw sliding-window primitive."""
    if not text:
        return []
    strat = (strategy or "sliding_window").strip().lower()

    if strat in ("sentence", "sentences"):
        sents = [m.group(0) for m in _SENTENCE_RE.finditer(text)]
        return [{"text": c, "attrs": {}} for c in _pack(sents, chunk_size)]

    if strat in ("paragraph", "paragraphs"):
        paras = _re.split(r"\n\s*\n", text)
        # merge tiny adjacent paragraphs up to chunk_size, but keep blank-line structure as the unit
        return [{"text": c, "attrs": {}} for c in _pack([p for p in paras if p.strip()],
                                                        chunk_size, sep="\n\n")]

    if strat in ("markdown_heading", "markdown", "heading"):
        marks = list(_MD_HEADING_RE.finditer(text))
        if not marks:                              # no headings → fall back to paragraphs
            return chunk_document(text, "paragraph", chunk_size, overlap)
        out: List[Dict[str, Any]] = []
        preface = text[:marks[0].start()].strip()  # text before the first heading
        if preface:
            out.append({"text": preface, "attrs": {}})
        for i, m in enumerate(marks):
            heading = m.group(2).strip()
            body_start = m.end()
            body_end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
            section = text[body_start:body_end].strip()
            full = f"{m.group(0).strip()}\n{section}".strip()
            attrs = {"heading": heading, "level": len(m.group(1))}
            if len(full) <= chunk_size:
                out.append({"text": full, "attrs": attrs})
            else:                                  # over-long section → sub-window, keep the heading
                for w in chunk_text(section, chunk_size=chunk_size, overlap=overlap):
                    out.append({"text": w, "attrs": dict(attrs)})
        return out

    # default: sliding window (strat == "sliding_window" or anything unknown, incl. "semantic"
    # which resolves to a server-side embedding chunker elsewhere; sliding is the safe fallback)
    return [{"text": c, "attrs": {}} for c in chunk_text(text, chunk_size=chunk_size, overlap=overlap)]
