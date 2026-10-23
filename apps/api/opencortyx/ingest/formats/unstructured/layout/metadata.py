"""A document's own metadata, read from its opening: title, identifiers, header fields, references.

Many documents open with a block of `Key: value` lines: an RFC's or a PEP's header, Markdown front
matter, an HTML page's definition list. Read once, as the document is read, they answer "what is
its status", "when was it published", "what does it replace" from one field, where otherwise the
answer is a line of text a search has to happen to surface.

Nothing here knows a document family. A header is two or more `Key: value` lines among the first
elements; a document's identifier is its filename's scheme and number (`rfc9304`, `pep-0809`), or a
short key and a lone number ("RFC: 9304", "STD: 97", "Request for Comments: 9304" when the number is
the filename's); a reference is a header value made of identifiers ("Obsoletes: 8113, 8114"), named
by its key. Links between documents are made from these by the resolution engine, which matches
references to identifiers.
"""
from __future__ import annotations

import re
from typing import Optional

#: How much of a document's opening is read for its header.
HEADER_ELEMENTS = 40
HEADER_LINES = 80
HEADER_CHARS = 4000
MAX_FIELDS = 24
MAX_VALUE = 300

_MONTHS = ("january|february|march|april|may|june|july|august|september|october|november|december|"
           "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec")
#: A date as headers write it: "October 2022", "15 October 2022", "2022-10-15", "15-Oct-2022".
_DATE = re.compile(r"\b(?:\d{1,2}[ -])?(?:" + _MONTHS + r")[a-z]*\.?[ ,-]*\d{4}\b|\b\d{4}-\d{2}-\d{2}(?!\d)",
                   re.IGNORECASE)
#: A header line: a key of one to four words joined by spaces or hyphens ("Superseded-By",
#: "Request for Comments") at the start of the line, a colon, and a value.
_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]{0,24}(?:[ -][A-Za-z][A-Za-z0-9]{0,24}){0,3}):\s+(.*\S)\s*$")
#: Where a value's first column ends: a wide gap (a two-column header) or a table cell border.
_COLUMN = re.compile(r"\s{3,}|\s\|\s")
_DATE_KEYS = ("published", "date", "created", "issued")
#: A filename's scheme and number: `rfc9304`, `pep-0809`.
_STEM_ID = re.compile(r"^([a-z]{2,8})[-_ ]?0*(\d{1,6})(?![\d.])", re.IGNORECASE)
_ID_TOKEN = re.compile(r"^(?:([A-Za-z]{2,8})[ -]?)?0*(\d{1,6})$")
_ID_ONLY = re.compile(r"^[A-Za-z]{2,8}[ -]?\d{1,6}$")


