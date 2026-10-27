"""Reading, with DuckDB: managed Delta tables, external lakes and files, attached databases.

Every connection is in memory and short-lived. DuckDB only queries - it stores nothing, so there is
no .duckdb file - and a connection is opened per job or per question, given the views of ONE
workspace's assets, and closed. Connections are cheap; views are never shared across workspaces.

Extensions come from the directory the image bakes them into (DUCKDB_EXTENSION_DIR), so a query
never downloads anything. One that is not there (a developer machine, CI) is installed on first use.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Iterable, Optional

from opencortyx.stores.data import layout

#: Extensions by what needs them. A managed asset needs `delta` only.
FORMAT_EXTENSIONS = {"delta": ("delta",), "iceberg": ("iceberg",), "postgres": ("postgres",),
                     "mysql": ("mysql",), "sqlite": ("sqlite",)}
REMOTE_EXTENSIONS = ("httpfs",)
#: Everything the image bakes in (apps/api/Dockerfile).
BUNDLED = ("delta", "iceberg", "httpfs", "aws", "azure", "postgres", "mysql", "sqlite")


class DataStoreError(RuntimeError):
    """A read that could not run: a missing extension, an unreachable source, a timeout."""


def _settings():
    from opencortyx.core.config import settings
    return settings


def _extension_dir() -> Optional[str]:
    d = getattr(_settings(), "DUCKDB_EXTENSION_DIR", "") or ""
    return d if d and os.path.isdir(d) else None


def load_extensions(con, names: Iterable[str]) -> None:
    for ext in dict.fromkeys(names):
        try:
            con.load_extension(ext)
        except Exception:                                     # noqa: BLE001 - not bundled here
            try:
                con.install_extension(ext)
                con.load_extension(ext)
            except Exception as e:                            # noqa: BLE001
                raise DataStoreError(
                    f"DuckDB extension '{ext}' is not bundled and could not be installed "
                    f"(no internet?): {e}") from e


def connect(*, extensions: Iterable[str] = (), memory_limit: Optional[str] = None,
            threads: Optional[int] = None, purpose: str = "query"):
    """A fresh in-memory DuckDB connection with `extensions` loaded and the working-memory limits set.
    The one place in OpenCortyx that opens DuckDB. `purpose="ingest"` gets the ingest limits."""
    import duckdb
    s = _settings()
    ingest = purpose == "ingest"
    config: dict = {"memory_limit": memory_limit or (s.DATA_DUCKDB_INGEST_MEMORY_LIMIT if ingest
                                                     else s.DATA_DUCKDB_MEMORY_LIMIT),
                    "autoinstall_known_extensions": False}
    n = int(threads or (s.DATA_DUCKDB_INGEST_THREADS if ingest else s.DATA_DUCKDB_THREADS) or 0)
    if n > 0:
        config["threads"] = n
    ext_dir = _extension_dir()
    if ext_dir:
        config["extension_directory"] = ext_dir
    con = duckdb.connect(database=":memory:", config=config)
    load_extensions(con, extensions)
    return con


def run(con, sql: str, params: Optional[list] = None, timeout: Optional[float] = None):
    """Execute and return an Arrow table, interrupting the query after `timeout` seconds."""
    limit = float(timeout if timeout is not None else _settings().DATA_QUERY_TIMEOUT_SECONDS)
    timer = threading.Timer(limit, con.interrupt) if limit > 0 else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        return con.execute(sql, params or []).fetch_arrow_table()
    except Exception as e:                                    # noqa: BLE001
        if "interrupt" in str(e).lower():
            raise DataStoreError(f"query stopped after {limit:g} s") from e
        raise
    finally:
        if timer:
            timer.cancel()


def _lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


@dataclass
class AssetRef:
    """What a view needs to know about an asset: a `data_asset` row, or the same fields by hand."""
    asset_id: str
    name: str
    kind: str
    storage: str = "managed"
    format: str = "delta"
    location: str = ""
    #: For an attached database: the DuckDB ATTACH string (secrets resolved for this connection only).
    dsn: str = ""
    options: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "AssetRef":
        return cls(asset_id=str(row.id), name=row.name or str(row.id), kind=row.kind,
                   storage=row.storage, format=row.format, location=row.location or "",
                   options=dict(getattr(row, "metadata_", None) or {}))


def scan_sql(ref: AssetRef, root: str, workspace_id) -> Optional[str]:
    """The FROM expression for one asset, or None when it has no data to read yet."""
    if ref.storage == "managed":
        path = layout.table_path(root, workspace_id, ref.kind, ref.asset_id)
        from opencortyx.stores.data import delta
        if not delta.exists(path):
            return None
        sql = f"delta_scan({_lit(path)})"
        if ref.kind == layout.UNSTRUCTURED:
            return f"(SELECT * FROM {sql} WHERE asset_id = {_lit(ref.asset_id)})"
        return sql
    fmt, loc = ref.format, ref.location
    if fmt == "delta":
        return f"delta_scan({_lit(loc)})"
    if fmt == "iceberg":
        return f"iceberg_scan({_lit(loc)})"
    if fmt == "parquet":
        return f"read_parquet({_lit(loc)})"
    if fmt == "csv":
        return f"read_csv({_lit(loc)})"
    if fmt in ("json", "ndjson"):
        return f"read_json({_lit(loc)})"
    if fmt in ("postgres", "mysql", "sqlite"):
        return None                                            # attached, see Views.add
    raise DataStoreError(f"no reader for external format {fmt!r}")


#: External formats read through an adapter (stores/data/adapters) rather than a DuckDB extension.
ADAPTED = ("mongodb", "mssql", "sqlserver", "oracle", "snowflake", "bigquery", "redshift", "trino",
           "clickhouse", "databricks", "athena", "db2")


def adapter_for(ref: "AssetRef"):
    """The adapter that reads `ref`'s source; `ref.dsn` holds its connection string for this use only."""
    if ref.format == "mongodb":
        from opencortyx.stores.data.adapters.mongodb import MongoAdapter, database_of
        return MongoAdapter(ref.dsn, ref.options.get("database") or database_of(ref.dsn))
    from opencortyx.stores.data.adapters.arrow_sql import ArrowSQLAdapter
    return ArrowSQLAdapter(ref.dsn)


