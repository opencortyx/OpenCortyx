"""MongoDB through the adapter contract: sampled schema, pushed-down scans, change streams.

DuckDB has no MongoDB extension, so a collection reaches it as Arrow batches: `scan` turns the
columns, filters and limit into a Mongo `find` (projection + query + limit), so only the documents
needed leave MongoDB. Nested documents and arrays stay JSON text in their column; the walker reads
the documents themselves (`documents`), exactly as it reads a JSON file.

Read-only by construction: nothing here writes. Point the connector at a secondary (read replica)
and give it a read-only user.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Iterator, Optional

from opencortyx.stores.data.adapters.base import Column, Filter, TableSchema

#: Documents sampled to infer a collection's fields.
SAMPLE = 1000
_OPS = {"=": "$eq", "!=": "$ne", "<": "$lt", "<=": "$lte", ">": "$gt", ">=": "$gte", "in": "$in"}


def _client(uri: str, timeout_ms: int = 10_000):
    from pymongo import MongoClient
    return MongoClient(uri, serverSelectionTimeoutMS=timeout_ms, readPreference="secondaryPreferred")


def _scalar(v):
    """A BSON value as something Arrow and JSON hold: ObjectId and dates as text, nested as JSON."""
    try:
        from bson import ObjectId
        if isinstance(v, ObjectId):
            return str(v)
    except ImportError:
        pass
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str, ensure_ascii=False)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def plain(doc: dict) -> dict:
    """A document as plain JSON values (ObjectId, dates and DBRefs as text), recursively."""
    def conv(v):
        if isinstance(v, dict):
            return {str(k): conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return _scalar(v)
    return conv(doc)


def _kind(v) -> str:
    try:
        from bson import DBRef, ObjectId
        if isinstance(v, ObjectId):
            return "objectid"
        if isinstance(v, DBRef):
            return "dbref"
    except ImportError:
        pass
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    if hasattr(v, "isoformat"):
        return "date"
    return "string" if v is not None else "null"


class MongoAdapter:
    def __init__(self, uri: str, database: Optional[str] = None):
        self.client = _client(uri)
        self.db = self.client.get_database(database) if database else self.client.get_default_database()

    def close(self) -> None:
        self.client.close()

    def collections(self) -> list[str]:
        return sorted(n for n in self.db.list_collection_names() if not n.startswith("system."))

    def schema(self, source: Optional[str] = None) -> list[TableSchema]:
        """Field paths and types inferred from a sample: how often each appears, mixed types, and
        reference candidates (ObjectId / DBRef fields, `*_id` names)."""
        out = []
        for name in ([source] if source else self.collections()):
            coll = self.db[name]
            seen: dict = defaultdict(Counter)
            n = 0
            for doc in coll.aggregate([{"$sample": {"size": SAMPLE}}]):
                n += 1
                for k, v in doc.items():
                    seen[str(k)][_kind(v)] += 1
            cols = []
            for k, kinds in seen.items():
                main = kinds.most_common(1)[0][0]
                ref = None
                if k != "_id" and (main in ("objectid", "dbref") or k.endswith("_id")):
                    ref = (k[:-3] if k.endswith("_id") else k, "_id")
                cols.append(Column(name=k, type=main if len(kinds) == 1 else "mixed",
                                   nullable=sum(kinds.values()) < n, primary_key=(k == "_id"),
                                   references=ref,
                                   comment=f"in {round(100 * sum(kinds.values()) / max(1, n))}% of sampled documents"))
            try:
                est = coll.estimated_document_count()
            except Exception:                                 # noqa: BLE001 - a view has no count
                est = None
            out.append(TableSchema(name=name, columns=cols, row_estimate=est))
        return out

    @staticmethod
    def query(filters: Optional[list[Filter]]) -> dict:
        q: dict = {}
        for f in filters or []:
            if f.op == "is_null":
                q[f.column] = None
            elif f.op in _OPS:
                q.setdefault(f.column, {})[_OPS[f.op]] = f.values() if f.op == "in" else f.value
            else:
                raise ValueError(f"unsupported filter operator {f.op!r}")
        return q

    def documents(self, source: str, filters: Optional[list[Filter]] = None,
                  limit: Optional[int] = None) -> Iterator[dict]:
        """Whole documents as plain JSON - what the walker reads, like records of a JSON file."""
        cur = self.db[source].find(self.query(filters))
        if limit:
            cur = cur.limit(int(limit))
        for doc in cur:
            yield plain(doc)

    def scan(self, source: str, columns: Optional[list[str]] = None,
             filters: Optional[list[Filter]] = None, limit: Optional[int] = None,
             batch_size: int = 10_000) -> Iterator:
        """Arrow batches of top-level fields, projection and filters pushed into the `find`."""
        import pyarrow as pa
        proj = {c: 1 for c in columns} if columns else None
        cur = self.db[source].find(self.query(filters), proj, batch_size=batch_size)
        if limit:
            cur = cur.limit(int(limit))
        buf: list = []
        names: list = list(columns or [])
        for doc in cur:
            row = {str(k): _scalar(v) for k, v in doc.items()}
            for k in row:
                if k not in names:
                    names.append(k)
            buf.append(row)
            if len(buf) >= batch_size:
                yield pa.RecordBatch.from_pylist(buf, schema=_schema(buf, names))
                buf = []
        if buf:
            yield pa.RecordBatch.from_pylist(buf, schema=_schema(buf, names))

    def changes(self, source: str, since: Optional[str] = None) -> Optional[Iterator]:
        """Changed documents since a resume token (change streams need a replica set). None when the
        deployment cannot say - the caller rescans instead."""
        try:
            stream = self.db[source].watch(full_document="updateLookup",
                                           resume_after={"_data": since} if since else None,
                                           max_await_time_ms=1000)
        except Exception:                                     # noqa: BLE001 - standalone server
            return None

        def gen():
            with stream:
                while stream.alive:
                    change = stream.try_next()
                    if change is None:
                        break
                    if change.get("fullDocument") is not None:
                        yield plain(change["fullDocument"])
        return gen()


def _schema(rows: list[dict], names: list):
    import pyarrow as pa
    from opencortyx.stores.data.layout import arrow_type
    return pa.schema([pa.field(n, arrow_type([r.get(n) for r in rows])) for n in names])


def database_of(uri: str) -> Optional[str]:
    from urllib.parse import urlsplit
    path = (urlsplit(uri).path or "").strip("/")
    return path or None
