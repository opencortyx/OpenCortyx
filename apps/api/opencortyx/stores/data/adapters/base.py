"""The contract every live source adapter keeps: schema, scan, changes.

A source DuckDB cannot attach natively (MongoDB, SQL Server through ADBC/ODBC, ...) gets an adapter
that hands DuckDB Arrow record batches, with the columns, filters and limit pushed down into the
source's own query language. DuckDB registers the stream as a view for one connection, so joins and
aggregates still run in DuckDB, across sources. Nothing is copied and nothing is kept.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Optional, Protocol


@dataclass
class Column:
    name: str                          # a column, or a field path for documents ("$.orders[].id")
    type: str
    nullable: bool = True
    primary_key: bool = False
    #: A reference this column holds, when the source declares or reveals one: (table, column).
    references: Optional[tuple[str, str]] = None
    comment: str = ""


@dataclass
class TableSchema:
    name: str                          # table, view or collection
    columns: list[Column] = field(default_factory=list)
    row_estimate: Optional[int] = None


@dataclass
class Filter:
    """One pushed-down predicate: column, operator (=, !=, <, <=, >, >=, in, is_null), value."""
    column: str
    op: str
    value: object = None

    def values(self) -> list:
        """The values of an `in` filter, as a list (one value becomes a list of one)."""
        v = self.value
        if v is None:
            return []
        return list(v) if isinstance(v, (list, tuple, set, frozenset)) else [v]


class SourceAdapter(Protocol):
    def schema(self, source: str | None = None) -> list[TableSchema]:
        """Tables or collections, their columns or field paths, types, keys and references."""

    def scan(self, source: str, columns: Optional[list[str]] = None,
             filters: Optional[list[Filter]] = None, limit: Optional[int] = None,
             batch_size: int = 10_000) -> Iterator:
        """pyarrow RecordBatches, with columns, filters and limit pushed into the source."""

    def changes(self, source: str, since: Optional[str] = None) -> Optional[Iterator]:
        """Records changed since a watermark, or None when the source cannot say (rescan instead)."""