class Views:
    """One connection with one workspace's assets as views. Use as a context manager."""

    def __init__(self, workspace_id, assets: Iterable[AssetRef], *, root: Optional[str] = None,
                 lock: bool = False, **conn_kwargs):
        self.workspace_id = workspace_id
        self.root = root or _settings().DATA_ROOT
        assets = list(assets)
        exts = {"delta"} if any(a.storage == "managed" for a in assets) else set()
        for a in assets:
            if a.storage == "external":
                exts.update(FORMAT_EXTENSIONS.get(a.format, ()))
                if "://" in (a.location or ""):
                    exts.update(REMOTE_EXTENSIONS)
        self.con = connect(extensions=sorted(exts), **conn_kwargs)
        self.names: dict[str, str] = {}                       # view -> asset_id
        self._taken: set[str] = set()
        self._attached = 0
        for a in assets:
            self.add(a)
        if lock:
            # Question-time: nothing the SQL runs can change these settings, re-enable downloads or
            # widen the limits.
            self.con.execute("SET lock_configuration = true")

    def add(self, ref: AssetRef) -> Optional[str]:
        source: Optional[str]
        if ref.storage == "external" and ref.format in ADAPTED:
            return self._add_adapted(ref)
        if ref.storage == "external" and ref.format in ("postgres", "mysql", "sqlite"):
            from opencortyx.stores.data.adapters import attach
            self._attached += 1
            alias = f"src{self._attached}"
            attach.attach(self.con, alias, ref.format, ref.dsn or ref.location)
            source = attach.qualified(alias, ref.format, ref.options.get("table") or ref.location)
        else:
            source = scan_sql(ref, self.root, self.workspace_id)
            if source is None:
                return None
        view = layout.view_name(ref.name, self._taken)
        self.con.execute(f'CREATE VIEW "{view}" AS SELECT * FROM {source}')
        self.names[view] = ref.asset_id
        return view

    def _add_adapted(self, ref: AssetRef) -> str:
        """A source DuckDB cannot attach: its adapter's scan, capped, registered for this connection."""
        import pyarrow as pa
        adapter = adapter_for(ref)
        source = ref.options.get("table") or ref.location
        cap = int(_settings().DATA_ADAPTER_MAX_ROWS)
        batches = list(adapter.scan(source, filters=ref.options.get("filters"), limit=cap))
        table = pa.Table.from_batches(batches) if batches else pa.table({})
        view = layout.view_name(ref.name, self._taken)
        self.con.register(view, table)
        self.names[view] = ref.asset_id
        return view

    def query(self, sql: str, params: Optional[list] = None, timeout: Optional[float] = None):
        return run(self.con, sql, params, timeout)

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:                                     # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
