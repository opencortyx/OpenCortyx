"""Writing Delta tables, with delta-rs (`deltalake`). Reading is DuckDB's job (duck.py).

One commit per call. A commit that loses a race with another writer is retried: appends never
conflict in Delta, but a delete or merge can, and a retry against the new version is the protocol's
own answer to that.

Only table features the pinned DuckDB `delta` extension reads are used: the default protocol, no
deletion vectors, no column mapping. tests/test_data_store.py writes with this module and reads
back with DuckDB for every operation here, so a version bump that breaks that pairing fails there.
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Iterable, Optional

import pyarrow as pa

#: A commit conflict is retried this many times, a little later each time.
COMMIT_RETRIES = 4


def _dl():
    import deltalake
    return deltalake


def _retry(fn):
    from deltalake.exceptions import CommitFailedError
    for attempt in range(COMMIT_RETRIES + 1):
        try:
            return fn()
        except CommitFailedError:
            if attempt == COMMIT_RETRIES:
                raise
            time.sleep(0.05 * (2 ** attempt))


def exists(path: str) -> bool:
    return os.path.isdir(os.path.join(path, "_delta_log"))


def version(path: str) -> Optional[int]:
    if not exists(path):
        return None
    return _dl().DeltaTable(path).version()


def _quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def predicate(column: str, values: Iterable) -> str:
    """`column IN ('a', 'b')` with every value quoted - the only predicate shape this module builds."""
    vals = [_quote(v) for v in values]
    if not vals:
        raise ValueError("an empty predicate would match nothing; refusing to build one")
    if not column.replace("_", "").isalnum():
        raise ValueError(f"not a column name: {column!r}")
    return f"{column} = {vals[0]}" if len(vals) == 1 else f"{column} IN ({', '.join(vals)})"


def append(path: str, data, *, partition_by: Optional[list] = None) -> int:
    """Add rows (a pyarrow Table or RecordBatchReader). New columns are added to the schema."""
    dl = _dl()
    os.makedirs(path, exist_ok=True)

    def go():
        dl.write_deltalake(path, data, mode="append", schema_mode="merge",
                           partition_by=partition_by if not exists(path) else None)
    _retry(go)
    return version(path) or 0


def overwrite(path: str, data) -> int:
    """Replace the table's rows (and schema) with `data` in one commit. Earlier versions stay
    readable by time travel until vacuumed - evidence pinned to them still opens."""
    dl = _dl()
    os.makedirs(path, exist_ok=True)
    _retry(lambda: dl.write_deltalake(path, data, mode="overwrite", schema_mode="overwrite"))
    return version(path) or 0


def merge(path: str, data: pa.Table, keys: list[str]) -> int:
    """Upsert by `keys`: matching rows are replaced, new ones inserted. A table that does not exist
    yet is created by the first call."""
    if not exists(path):
        return append(path, data)
    dl = _dl()
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)

    def go():
        (dl.DeltaTable(path).merge(data, predicate=on, source_alias="s", target_alias="t")
         .when_matched_update_all().when_not_matched_insert_all().execute())
    _retry(go)
    return version(path) or 0


def delete(path: str, where: str) -> int:
    """Delete the rows matching `where` (build it with `predicate`). A missing table is a no-op."""
    if not exists(path):
        return 0
    _retry(lambda: _dl().DeltaTable(path).delete(where))
    return version(path) or 0


def drop(path: str) -> None:
    """Remove a whole table: a structured or semi-structured asset's own. Never the shared
    unstructured table - delete its rows instead."""
    if exists(path):
        shutil.rmtree(path)


def optimize(path: str, z_order: Optional[list[str]] = None) -> dict:
    """Compact small files (and cluster by `z_order`, e.g. asset_id for the shared chunks table)."""
    if not exists(path):
        return {}
    dt = _dl().DeltaTable(path)
    return dict(_retry(lambda: dt.optimize.z_order(z_order) if z_order else dt.optimize.compact()) or {})


def vacuum(path: str, retention_hours: int = 168) -> int:
    """Delete files no version newer than `retention_hours` needs. Evidence pinned to an older version
    then opens through the newest one (the lookup reports it), never a missing file."""
    if not exists(path):
        return 0
    removed = _dl().DeltaTable(path).vacuum(retention_hours=retention_hours, dry_run=False,
                                           enforce_retention_duration=False)
    return len(removed or [])


def read_at(path: str, version_: int, column: str, value) -> list[dict]:
    """Rows where `column == value` at an older table version (time travel), through delta-rs: the
    pinned DuckDB extension reads the latest version only."""
    import pyarrow.dataset as ds
    dt = _dl().DeltaTable(path, version=int(version_))
    return dt.to_pyarrow_dataset().to_table(filter=ds.field(column) == value).to_pylist()
