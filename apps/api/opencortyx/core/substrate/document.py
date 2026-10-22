"""What a node SAYS — one definition, shared by everything that reads or writes it.

There used to be four answers to this question and they did not agree:

  * the ingest writers built a string and stored it as `properties.__text`;
  * `retrieve.patterns.node_text` re-derived its own string from `properties` and never read
    `__text` at all;
  * the FTS index searched `coalesce(text, __context, __text, label)`;
  * the L1 candidate surfaces read `properties.text` and nothing else, so every node whose content
    lived in `__text` — which is every structured row and every walked JSON node — arrived at the
    context builder carrying only its own label and was dropped there as "a name, not a statement".

Four definitions of one concept is how a node can be searchable and unreadable at the same time.
This module is the definition. `document()` is the rule; `__text` is that rule MATERIALISED at write
time so SQL can index it, not a second opinion about what the node says.

The rule has one branch, and it is a statement of authority rather than a fallback chain: an
AUTHORED `text` is what the node says, because a human or an extractor wrote it deliberately.
Absent that, the node still says something — its type, its name and its own scalar attributes are
text, and a node holding them is answerable whether or not anyone ever wrote a description for it.
"""
from __future__ import annotations

DERIVED_TEXT_SKIP = {"text", "embedding", "source", "source_ref", "document_id", "collection_id",
                     "created_date", "modified_date", "checksum", "position", "offset", "index"}

#: Where the materialised form of `document()` is kept on a node.
STORED_KEY = "__text"

DEFAULT_LIMIT = 1500


def document(properties: dict | None, label: str = "", type_: str = "",
             limit: int = DEFAULT_LIMIT) -> str:
    """The node's content as a single string. Never empty when the node carries any meaning."""
    props = properties or {}
    authored = str(props.get("text") or "").strip()
    if authored:
        return authored[:limit]
    label = str(label or "").strip()
    type_ = str(type_ or "").strip()
    head = f"{type_} {label}".strip() if label and label != type_ else (type_ or label)
    pairs = [f"{k}={v}" for k, v in props.items()
             if not str(k).startswith("__") and k not in DERIVED_TEXT_SKIP
             and isinstance(v, (str, int, float, bool)) and str(v).strip()]
    return ". ".join(p for p in (head, "; ".join(pairs)) if p)[:limit]


def from_row(properties: dict | None, label: str = "", type_: str = "",
             limit: int = DEFAULT_LIMIT) -> str:
    """`document()` for a node read back out of SQL, where the stored form is already present.

    Readers take the stored value when there is one: it was produced by `document()` at write time
    from the properties as they were then, and re-deriving would silently disagree with whatever the
    FTS index actually matched on. When it is absent — an older row, or a writer that has not been
    through this module — the rule still applies and the text is derived, so no node is ever
    reduced to its label just because nobody materialised it.
    """
    props = properties or {}
    stored = str(props.get(STORED_KEY) or "").strip()
    authored = str(props.get("text") or "").strip()
    if authored:
        return authored[:limit]
    if stored:
        return stored[:limit]
    return document(props, label, type_, limit)


#: Property keys that are machinery rather than content.
_INTERNAL_PREFIXES = ("__",)
_INTERNAL_SUFFIXES = ("_id", "_date")
_INTERNAL_EXACT = {"id", "source", "created_date", "modified_date", "superseded", "model", "extractor"}
#: Shorter than this carries no query match; longer is a document, not an attribute.
ATTR_MIN_CHARS = 2
ATTR_MAX_CHARS = 2000
#: The text of a passage (a paragraph, a code block, a table) longer than ATTR_MAX_CHARS is embedded
#: in windows of that size overlapping by this much, one point each, at most TEXT_MAX_WINDOWS of them.
#: Before, such a passage had no embedding at all: a 3,300-character RFC paragraph was findable by
#: keyword only.
TEXT_WINDOW_OVERLAP = 200
TEXT_MAX_WINDOWS = 24


def text_windows(text: str, size: int = ATTR_MAX_CHARS, overlap: int = TEXT_WINDOW_OVERLAP) -> list[str]:
    """`text` cut into windows of at most `size` characters, at sentence ends where there are any
    (else at spaces, else anywhere), each repeating the last ~`overlap` characters of the one before
    so a sentence across a cut is whole in one of them."""
    import re
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces = [p for p in re.split(r"(?<=[.!?:;])\s+|\n{2,}", text) if p.strip()]
    out: list[str] = []
    current = ""
    for piece in pieces:
        while len(piece) > size:                      # one sentence longer than a window
            cut = piece.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            pieces_head, piece = piece[:cut], piece[cut:].lstrip()
            if current:
                out.append(current)
                current = ""
            out.append(pieces_head)
        if current and len(current) + 1 + len(piece) > size:
            out.append(current)
            tail = current[-overlap:] if overlap > 0 else ""      # [-0:] would be all of it
            tail = tail[tail.find(" ") + 1:] if " " in tail else tail
            current = f"{tail} {piece}" if tail and len(tail) + 1 + len(piece) <= size else piece
        else:
            current = f"{current} {piece}" if current else piece
    if current:
        out.append(current)
    return out


def is_indexable_attr(key: str, value) -> bool:
    """Whether a property gets its own embedding: text of usable length that is not machinery."""
    if not isinstance(key, str) or not isinstance(value, str):
        return False
    k = key.lower()
    if k in _INTERNAL_EXACT or k.startswith(_INTERNAL_PREFIXES) or k.endswith(_INTERNAL_SUFFIXES):
        return False
    return ATTR_MIN_CHARS <= len(value.strip()) <= ATTR_MAX_CHARS


def discover_attributes(properties: dict | None) -> dict[str, str]:
    """The attributes a node offers as search surfaces, found in its own properties."""
    if not properties:
        return {}
    return {k: v.strip() for k, v in properties.items() if is_indexable_attr(k, v)}


def attr_search_text(attr: str, value: str) -> str:
    """What gets embedded for an attribute: its name in words, then its value ("date time: ...")."""
    import re
    label = re.sub(r"[_\-]+", " ", attr).strip()
    return f"{label}: {value}" if label else value
