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
    semantic: bool = False          # a property of an object type (its role was read from the values)

    @property
    def numeric(self) -> bool:
        t = self.data_type.lower()
        return any(x in t for x in ("int", "float", "double", "decimal", "numeric", "real", "number"))

    @property
    def temporal(self) -> bool:
        if self.semantic:
            return self.role == "time"            # an object-type property: its role read from the values
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
    key_columns: list = field(default_factory=list)      # the columns a record is identified by
    assets: list = field(default_factory=list)           # an object type's table: the assets bound to it

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
    #: (view, SQL) of each accepted object type's logical table (knowledge/semantic/views.py)
    logical: list = field(default_factory=list)

    def scoped(self, asset_ids) -> "Schema":
        """Only the tables of these assets - a question scoped to collections reads their data alone.
        An object type spans every collection its records are in, so it is left out."""
        keep = {str(a) for a in asset_ids}
        tables = {v: t for v, t in self.tables.items() if t.kind != "object" and t.asset_id in keep}
        return Schema(tables=tables, joins=[j for j in self.joins if j.left in tables and j.right in tables],
                      refs=[r for r in self.refs if r.asset_id in keep], logical=[])

    def needs(self, views) -> dict:
        """What a query over `views` must open: only those assets (an object type's table, the assets
        bound to it), under the names the workspace gives them - instead of every asset it has."""
        want: set = set()
        logical = []
        defs = dict(self.logical)
        for v in views or []:
            t = self.tables.get(v) or self.tables.get(str(v).lower())
            if t is None:
                continue
            if t.kind == "object":
                want |= set(t.assets)
                if t.view in defs:
                    logical.append((t.view, defs[t.view]))
            else:
                want.add(t.asset_id)
        names = {t.asset_id: t.view for t in self.tables.values() if t.kind != "object"}
        return {"assets": [r for r in self.refs if r.asset_id in want], "logical": logical,
                "view_names": {a: names[a] for a in want if a in names}}

    def objects(self) -> list:
        """The object-type tables: one per accepted type of the semantic layer."""
        return [t for t in self.tables.values() if t.kind == "object"]

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
                  version_ref=getattr(row, "version_ref", None),
                  key_columns=[str(k) for k in (meta.get("key_columns") or []) if k])
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
    await _semantic_layer(workspace_id, out, by_asset, taken)
    return out


async def _semantic_layer(workspace_id, out: Schema, by_asset: dict, taken: set) -> None:
    """Each accepted object type as a table over every asset bound to it, and each accepted link
    type as the join between two of them."""
    from opencortyx.knowledge import semantic
    from opencortyx.knowledge.semantic import views as sviews
    try:
        layer = (await semantic.load(workspace_id)).accepted()
    except Exception:                                         # noqa: BLE001 - no layer, asset tables only
        return
    asset_views = {aid: t.view for aid, t in by_asset.items()}
    view_of: dict = {}
    for ot in layer.types:
        sql = sviews.type_sql(ot, asset_views)
        if not sql:
            continue
        view = sviews.view_name(ot, taken)
        view_of[ot.id] = view
        out.logical.append((view, sql))
        t = Table(asset_id=ot.id, view=view, name=ot.name, kind="object", storage="logical",
                  format=ot.kind, rows=ot.rows, description=ot.description,
                  key_columns=list(ot.keys), assets=sorted({b.asset_id for b in ot.bindings}))
        for p in ot.properties:
            t.columns.append(Column(name=p.name, data_type={"number": "DOUBLE", "date": "DATE"}.get(p.data_type, "VARCHAR"),
                                    description=p.description, value_set=dict(p.value_set or {}), role=p.role,
                                    primary_key=(p.name in ot.keys), semantic=True))
        out.tables[view] = t
    for lk in layer.links:
        if lk.source in view_of and lk.target in view_of:
            out.joins.append(Join(view_of[lk.source], lk.source_prop, view_of[lk.target], lk.target_prop, "declared"))


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


def named_table(schema: Schema, question: str) -> Optional[Table]:
    """The table the question names outright ("in licences-17", "the orders table"), if one."""
    qtext = " " + " ".join(words(question)) + " "
    hits = [t for t in schema.tables.values()
            if any(f" {' '.join(words(nm))} " in qtext for nm in (t.name, t.view) if words(nm))]
    # "licences 17" names licences-17, not a table called just "licences": the longest name wins.
    hits.sort(key=lambda t: -len(words(t.name)))
    return hits[0] if hits else None


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
        # The table named outright ("in licences 00", "the orders table"): that one, not a lookalike.
        for nm in (t.name, t.view):
            phrase = " ".join(words(nm))
            if phrase and f" {phrase} " in qtext:
                s += 10.0
                break
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