def snake(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")


def relation(key: str) -> str:
    """The edge type a reference key names: "Superseded-By" -> SUPERSEDED_BY."""
    return snake(key).upper()


def _initials(key: str) -> str:
    words = [w for w in re.split(r"[ -]+", key.strip()) if w]
    return words[0].lower() if len(words) == 1 else "".join(w[0] for w in words).lower()


def identifier(scheme: str, number: str) -> str:
    return f"{scheme.lower()} {int(number)}"


def header_end(texts: list[str], labels: list[str]) -> int:
    """Index of the first element after the header: the first heading once a field line was seen.
    A "Note: ..." in the body is not part of the header."""
    seen = False
    for i, (text, label) in enumerate(zip(texts, labels)):
        if seen and label in ("section_header", "title"):
            return i
        seen = seen or any(_LINE.match(x) for x in (text or "").splitlines())
    return len(texts)


def _lines(texts: list[str]) -> list[str]:
    """The opening as lines. An element that is only a key ("Obsoletes:", a definition list's term)
    is joined with the next one, its value."""
    out: list[str] = []
    i = 0
    while i < len(texts) and len(out) < HEADER_LINES:
        text = (texts[i] or "").strip()
        if text.endswith(":") and len(text) <= 40 and i + 1 < len(texts):
            text = f"{text} {(texts[i + 1] or '').strip()}"
            i += 1
        out.extend(text.splitlines())
        i += 1
    return out[:HEADER_LINES]


def header_fields(texts: list[str]) -> dict[str, str]:
    """`Key: value` lines of a document's opening, in order, first occurrence of a key kept. A
    value is its first column: a two-column header's right side (an author, a date) and a table
    cell border are not part of it."""
    out: dict[str, str] = {}
    for line in _lines(texts):
        m = _LINE.match(line)
        if not m:
            continue
        key = m.group(1).strip()
        value = _COLUMN.split(m.group(2).strip().strip("|").strip())[0].strip(" |")[:MAX_VALUE]
        if value and key not in out:
            out[key] = value
        if len(out) >= MAX_FIELDS:
            break
    return out


def _identifiers_in(value: str, scheme: str) -> list[str]:
    """The identifiers a header value lists, in order: "8113, 8114", "RFC 8113 and RFC 8114",
    "PEP 703". Stops at the first word that is not one; dates are removed first."""
    out: list[str] = []
    for token in re.split(r"[,;\s]+", _DATE.sub(" ", value)):
        token = token.strip("().[]")
        if not token or token.lower() in ("and", "&"):
            continue
        if token.isalpha() and token.lower() == scheme:
            continue                                     # "RFC" before a number
        m = _ID_TOKEN.match(token)
        if not m:
            break
        out.append(identifier(m.group(1) or scheme, m.group(2)))
    return out


def document_metadata(texts: list[str], labels: list[str], filename: str, native: dict | None = None) -> dict:
    """{title, identifiers, published, fields, references} of one document.

    `texts`/`labels` are the reader's elements and their labels, in reading order; `native` is what
    the file format carries (readers/native.py). The document's own text wins for its title and date
    (a file's stored title is often stale: "Document1"); native fields fill the ones it lacks."""
    native = {k: v for k, v in (native or {}).items() if k and v}
    texts, labels = list(texts[:HEADER_ELEMENTS]), list(labels[:HEADER_ELEMENTS])
    stem = re.sub(r"\.[A-Za-z0-9]{1,6}$", "", (filename or "").replace("\\", "/").rsplit("/", 1)[-1])
    stem_id = _STEM_ID.match(stem)
    scheme, number = (stem_id.group(1).lower(), stem_id.group(2)) if stem_id else ("", "")

    end = header_end(texts, labels)
    fields = header_fields(texts[:end])
    if len(fields) < 2:                                  # one "Note: ..." is prose, not a header
        fields = {}

    ids: list[str] = [identifier(scheme, number)] if scheme else []
    own_keys: set[str] = set()
    for key, value in fields.items():
        only = re.fullmatch(r"0*(\d{1,6})", value)
        if not only:
            continue
        # The document's own number ("Request for Comments: 9304"), or its number in another series
        # under the series' short name ("STD: 97"): an identifier of this document, not a reference.
        if (number and int(only.group(1)) == int(number)) or re.fullmatch(r"[A-Za-z]{2,5}", key.strip()):
            ids.append(identifier(_initials(key), only.group(1)))
            own_keys.add(key)
    if native.get("identifier") and re.search(r"\d", native["identifier"]) and len(native["identifier"]) <= 80:
        ids.append(native["identifier"].strip().lower())       # an e-book's ISBN, a report number
    own_scheme = scheme or (ids[0].split()[0] if ids else "")

    references: list[list[str]] = []
    if own_scheme:
        for key, value in fields.items():
            if key in own_keys or snake(key) in _DATE_KEYS:
                continue
            for ref in _identifiers_in(value, own_scheme):
                if ref not in ids and [relation(key), ref] not in references:
                    references.append([relation(key), ref])

    # A title that is only an identifier ("RFC 9304" above the real title) is not the title.
    title: Optional[str] = next((t.strip() for t, label in zip(texts, labels)
                                 if label == "title" and t and t.strip() and not _ID_ONLY.match(t.strip())), None)
    title = title or fields.get("Title") or fields.get("title")
    if not title and fields:
        # A plain-text header is followed by the title on a line of its own: the first short
        # element after the header's fields that is neither a sentence nor a field.
        last = max((i for i, t in enumerate(texts[:end]) if any(_LINE.match(x) for x in (t or "").splitlines())),
                   default=-1)
        title = next((t.strip() for t in texts[last + 1:last + 6]
                      if t and 3 < len(t.strip()) <= 150 and not t.strip().endswith(".")
                      and not any(_LINE.match(x) for x in t.splitlines())), None)
    title = title or native.get("title")
    if not title:
        title = next((t.strip() for t, label in zip(texts, labels)
                      if label == "section_header" and t and t.strip()), None)

    published = next((fields[k] for k in fields if snake(k) in _DATE_KEYS), None)
    published = published or next((native[k] for k in ("published", "date", "created", "issued") if native.get(k)), None)
    date = _DATE.search(published or " ".join(t for t in texts if t)[:HEADER_CHARS])
    return {"title": (title or "")[:MAX_VALUE] or None,
            "identifiers": list(dict.fromkeys(ids)),
            "published": date.group(0) if date else None,
            "fields": {**{k: v for k, v in native.items() if k not in ("title", "identifier")},
                       **{snake(k): v for k, v in fields.items() if snake(k)}},
            "references": references}


def apply_rules(meta: dict, rules: dict | None) -> dict:
    """A mapping's metadata card applied to `document_metadata`'s result: `rename` {field: name}
    renames a field and the relation its references make ("obsoletes" -> "replaces": REPLACES),
    `drop` [field] removes a field and its references, `header_references: false` keeps the fields
    but links nothing."""
    if not rules:
        return meta
    rename = {snake(k): snake(v) for k, v in (rules.get("rename") or {}).items() if snake(k) and snake(v)}
    drop = {snake(k) for k in (rules.get("drop") or []) if snake(k)}
    fields = {rename.get(k, k): v for k, v in meta["fields"].items() if k not in drop}
    refs = [] if rules.get("header_references") is False else [
        [relation(rename.get(snake(rel), snake(rel))), ref] for rel, ref in meta["references"]
        if snake(rel) not in drop]
    return {**meta, "fields": fields, "references": refs}
