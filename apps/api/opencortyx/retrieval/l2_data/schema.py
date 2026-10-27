"""The workspace's data, as the SQL planner sees it: tables, columns, value sets, joins.

Built from the asset registry (data_asset - managed Delta tables and external ones alike) and the
data layer of the graph (each asset's Attribute nodes with their types, descriptions, value_set and
role; JOINS_TO between them; a structured asset's FK joins). Never from rows: the planner works on
the schema, and the only values it sees are a category column's value_set - which is what lets it
write `status = 'ACTIVE'` instead of `status = 'active'` and get rows back.

View names are computed exactly as stores/data names the views (layout.view_name, in registry
order), so the SQL the planner writes runs against the connection unchanged.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from opencortyx.stores.data import layout

_CACHE_TTL = 60.0
_cache: dict = {}
_WORD = re.compile(r"[a-z0-9]+")


def words(text: str) -> list[str]:
    return _WORD.findall(str(text or "").lower().replace("_", " "))


@dataclass
class Column:
    name: str
    data_type: str = ""
    description: str = ""
    value_set: dict = field(default_factory=dict)
    role: str = ""
    pii: bool = False
    primary_key: bool = False

    @property
    def numeric(self) -> bool:
        t = self.data_type.lower()
        return any(x in t for x in ("int", "float", "double", "decimal", "numeric", "real", "number"))

    @property
    def temporal(self) -> bool:
        t = self.data_type.lower()
        return any(x in t for x in ("date", "time")) or bool(re.search(r"(date|_at|_on|issued|expir|created)$",
                                                                       self.name.lower()))


@dataclass
class Table:
    asset_id: str
    view: str
    name: str
    kind: str
    storage: str
    format: str
    columns: list[Column] = field(default_factory=list)
    description: str = ""
    rows: Optional[int] = None
    version_ref: Optional[str] = None

    def column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name.lower() == str(name).lower()), None)


@dataclass
class Join:
    left: str          # view
    left_col: str
    right: str
    right_col: str
    source: str = "declared"    # declared (FK / mapping rule) or detected


@dataclass
class Schema:
    tables: dict = field(default_factory=dict)       # view -> Table
    joins: list = field(default_factory=list)
    refs: list = field(default_factory=list)         # stores.data AssetRef, in view order

    def table_of(self, asset_id: str) -> Optional[Table]:
        return next((t for t in self.tables.values() if t.asset_id == str(asset_id)), None)

    def empty(self) -> bool:
        return not self.tables


async def load(workspace_id, *, fresh: bool = False) -> Schema:
    key = str(workspace_id)
    hit = _cache.get(key)
    if hit and not fresh and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1]
    schema = await _build(workspace_id)
    _cache[key] = (time.monotonic(), schema)
    return schema


def invalidate(workspace_id) -> None:
    _cache.pop(str(workspace_id), None)


async def _build(workspace_id) -> Schema:
    from opencortyx.core.dataplane import assets
    from opencortyx.core.substrate import NodeRepo
    refs = await assets.refs(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])
    out = Schema(refs=refs)
    if not refs:
        return out
    rows = {str(r.id): r for r in await assets.list_assets(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])}
    taken: set = set()
    by_asset: dict = {}
    for ref in refs:
        view = layout.view_name(ref.name, taken)
        row = rows.get(ref.asset_id)
        meta = dict(getattr(row, "metadata_", None) or {})
        t = Table(asset_id=ref.asset_id, view=view, name=ref.name, kind=ref.kind, storage=ref.storage,
                  format=ref.format, rows=meta.get("rows") or meta.get("records"),
                  version_ref=getattr(row, "version_ref", None))
        out.tables[view] = t
        by_asset[ref.asset_id] = t
    repo = NodeRepo()
    attr_by_id: dict = {}
    asset_nodes = {n.id: n for n in await repo.get_many(workspace_id, list(by_asset))}
    for aid, node in asset_nodes.items():
        t = by_asset.get(str(aid))
        if t is not None:
            p = node.properties or {}
            t.description = str(p.get("description") or p.get("grain") or "")
    for a in await repo.find(workspace_id, types=["Attribute"], limit=20000):
        p = a.properties or {}
        t = by_asset.get(str(p.get("parent_table_id") or ""))
        if t is None:
            continue
        col = Column(name=str(p.get("physical_column") or a.label or ""),
                     data_type=str(p.get("data_type") or ""), description=str(p.get("description") or ""),
                     value_set=dict(p.get("value_set") or {}), role=str(p.get("graph_role") or p.get("role") or ""),
                     pii=bool(p.get("pii") or p.get("is_pii")), primary_key=bool(p.get("is_primary_key")))
        if col.name:
            t.columns.append(col)
            attr_by_id[str(a.id)] = (t, col)
    # joins: data-layer JOINS_TO between Attribute nodes, and a structured asset's FK joins
    for e in await repo.edges(workspace_id, types=["JOINS_TO"], limit=20000):
        s, d = attr_by_id.get(str(e.source_id)), attr_by_id.get(str(e.target_id))
        if s and d:
            out.joins.append(Join(s[0].view, s[1].name, d[0].view, d[1].name, "declared"))
            continue
        ts, td = by_asset.get(str(e.source_id)), by_asset.get(str(e.target_id))
        p = e.properties or {}
        if ts and td and p.get("from_key") and p.get("to_key"):
            out.joins.append(Join(ts.view, p["from_key"], td.view, p["to_key"], "declared"))
    out.joins.extend(_detected_joins(out))
    return out


def _detected_joins(schema: Schema) -> list[Join]:
    """`customer_id` in one table, `customer_id` or a `customer` table's `id` in another: a join a
    planner may use when nothing was declared. Declared joins are preferred (join_path weights)."""
    found = []
    declared = {(j.left, j.left_col.lower(), j.right, j.right_col.lower()) for j in schema.joins}
    tables = list(schema.tables.values())
    for a in tables:
        for col in a.columns:
            n = col.name.lower()
            if not n.endswith("_id"):
                continue
            stem = n[:-3]
            for b in tables:
                if b.view == a.view:
                    continue
                target = None
                if b.column(n) is not None and (b.column(n).primary_key or stem in b.view):
                    target = b.column(n).name
                elif stem and stem in b.view and b.column("id") is not None:
                    target = "id"
                elif b.column(n) is not None:
                    target = b.column(n).name
                if target and (a.view, n, b.view, target.lower()) not in declared:
                    found.append(Join(a.view, col.name, b.view, target, "detected"))
    return found


def score_tables(schema: Schema, question: str) -> list[tuple[float, Table]]:
    """Tables the question touches, best first: its words in a table's name, its columns' names and
    descriptions, and - strongest - a category value it names ("EXPIRED", "APAC")."""
    q = set(words(question))
    qtext = " " + " ".join(words(question)) + " "
    ranked = []
    for t in schema.tables.values():
        s = 0.0
        name_words = set(words(t.name)) | set(words(t.view))
        stems = {w.rstrip("s") for w in q} | q
        s += 2.0 * len(stems & (name_words | {w.rstrip("s") for w in name_words}))
        for c in t.columns:
            cw = set(words(c.name))
            if cw and cw <= q | {w + "s" for w in q} | {w.rstrip("s") for w in q}:
                s += 1.0
            s += 0.3 * len(q & set(words(c.description)))
            for v in c.value_set:
                if f" {' '.join(words(v))} " in qtext and words(v):
                    s += 2.5
        if s > 0:
            ranked.append((s, t))
    ranked.sort(key=lambda x: -x[0])
    return ranked
