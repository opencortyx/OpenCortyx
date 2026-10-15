"""SCHEMA-SIGNATURE ROUTING — one rule, all three paradigms.

The contract the pipeline's Step-1 options describe, and what every ingest path must honour:

  1. A source that MATCHES a bound schema mapping's signature is read through THAT mapping.
  2. A source no bound mapping matches is read by the DEFAULT WALKER when the pipeline's
     `auto_accept` option is ON, and always when no mapping is bound at all.
  3. With a mapping bound and `auto_accept` OFF, the mapping is an ALLOW-LIST: non-matching sources
     are IGNORED — not walked, not written to the graph at all.

The second Step-1 option, `extract_only`, keeps the schema and not the data: tables and columns
without rows, one record's structure without values, and for documents only what extraction emits
(chunks are used for extraction and not saved).

What "signature" means per paradigm — always the source's SHAPE, never its filename:
  • semi-structured — the record's key-path set vs a declared structure's (schema.match_structure,
    overlap-based so optional/missing fields still match).
  • structured      — the mapping's declared COLUMNS ⊆ the table's columns. Column names, not the
    table name: the same table arrives as `customers.csv`, `customers_2026.csv`, `dbo.customers`.
  • unstructured    — documents have no schema, so the signature is the mapping's declared
    source_type / formats vs the file's extension. A mapping that declares neither matches every
    document (that is the honest reading of "this mapping describes my documents").

`auto_accept` unset → True, so zero-config ingestion keeps working; the gate only engages for a
pipeline that BOTH binds a mapping and turns the option off.
"""
import os

_FALSE_STRINGS = ("false", "0", "no", "off")


def auto_create_on(cfg, bound: bool) -> bool:
    """May this run read a source no bound mapping describes with the default walker?

    Always when nothing is bound — the default walker is how an unconfigured pipeline reads anything.
    With a mapping bound, only while `auto_accept` is on; off turns the mapping into an allow-list."""
    return not bound or auto_accept_on(cfg)


def auto_accept_on(cfg) -> bool:
    """The "Accept a newly detected shape automatically" option. Default ON — absent config must
    never silently ignore a user's data."""
    v = (cfg or {}).get("auto_accept")
    if v is None:
        return True
    if isinstance(v, str):
        return v.strip().lower() not in _FALSE_STRINGS
    return bool(v)


def extract_only_on(cfg) -> bool:
    """The "Extract only" option. Also honours the spellings saved before it was unified:
    `structure_only` (semi-structured) and `save_chunks: false` (documents)."""
    cfg = cfg or {}
    for key in ("extract_only", "structure_only"):
        v = cfg.get(key)
        if isinstance(v, str):
            if v.strip() and v.strip().lower() not in _FALSE_STRINGS:
                return True
        elif v:
            return True
    return cfg.get("save_chunks") is False


# ── structured ───────────────────────────────────────────────────────────────────────────────────
def card_columns(card) -> set:
    """The column names a structured mapping card declares (ignoring ones it drops)."""
    out = set()
    for c in (card or {}).get("columns") or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "").strip()
        if name and str(c.get("role") or "").strip().lower() not in ("ignore", "skip"):
            out.add(name.lower())
    return out


def match_table_card(cards: list, table_name: str, columns: list):
    """The bound card this table should be read through, or None.

    A card matches when its declared columns are all present in the table (subset). Extra columns in
    the source are fine — a mapping describes what it needs, not everything the table has. The table
    NAME is only a tie-breaker between several matching cards, never a requirement."""
    have = {str((c.get("name") if isinstance(c, dict) else c) or "").strip().lower()
            for c in (columns or [])}
    have.discard("")
    tname = str(table_name or "").strip().lower()
    matches = []
    for card in cards or []:
        want = card_columns(card)
        if want and want <= have:
            matches.append(card)
    if not matches:
        return None
    for card in matches:                     # same-name card wins when several fit
        if str(card.get("table") or "").strip().lower() == tname:
            return card
    return matches[0]


# ── unstructured ─────────────────────────────────────────────────────────────────────────────────
def _exts_of(mapping) -> set:
    """Extensions an unstructured mapping declares it reads (source_type and/or editor.formats)."""
    out = set()
    raw = [(mapping or {}).get("source_type") or ""]
    ed = ((mapping or {}).get("payload") or {}).get("editor") or {}
    for key in ("formats", "file_types", "extensions"):
        v = ed.get(key)
        if isinstance(v, (list, tuple)):
            raw.extend(str(x) for x in v)
        elif v:
            raw.append(str(v))
    for token in raw:
        for part in str(token).replace(";", ",").split(","):
            p = part.strip().lstrip(".").lower()
            if p and p not in ("any", "all", "*", "file", "document", "documents"):
                out.add(p)
    return out


def match_document(mappings: list, filename: str):
    """The bound unstructured mapping a file should be read through, or None.

    A mapping that declares no formats matches everything (documents have no schema to compare), so
    the gate only excludes a file when the author actually said "this mapping is for PDFs"."""
    ext = os.path.splitext(str(filename or ""))[1].lstrip(".").lower()
    for m in mappings or []:
        want = _exts_of(m)
        if not want or (ext and ext in want):
            return m
    return None
