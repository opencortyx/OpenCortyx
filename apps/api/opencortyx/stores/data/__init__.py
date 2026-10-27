"""The data store: where data lives, apart from the knowledge built from it.

    write ── delta-rs (`deltalake`) ──► DATA_ROOT/<workspace>/{unstructured, structured/<id>, semistructured/<id>}
    read  ── DuckDB ─────────────────► the same tables, external lakes and files, attached databases

This package is the ONLY importer of `duckdb` and `deltalake` in OpenCortyx
(tests/test_data_store.py checks it). Everything else asks it for rows, chunks or records, and gets
Arrow tables or dicts back - so the engines can be swapped (delta-rs' own reader, DataFusion) without
touching a caller.

See docs/knowledge-layer-implementation.md, sections 2 and 2.6.
"""
from __future__ import annotations

from typing import Iterable, Optional

from opencortyx.stores.data import delta, duck, layout
from opencortyx.stores.data.duck import AssetRef, DataStoreError, Views
from opencortyx.stores.data.layout import (
    KINDS, SEMISTRUCTURED, STRUCTURED, UNIT_KEY, UNIT_KIND, UNSTRUCTURED,
)

__all__ = ["AssetRef", "DataStore", "DataStoreError", "KINDS", "SEMISTRUCTURED", "STRUCTURED",
           "UNIT_KEY", "UNIT_KIND", "UNSTRUCTURED", "Views", "compact", "connect", "managed_tables"]


def connect(*, extensions: Iterable[str] = (), **kwargs):
    """A bare DuckDB connection (memory and thread limits set, `extensions` loaded) for readers that
    build their own SQL over files: the tabular and lake readers at ingest."""
    return duck.connect(extensions=extensions, **kwargs)


