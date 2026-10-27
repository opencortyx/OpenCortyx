"""Structured assets on the data plane: rows into Delta, live and lake tables registered in place.

Used by every structured ingest (an uploaded file, a SQL database, a lake table) when the workspace's
data lives in Delta (`data_store = delta`). The graph side - the catalogue, row mode, the Mapper,
extractors - runs exactly as before; this adds where the rows themselves live:

  managed   a file's rows go to DATA_ROOT/<ws>/structured/<asset_id>, types fixed once at ingest,
            each row keyed by `_row_key` (the same key its evidence uses), one commit per ingest
  external  a live database table or a customer's lake table is REGISTERED, never copied: its
            location, its connector and the version read (a Delta version, an Iceberg snapshot,
            or the time of the read for a live table)

and the column profile the SQL planner needs: null share, distinct count, and for a short category
column (not an identifier, a date, a number or PII) its values and counts - `value_set`, the one
sample the graph keeps, because "status = 'active'" against ACTIVE returns nothing.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, Optional

from opencortyx.core.observability import get_logger
from opencortyx.stores.data.layout import STRUCTURED, row_key

log = get_logger(__name__)

#: A category column with at most this many distinct values keeps them as `value_set`.
VALUE_SET_MAX = 50
#: Column names that mark personal data: never profiled into the graph, never sent to a model.
_PII = re.compile(r"(^|_)(e?mail|phone|mobile|ssn|passport|national_id|iban|card|dob|birth|address|"
                  r"postcode|zip|ip_address|salary|password|token|secret)(_|$)", re.I)


def is_pii(name: str) -> bool:
    return bool(_PII.search(str(name or "")))


async def uses_delta(workspace_id) -> bool:
    try:
        from opencortyx.knowledge.workspace import data_store
        return (await data_store(workspace_id)) == "delta"
    except Exception:                                         # noqa: BLE001 - default to the old path
        return False


def _keyed_batches(batches: Iterable, key_columns: list, snapshot: str) -> Iterator:
    """Each Arrow batch with `_row_key`, `_snapshot` and `_ingested_at` added. `_row_key` is
    computed over the same JSON-normalised values row mode keys its evidence by."""
    import pyarrow as pa

    from opencortyx.ingest.formats.structured.tabular import _jsonable
    occ: dict = {}
    now = datetime.now(timezone.utc)
    for batch in batches:
        rows = batch.to_pylist()
        keys = [row_key({k: _jsonable(v) for k, v in r.items()}, key_columns, occ) for r in rows]
        n = batch.num_rows
        yield pa.RecordBatch.from_arrays(
            [*batch.columns, pa.array(keys, pa.string()), pa.array([snapshot] * n, pa.string()),
             pa.array([now] * n, pa.timestamp("us", tz="UTC"))],
            names=[*batch.schema.names, "_row_key", "_snapshot", "_ingested_at"])


def write_rows(workspace_id, asset_id, batches: Callable[[], Iterable], key_columns: list,
               snapshot: Optional[str] = None) -> dict:
    """Replace a managed asset's rows with what `batches()` yields, in one commit (sync: call it in a
    worker thread). Earlier versions stay readable by evidence pinned to them until vacuumed."""
    import pyarrow as pa

    from opencortyx.stores.data import DataStore, delta
    store = DataStore(workspace_id)
    path = store.path(STRUCTURED, asset_id)
    snap = snapshot or uuid.uuid4().hex[:12]
    it = _keyed_batches(batches(), key_columns, snap)
    first = next(it, None)
    if first is None:
        return {"rows": 0, "version": delta.version(path)}
    rows = [first.num_rows]

    def _all():
        yield first
        for b in it:
            rows.append(b.num_rows)
            yield b
    reader = pa.RecordBatchReader.from_batches(first.schema, _all())
    version = delta.overwrite(path, reader)
    return {"rows": sum(rows), "version": version, "snapshot": snap}


def profile(workspace_id, ref, columns: list[dict]) -> dict:
    """{column: {null_pct, distinct_count, value_set?}} over an asset's view (sync)."""
    from opencortyx.stores.data import Views
    with Views(workspace_id, [ref]) as v:
        view = next(iter(v.names), None)
        return _profile_view(v, view, columns) if view else {}


def _profile_view(v, view: str, columns: list[dict]) -> dict:
    out: dict = {}
    total = v.query(f'SELECT count(*) AS n FROM "{view}"').column("n")[0].as_py() or 0
    for c in columns:
        name = c["name"]
        q = '"' + str(name).replace('"', '""') + '"'
        try:
            r = v.query(f'SELECT count(*) - count({q}) AS nulls, approx_count_distinct({q}) AS d '
                        f'FROM "{view}"').to_pylist()[0]
        except Exception:                                     # noqa: BLE001 - a type DuckDB cannot count
            continue
        prof = {"null_pct": round(100.0 * (r["nulls"] or 0) / total, 2) if total else 0.0,
                "distinct_count": int(r["d"] or 0), "pii": is_pii(name)}
        if _is_category(c, prof, total):
            vs = v.query(f'SELECT CAST({q} AS VARCHAR) AS v, count(*) AS n FROM "{view}" '
                         f'WHERE {q} IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT {VALUE_SET_MAX + 1}'
                         ).to_pylist()
            if len(vs) <= VALUE_SET_MAX:
                prof["value_set"] = {x["v"]: int(x["n"]) for x in vs}
        out[name] = prof
    return out


