"""Where managed data lives, and the fixed columns of each group of tables.

    DATA_ROOT/<workspace_id>/
      unstructured/                  ONE table for every document: one row per chunk
      structured/<asset_id>/         one table per structured asset, its own columns
      semistructured/<asset_id>/     one table per semi-structured asset, one row per record

Unstructured is one table because every document has the same columns: a table per PDF would be
thousands of tiny tables. Structured and semi-structured assets each bring their own columns.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid

STRUCTURED = "structured"
SEMISTRUCTURED = "semistructured"
UNSTRUCTURED = "unstructured"
KINDS = (STRUCTURED, SEMISTRUCTURED, UNSTRUCTURED)

#: The column one unit (chunk, row, record) is found by - what an evidence pointer names.
UNIT_KEY = {UNSTRUCTURED: "chunk_id", STRUCTURED: "_row_key", SEMISTRUCTURED: "record_id"}
UNIT_KIND = {UNSTRUCTURED: "chunk", STRUCTURED: "row", SEMISTRUCTURED: "record"}

#: Columns every row of the group carries, beside the asset's own (structured, semi-structured).
SYSTEM_COLUMNS = {
    UNSTRUCTURED: ("asset_id", "version", "chunk_id", "ordinal", "element_type", "level",
                   "parent_chunk_id", "section_path", "page", "bbox", "text", "table_grid",
                   "ingested_at"),
    STRUCTURED: ("_row_key", "_snapshot", "_ingested_at"),
    SEMISTRUCTURED: ("record_id", "parent_record_id", "json_path", "sync_run", "fetched_at", "raw"),
}


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown asset kind {kind!r}; one of {', '.join(KINDS)}")
    return kind


def _uuid(value, what: str) -> str:
    """Ids become path segments, so only a UUID is accepted: no '..', no separators, no surprises."""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{what} must be a UUID, got {value!r}") from None


def workspace_root(root: str, workspace_id) -> str:
    return os.path.join(root, _uuid(workspace_id, "workspace_id")).replace("\\", "/")


def table_path(root: str, workspace_id, kind: str, asset_id=None) -> str:
    """The Delta table an asset's data is in. Unstructured assets share one table, so asset_id is
    not part of its path."""
    base = workspace_root(root, workspace_id)
    if check_kind(kind) == UNSTRUCTURED:
        return f"{base}/{UNSTRUCTURED}"
    return f"{base}/{kind}/{_uuid(asset_id, 'asset_id')}"


def view_name(name: str, taken: set[str]) -> str:
    """A SQL identifier from an asset's name ("Licences 2026.csv" -> licences_2026), unique within one
    connection. Generated SQL then reads like the customer's own schema."""
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", (name or "").strip())
    base = re.sub(r"[^0-9a-zA-Z]+", "_", base).strip("_").lower() or "asset"
    if base[0].isdigit():
        base = "t_" + base
    base = base[:60]
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out)
    return out


def row_key(row: dict, key_columns=(), occurrences: dict | None = None) -> str:
    """A row's `_row_key`: its declared or detected key, else a hash of its content plus how many
    identical rows came before it - never the row number alone, which changes when a row is
    inserted above. `occurrences` (one dict per table read) counts the duplicates."""
    keys = [c for c in (key_columns or ()) if row.get(c) not in (None, "")]
    if keys:
        return "|".join(str(row[c]) for c in keys)
    digest = hashlib.sha1(json.dumps(row, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]
    if occurrences is None:
        return digest
    n = occurrences.get(digest, 0)
    occurrences[digest] = n + 1
    return f"{digest}:{n}"


def arrow_type(values):
    """The Arrow type a column of mixed JSON values is kept as: all numbers (or all booleans) keep
    that type, anything mixed is text - so records with differing fields still make one table."""
    import pyarrow as pa
    seen = [v for v in values if v is not None]
    if seen and all(isinstance(v, bool) for v in seen):
        return pa.bool_()
    if seen and all(isinstance(v, int) and not isinstance(v, bool) for v in seen):
        return pa.int64()
    if seen and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return pa.float64()
    return pa.string()