class DataStore:
    """One workspace's managed data. Cheap to construct; holds no connection."""

    def __init__(self, workspace_id, root: Optional[str] = None):
        from opencortyx.core.config import settings
        self.workspace_id = workspace_id
        self.root = root or settings.DATA_ROOT

    def path(self, kind: str, asset_id=None) -> str:
        return layout.table_path(self.root, self.workspace_id, kind, asset_id)

    # ── writing ───────────────────────────────────────────────────────────────────────────────
    def append(self, kind: str, asset_id, data) -> int:
        """Add an asset's chunks, rows or records in one commit; returns the new table version."""
        return delta.append(self.path(kind, asset_id), data)

    def merge(self, kind: str, asset_id, data, keys: Optional[list[str]] = None) -> int:
        """Upsert by the group's unit key (plus asset_id in the shared chunks table)."""
        keys = keys or (["asset_id", UNIT_KEY[kind]] if kind == UNSTRUCTURED else [UNIT_KEY[kind]])
        return delta.merge(self.path(kind, asset_id), data, keys)

    def delete_asset(self, kind: str, asset_id) -> None:
        """Everything of one asset. Its own table goes; in the shared chunks table, its rows do."""
        if kind == UNSTRUCTURED:
            delta.delete(self.path(kind), delta.predicate("asset_id", [str(asset_id)]))
        else:
            delta.drop(self.path(kind, asset_id))

    def delete_units(self, kind: str, asset_id, unit_keys: Iterable) -> int:
        keys = [str(k) for k in unit_keys]
        if not keys:
            return delta.version(self.path(kind, asset_id)) or 0
        where = delta.predicate(UNIT_KEY[kind], keys)
        if kind == UNSTRUCTURED:
            where = f"{delta.predicate('asset_id', [str(asset_id)])} AND {where}"
        return delta.delete(self.path(kind, asset_id), where)

    def version(self, kind: str, asset_id=None) -> Optional[int]:
        return delta.version(self.path(kind, asset_id))

    def optimize(self, kind: str, asset_id=None) -> dict:
        return delta.optimize(self.path(kind, asset_id),
                              z_order=["asset_id"] if kind == UNSTRUCTURED else None)

    def vacuum(self, kind: str, asset_id=None, retention_hours: int = 168) -> int:
        return delta.vacuum(self.path(kind, asset_id), retention_hours)

    # ── reading ───────────────────────────────────────────────────────────────────────────────
    def _ref(self, kind: str, asset_id) -> AssetRef:
        return AssetRef(asset_id=str(asset_id), name="t", kind=kind)

    def read(self, kind: str, asset_id, *, columns: Optional[list[str]] = None,
             limit: Optional[int] = None, offset: int = 0, order_by: Optional[str] = None):
        """An asset's rows as an Arrow table (a page of them with limit/offset), or None when the
        asset has no data yet."""
        with Views(self.workspace_id, [self._ref(kind, asset_id)], root=self.root) as v:
            if "t" not in v.names:
                return None
            cols = ", ".join(f'"{c}"' for c in columns) if columns else "*"
            sql = f'SELECT {cols} FROM "t"'
            order = order_by or ("ordinal" if kind == UNSTRUCTURED else UNIT_KEY[kind])
            sql += f' ORDER BY "{order}"'
            if limit is not None:
                sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
            return v.query(sql)

    def lookup(self, kind: str, asset_id, unit_keys: Iterable, *,
               columns: Optional[list[str]] = None) -> dict:
        """{unit_key: row} for many units in one read - the batched fetch of passage text, or the
        rows a set of evidence pointers name."""
        keys = [str(k) for k in unit_keys]
        if not keys:
            return {}
        key = UNIT_KEY[kind]
        with Views(self.workspace_id, [self._ref(kind, asset_id)], root=self.root) as v:
            if "t" not in v.names:
                return {}
            cols = "*" if not columns else ", ".join(f'"{c}"' for c in dict.fromkeys([key, *columns]))
            marks = ", ".join("?" for _ in keys)
            rows = v.query(f'SELECT {cols} FROM "t" WHERE CAST("{key}" AS VARCHAR) IN ({marks})',
                           keys).to_pylist()
        return {str(r[key]): r for r in rows}

    def open_unit(self, kind: str, asset_id, unit_key, version_ref=None) -> tuple[Optional[dict], bool]:
        """The chunk, row or record behind a piece of evidence, as it was at `version_ref`.

        Returns (row, current): `current` is False when the pinned version could not be read (it
        was vacuumed) and the row comes from the latest version instead - the caller then says
        "may have changed since extraction" rather than showing a different value as the old one."""
        key = str(unit_key)
        path = self.path(kind, asset_id)
        if version_ref not in (None, ""):
            try:
                rows = delta.read_at(path, int(version_ref), UNIT_KEY[kind], key)
                if kind == UNSTRUCTURED:
                    rows = [r for r in rows if str(r.get("asset_id")) == str(asset_id)]
                if rows:
                    return rows[0], True
            except Exception:                                 # noqa: BLE001 - vacuumed or gone
                pass
            row = self.lookup(kind, asset_id, [key]).get(key)
            return row, False
        return self.lookup(kind, asset_id, [key]).get(key), True

    def views(self, assets: Iterable[AssetRef], *, lock: bool = False, **conn_kwargs) -> Views:
        """A connection with this workspace's assets as named views - managed and external alike."""
        return Views(self.workspace_id, assets, root=self.root, lock=lock, **conn_kwargs)


def managed_tables(root: Optional[str] = None):
    """(workspace_id, kind, asset_id or None) for every managed Delta table under DATA_ROOT."""
    import os
    import uuid as _uuid
    if root is None:
        from opencortyx.core.config import settings
        root = settings.DATA_ROOT
    if not os.path.isdir(root):
        return
    for ws in sorted(os.listdir(root)):
        try:
            _uuid.UUID(ws)
        except ValueError:
            continue
        base = os.path.join(root, ws)
        if delta.exists(os.path.join(base, UNSTRUCTURED)):
            yield ws, UNSTRUCTURED, None
        for kind in (STRUCTURED, SEMISTRUCTURED):
            group = os.path.join(base, kind)
            if os.path.isdir(group):
                for asset in sorted(os.listdir(group)):
                    if delta.exists(os.path.join(group, asset)):
                        yield ws, kind, asset


def compact(root: Optional[str] = None, retention_hours: int = 168) -> dict:
    """Compact every managed table and remove files older versions no longer need."""
    done, failed = 0, 0
    for ws, kind, asset in managed_tables(root):
        store = DataStore(ws, root=root)
        try:
            store.optimize(kind, asset)
            store.vacuum(kind, asset, retention_hours)
            done += 1
        except Exception:                                     # noqa: BLE001 - one table never stops the rest
            failed += 1
    return {"tables": done, "failed": failed}
