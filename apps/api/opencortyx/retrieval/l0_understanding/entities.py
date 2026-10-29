"""L0 — Entity mention detection: which spans of the question name something in the graph.

This finds MENTIONS, not entities. Binding a mention to a canonical node is resolution (L1), and
keeping the two apart matters: a mention that resolves to nothing is still a useful lexical and
semantic signal, while a mention wrongly bound to the wrong node poisons every stage after it.

No gazetteer, no type list. A gazetteer built from the current corpus stops working the moment the
corpus changes, and a type list ("Person, Company, Country") assumes a schema this module has no
business knowing. Detection is structural — capitalisation, quoting, identifier shape, possessive
and prepositional frames — so it degrades gracefully on unfamiliar data instead of silently
finding nothing.
"""
from __future__ import annotations

import re

from opencortyx.retrieval.contracts import EntityMention

#: Words that are capitalised for reasons other than being a name.
_STOP_CAPS = {
    "what", "when", "where", "which", "who", "whom", "whose", "why", "how", "is", "are", "was",
    "were", "do", "does", "did", "the", "a", "an", "and", "or", "but", "if", "then", "than",
    "this", "that", "these", "those", "there", "here", "it", "its", "his", "her", "their",
    "would", "could", "should", "will", "shall", "can", "may", "might", "must", "have", "has",
    "had", "based", "tell", "find", "give", "list", "show", "in", "on", "at", "of", "for", "to",
    "from", "with", "by", "about", "after", "before", "during", "between", "both", "besides",
    "any", "all", "some", "most", "many", "much",
}

#: Identifier-shaped tokens: codes and references that are certainly names of things.
_IDENTIFIER = re.compile(r"\b([A-Z]{2,}[-_]?\d{2,}[-\w]*|\b\d{6,}\b)")

#: Explicitly quoted spans are always mentions.
_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”'‘’]{2,60})[\"“”'‘’]")

#: A run of capitalised words, allowing internal lowercase title/origin connectors ("Bank of England", "Ludwig van Beethoven")
#: and numbers that are part of the name ("RFC 9562", "PEP 8", "Windows 11").
_CAP_RUN = re.compile(r"\b([A-Z][\w.\-]*(?:\s+(?:of|de|van|der|von)\s+[A-Z][\w.\-]*|"
                      r"\s+[A-Z][\w.\-]*|\s+\d[\w.\-]*)*)\b")

#: "<X>'s <attribute>" — X is a mention and the attribute names the field being asked for.
_POSSESSIVE = re.compile(r"\b([A-Z][\w\-]+)(?:'s|’s)\s+(\w+)")


def _clean(span: str) -> str:
    return span.strip(" \t.,;:!?").strip()


def detect_mentions(query: str) -> list[EntityMention]:
    """Ordered, de-duplicated mentions. Overlapping candidates keep the longer span."""
    q = query or ""
    found: list[EntityMention] = []
    spans: list[tuple[int, int]] = []

    def claim(a: int, b: int) -> bool:
        for s, e in spans:
            if not (b <= s or a >= e):
                return False
        spans.append((a, b))
        return True

    for m in _IDENTIFIER.finditer(q):
        if claim(*m.span()):
            found.append(EntityMention(text=_clean(m.group(1)), start=m.start(), end=m.end(),
                                       hint_type="identifier"))
    for m in _QUOTED.finditer(q):
        if claim(*m.span()):
            found.append(EntityMention(text=_clean(m.group(1)), start=m.start(1), end=m.end(1)))

    # Longest-first so "Bank of England" wins over "Bank".
    caps = sorted(_CAP_RUN.finditer(q), key=lambda m: len(m.group(1)), reverse=True)
    for m in caps:
        span = _clean(m.group(1))
        if not span or span.lower() in _STOP_CAPS:
            continue
        # A single capitalised word at position 0 is usually just sentence case.
        if m.start() == 0 and " " not in span and len(span.split()) == 1:
            lowered = span.lower()
            if lowered in _STOP_CAPS:
                continue
        # Trim stop-words from the EDGES only. Filtering them everywhere turns
        # "Bank of England" into "Bank England" - the connector is part of the name.
        parts = span.split()
        while parts and parts[0].lower() in _STOP_CAPS:
            parts.pop(0)
        while parts and parts[-1].lower() in _STOP_CAPS:
            parts.pop()
        if not parts:
            continue
        span = " ".join(parts)
        if len(span) < 2 or not any(c.isalpha() for c in span):
            continue                    # "In 2024": a year after a stop-word is not a name
        if claim(m.start(), m.end()):
            found.append(EntityMention(text=span, start=m.start(), end=m.end()))
    found.sort(key=lambda e: e.start)
    seen: set[str] = set()
    out: list[EntityMention] = []
    for e in found:
        key = e.text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def detect_attribute_targets(query: str) -> list[str]:
    """Field names the question asks FOR — "John's email" wants `email`.

    Used to weight attribute embedding surfaces: a question naming a field should search that
    field's surface hardest. The word is taken from the question as-is; matching it to a real
    attribute happens against the corpus, never against a declared schema.
    """
    q = query or ""
    out: list[str] = []
    for m in _POSSESSIVE.finditer(q):
        w = m.group(2).lower().strip()
        if w and w not in _STOP_CAPS and len(w) > 2:
            out.append(w)
    m = re.match(r"^\s*what\s+(?:is|are|was|were)\s+(?:the\s+)?([\w ]{2,30}?)\s+(?:of|for)\b",
                 q, re.I)
    if m:
        w = m.group(1).strip().lower()
        if w and w not in _STOP_CAPS:
            out.append(w)
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w)
            uniq.append(w)
    return uniq
