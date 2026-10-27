"""SQL databases DuckDB cannot attach (SQL Server, Oracle, Snowflake, BigQuery, Redshift, Trino,
ClickHouse...): read through the connector's own SQLAlchemy driver and handed to DuckDB as Arrow
batches. Same contract as every adapter; the query DuckDB plans over the result runs in DuckDB.

Pushdown: the projection, the filters and the limit are written into the source's own SELECT, so a
scan of a large table moves only what the question needs.
"""
from __future__ import annotations

from typing import Iterator, Optional

from opencortyx.stores.data.adapters.base import Column, Filter, TableSchema

#: Connector kinds served here (core/security/secrets.py SQL_DIALECTS names).
KINDS = ("mssql", "sqlserver", "oracle", "snowflake", "bigquery", "redshift", "trino", "clickhouse",
         "databricks", "athena", "db2", "mariadb_odbc")


def _quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


class ArrowSQLAdapter:
    def __init__(self, url: str):
        """`url` is the connector's SQLAlchemy URL (resolve_connector_dsn)."""
        self.url = url

    def _engine(self):
        from sqlalchemy import create_engine
        return create_engine(self.url)

    def schema(self, source: Optional[str] = None) -> list[TableSchema]:
        from sqlalchemy import inspect
        eng = self._engine()
        try:
            insp = inspect(eng)
            schema, _, table = (source or "").rpartition(".")
            names = [table] if source else insp.get_table_names()
            out = []
            for t in names:
                pk = set((insp.get_pk_constraint(t, schema=schema or None) or {}).get("constrained_columns") or [])
                fks = {c: (fk["referred_table"], (fk.get("referred_columns") or [None])[0])
                       for fk in insp.get_foreign_keys(t, schema=schema or None)
                       for c in fk.get("constrained_columns") or []}
                cols = [Column(name=c["name"], type=str(c["type"]), nullable=bool(c.get("nullable", True)),
                               primary_key=c["name"] in pk, references=fks.get(c["name"]),
                               comment=c.get("comment") or "")
                        for c in insp.get_columns(t, schema=schema or None)]
                out.append(TableSchema(name=f"{schema}.{t}" if schema else t, columns=cols))
            return out
        finally:
            eng.dispose()

    def select(self, source: str, columns: Optional[list[str]] = None,
               filters: Optional[list[Filter]] = None, limit: Optional[int] = None) -> tuple[str, dict]:
        cols = ", ".join(_quote(c) for c in columns) if columns else "*"
        table = ".".join(_quote(p) for p in source.split("."))
        where, params = [], {}
        for i, f in enumerate(filters or []):
            col = _quote(f.column)
            if f.op == "is_null":
                where.append(f"{col} IS NULL")
            elif f.op == "in":
                keys = []
                for j, v in enumerate(f.value or []):
                    params[f"p{i}_{j}"] = v
                    keys.append(f":p{i}_{j}")
                where.append(f"{col} IN ({', '.join(keys)})" if keys else "1 = 0")
            elif f.op in ("=", "!=", "<", "<=", ">", ">="):
                params[f"p{i}"] = f.value
                where.append(f"{col} {'<>' if f.op == '!=' else f.op} :p{i}")
            else:
                raise ValueError(f"unsupported filter operator {f.op!r}")
        sql = f"SELECT {cols} FROM {table}" + (f" WHERE {' AND '.join(where)}" if where else "")
        return sql, params

    def scan(self, source: str, columns: Optional[list[str]] = None,
             filters: Optional[list[Filter]] = None, limit: Optional[int] = None,
             batch_size: int = 10_000) -> Iterator:
        import pyarrow as pa
        from sqlalchemy import text
        sql, params = self.select(source, columns, filters)
        eng = self._engine()
        try:
            with eng.connect() as conn:
                res = conn.execution_options(stream_results=True).execute(text(sql), params)
                keys = list(res.keys())
                left = int(limit) if limit else None
                while True:
                    n = batch_size if left is None else min(batch_size, left)
                    if n <= 0:
                        break
                    rows = res.fetchmany(n)
                    if not rows:
                        break
                    yield pa.RecordBatch.from_pylist([dict(zip(keys, r)) for r in rows])
                    if left is not None:
                        left -= len(rows)
        finally:
            eng.dispose()

    def changes(self, source: str, since: Optional[str] = None) -> Optional[Iterator]:
        return None
