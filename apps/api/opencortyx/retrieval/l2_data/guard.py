"""The SQL guard: generated SQL runs only if it is one read-only SELECT over this workspace's views.

DuckDB can read any file and any database it can reach, so this is not optional. A query passes when:

  - it parses (sqlglot, DuckDB dialect) as exactly ONE statement, a SELECT (CTEs and set operations
    of SELECTs included);
  - every table it reads is one of the views registered for this workspace (or a CTE of its own);
  - it calls no table function (read_csv, delta_scan, postgres_scan, glob, ...) and none of the
    functions that read or write outside the views;
  - it contains no ATTACH, COPY, PRAGMA, SET, INSTALL, LOAD, CREATE, INSERT, UPDATE, DELETE, DROP,
    ALTER, EXPORT, IMPORT or CALL anywhere.

A LIMIT is added when the query has none. The connection it runs on is locked as well
(stores/data Views(lock=True)) - two layers, because each covers what the other might miss.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Rows a question-time query may return to the model.
ROW_LIMIT = 200

_FORBIDDEN_WORDS = re.compile(
    r"\b(attach|detach|copy|pragma|install|load|export|import|call|checkpoint|vacuum|"
    r"create|insert|update|delete|drop|alter|truncate|grant|revoke|set|reset|use)\b", re.I)
#: Functions that reach outside the views (files, URLs, other databases, the environment).
_FORBIDDEN_FUNCS = {"read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto",
                    "read_ndjson", "read_text", "read_blob", "read_xlsx", "delta_scan", "iceberg_scan",
                    "parquet_scan", "postgres_scan", "postgres_query", "mysql_query", "sqlite_scan",
                    "glob", "query", "query_table", "getenv", "current_setting", "duckdb_settings",
                    "duckdb_extensions", "duckdb_secrets", "duckdb_databases", "pragma_table_info",
                    "system", "shell"}


class Rejected(ValueError):
    """The query is not one the guard lets run; the message says why (fed back to the planner)."""


@dataclass
class Checked:
    sql: str
    tables: list


def check(sql: str, views, row_limit: int = ROW_LIMIT) -> Checked:
    import sqlglot
    from sqlglot import exp
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise Rejected("empty query")
    # Words are checked on the text with string literals removed, so 'Delete' as a value is fine.
    bare = re.sub(r"'(?:[^']|'')*'", "''", text)
    m = _FORBIDDEN_WORDS.search(bare)
    if m:
        raise Rejected(f"'{m.group(1).upper()}' is not allowed: only a SELECT over the workspace's tables")
    try:
        statements = [s for s in sqlglot.parse(text, read="duckdb") if s is not None]
    except Exception as e:                                    # noqa: BLE001 - sqlglot.ParseError
        raise Rejected(f"the query does not parse: {str(e)[:200]}") from None
    if len(statements) != 1:
        raise Rejected("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)) and not (
            isinstance(tree, exp.Subquery)):
        raise Rejected(f"only SELECT is allowed, not {type(tree).__name__.upper()}")
    allowed = {str(v).lower() for v in views}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    used = []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise Rejected("table functions are not allowed")
        if t.args.get("db") or t.args.get("catalog"):
            raise Rejected(f"'{t.sql()}' is not one of this workspace's tables")
        name = t.name.lower()
        if name in ctes:
            continue
        if name not in allowed:
            raise Rejected(f"'{t.name}' is not one of this workspace's tables "
                           f"({', '.join(sorted(allowed))[:300]})")
        used.append(t.name)
    for f in tree.find_all(exp.Func):
        fname = (f.sql_name() if not isinstance(f, exp.Anonymous) else str(f.this)).lower()
        if fname in _FORBIDDEN_FUNCS:
            raise Rejected(f"function {fname}() is not allowed")
    if isinstance(tree, exp.Select) and not tree.args.get("limit"):
        tree = tree.limit(row_limit)
        text = tree.sql(dialect="duckdb")
    elif not isinstance(tree, exp.Select):
        text = f"SELECT * FROM ({text}) AS q LIMIT {int(row_limit)}"
    return Checked(sql=text, tables=sorted(set(used)))
