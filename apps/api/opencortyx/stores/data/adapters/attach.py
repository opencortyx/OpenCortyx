"""Databases DuckDB attaches natively: PostgreSQL, MySQL/MariaDB, SQLite. Always READ_ONLY.

DuckDB pushes column selection and filters down to the database, so a live table is queried in
place and nothing is copied. The connection string is resolved from the connector's secret for one
DuckDB connection only and is never logged.
"""
from __future__ import annotations

import re
from typing import Iterator, Optional

from opencortyx.stores.data.adapters.base import Column, Filter, TableSchema

NATIVE = {"postgres": "POSTGRES", "mysql": "MYSQL", "sqlite": "SQLITE"}
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _ident(name: str) -> str:
    """A quoted SQL identifier. Names come from catalogues, not users, but are quoted regardless."""
    return '"' + str(name).replace('"', '""') + '"'


def attach(con, alias: str, kind: str, dsn: str) -> None:
    if kind not in NATIVE:
        raise ValueError(f"{kind!r} is not attached natively; it needs an adapter")
    if not _IDENT.match(alias):
        raise ValueError(f"bad alias {alias!r}")
    from opencortyx.stores.data.duck import load_extensions
    load_extensions(con, [kind])
    lit = "'" + str(dsn).replace("'", "''") + "'"
    con.execute(f"ATTACH {lit} AS {alias} (TYPE {NATIVE[kind]}, READ_ONLY)")


def qualified(alias: str, kind: str, table: str) -> str:
    """`alias.schema.table` from "schema.table" or "table" (SQLite has no schemas)."""
    parts = [p for p in str(table).split(".") if p]
    if not parts:
        raise ValueError("no table named")
    if kind == "sqlite":
        parts = parts[-1:]
    return ".".join([alias] + [_ident(p) for p in parts])


class AttachedDatabase:
    """The SourceAdapter contract over an attached database, for callers that want batches."""

    def __init__(self, con, alias: str, kind: str, dsn: str):
        self.con, self.alias, self.kind = con, alias, kind
        attach(con, alias, kind, dsn)

    def schema(self, source: Optional[str] = None) -> list[TableSchema]:
        rows = self.con.execute(
            "SELECT schema_name, table_name, column_name, data_type, is_nullable "
            "FROM duckdb_columns() WHERE database_name = ? ORDER BY schema_name, table_name, column_index",
            [self.alias]).fetchall()
        out: dict[str, TableSchema] = {}
        for schema, table, col, typ, nullable in rows:
            name = table if self.kind == "sqlite" else f"{schema}.{table}"
            if source and name != source and table != source:
                continue
            out.setdefault(name, TableSchema(name=name)).columns.append(
                Column(name=col, type=str(typ), nullable=bool(nullable)))
        return list(out.values())

    def scan(self, source: str, columns: Optional[list[str]] = None,
             filters: Optional[list[Filter]] = None, limit: Optional[int] = None,
             batch_size: int = 10_000) -> Iterator:
        cols = ", ".join(_ident(c) for c in columns) if columns else "*"
        sql = f"SELECT {cols} FROM {qualified(self.alias, self.kind, source)}"
        params: list = []
        where = []
        for f in filters or []:
            if f.op == "is_null":
                where.append(f"{_ident(f.column)} IS NULL")
            elif f.op == "in":
                vals = list(f.value or [])
                where.append(f"{_ident(f.column)} IN ({', '.join('?' for _ in vals)})" if vals else "FALSE")
                params.extend(vals)
            elif f.op in ("=", "!=", "<", "<=", ">", ">="):
                where.append(f"{_ident(f.column)} {f.op} ?")
                params.append(f.value)
            else:
                raise ValueError(f"unsupported filter operator {f.op!r}")
        if where:
            sql += " WHERE " + " AND ".join(where)
        if limit:
            sql += f" LIMIT {int(limit)}"
        reader = self.con.execute(sql, params).fetch_record_batch(batch_size)
        yield from reader

    def changes(self, source: str, since: Optional[str] = None) -> Optional[Iterator]:
        """With a declared `updated_at` column the caller filters on it through `scan`; the database
        itself offers no change feed through ATTACH."""
        return None