def profile_arrow(table, columns: list[dict]) -> dict:
    """`profile` over an Arrow table in memory - a sample of a source that is not copied."""
    from opencortyx.stores.data import Views
    with Views(None, [], root="") as v:
        v.con.register("sample", table)
        v.names["sample"] = "sample"
        return _profile_view(v, "sample", columns)


def _is_category(col: dict, prof: dict, total: int) -> bool:
    """A short, repeating, non-personal text value: a status, a region, a type."""
    if prof["pii"] or not total or prof["distinct_count"] > VALUE_SET_MAX:
        return False
    t = str(col.get("type") or col.get("data_type") or "").lower()
    if any(x in t for x in ("int", "float", "double", "decimal", "numeric", "date", "time", "bool")):
        return False
    if col.get("primary_key") or col.get("foreign_key"):
        return False
    return prof["distinct_count"] < max(2, total * 0.5)


async def store_file_table(workspace_id, asset_id, *, path: str, filename: str,
                           overrides: Optional[dict], name: str, key_columns: list,
                           columns: list[dict], source_key: str, collection_id=None) -> dict:
    """A tabular file's rows into its managed Delta table, registered and profiled."""
    import anyio

    from opencortyx.core.dataplane import assets
    from opencortyx.ingest.formats.structured import tabular as _T
    from opencortyx.stores.data import AssetRef

    def go():
        res = write_rows(workspace_id, asset_id,
                         lambda: _T.iter_arrow_batches(path, filename, overrides), key_columns)
        ref = AssetRef(asset_id=str(asset_id), name=name, kind=STRUCTURED)
        res["profile"] = profile(workspace_id, ref, columns) if res["rows"] else {}
        return res
    res = await anyio.to_thread.run_sync(go)
    await assets.register(workspace_id, asset_id, kind=STRUCTURED, name=name, storage="managed",
                          format=_T.file_format(filename), location=f"{STRUCTURED}/{asset_id}",
                          version_ref=res.get("version"),
                          metadata={"filename": filename, "source_key": source_key,
                                    "rows": res["rows"], "key_columns": key_columns,
                                    "collection_id": str(collection_id) if collection_id else None})
    return res


async def register_live_table(workspace_id, asset_id, *, connector_id, kind: str, table: str,
                              name: str, columns: list[dict], source_key: str) -> dict:
    """A table in a live database, registered in place (read through DuckDB ATTACH, never copied)
    and profiled through the same attach. `kind`: postgres, mysql or sqlite."""
    import anyio

    from opencortyx.core.dataplane import assets
    from opencortyx.core.dataplane.connectors import attach_dsn
    from opencortyx.stores.data import AssetRef
    observed = datetime.now(timezone.utc).isoformat()
    await assets.register(workspace_id, asset_id, kind=STRUCTURED, name=name, storage="external",
                          format=kind, location=table, connector_id=connector_id, version_ref=observed,
                          metadata={"source_key": source_key, "table": table, "live": True})
    try:
        dsn = await attach_dsn(workspace_id, connector_id, kind)
        ref = AssetRef(asset_id=str(asset_id), name=name, kind=STRUCTURED, storage="external",
                       format=kind, location=table, dsn=dsn, options={"table": table})
        prof = await anyio.to_thread.run_sync(lambda: profile(workspace_id, ref, columns))
    except Exception as e:                                    # noqa: BLE001 - registration stands
        log.warning("dataplane.live_profile_failed", table=table, error=str(e)[:200])
        prof = {}
    return {"version": observed, "profile": prof}


async def register_lake_table(workspace_id, asset_id, *, connector_id, fmt: str, location: str,
                              name: str, columns: list[dict], source_key: str,
                              version_ref: Optional[str] = None) -> dict:
    """A customer's Delta / Iceberg / Parquet table, registered where it is. `version_ref` is its
    Delta version or Iceberg snapshot, compared on the next sync to detect a change."""
    from opencortyx.core.dataplane import assets
    await assets.register(workspace_id, asset_id, kind=STRUCTURED, name=name, storage="external",
                          format=fmt, location=location, connector_id=connector_id,
                          version_ref=version_ref,
                          metadata={"source_key": source_key, "columns": [c.get("name") for c in columns]})
    return {"version": version_ref}


def attribute_profile_props(prof: dict) -> dict:
    """What an Attribute node carries from a profile. No sample values: only a category's value_set."""
    if not prof:
        return {}
    out = {"null_pct": prof.get("null_pct"), "distinct_count": prof.get("distinct_count"),
           "pii": bool(prof.get("pii"))}
    if prof.get("value_set") and not prof.get("pii"):
        out["value_set"] = prof["value_set"]
    return out
