"""Publishing the semantic layer to the graph: one node per real entity, one edge per link.

Only what is chosen becomes knowledge: an entity type a mapping marks `materialize`, or - with the
`knowledge_publish` setting - every accepted entity type the Detector found. Event tables (no key)
never become nodes; their rows are read through SQL.

    node     one per key value, its id derived from the workspace, the type and the key, so the
             same customer from the CRM and from billing is ONE node and a second publish updates it.
             It carries the key, the type's category and text properties (not its measures - those
             are answered from the rows, nor its prose), and the assets it was read from; its
             label is the record's name (the type's `label` property) where it has one
    edge     one per accepted link whose two types are both published: the reference's value
             matched to the target's key in SQL, drawn as the link's relationship type
    identity each node's key is recorded in the identity map (source_key) for every asset that holds
             it, which is how a question or an extractor finds the node from a source's own id

The type's `identify_by` is its key (store.save_detected), so entities extracted later from
documents resolve onto these nodes through the resolution engine.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from opencortyx.core.observability import get_logger
from opencortyx.knowledge.semantic import store
from opencortyx.knowledge.semantic.views import conflicts_sql, q, type_sql, view_name

log = get_logger(__name__)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/semantic-entity")
#: Records published per type at most; past it the rest stay readable through SQL only.
MAX_PER_TYPE = 100_000
BATCH = 500


def entity_id(workspace_id, type_name: str, key) -> str:
    return str(uuid.uuid5(_NS, f"{workspace_id}:{type_name}:{key}"))


def _chosen(layer, everything: bool) -> list:
    """The types to publish: those a mapping marks for the graph - one row one node, identified or
    not - and, with `everything`, every identified type."""
    return [t for t in layer.types if t.materialize or (everything and t.kind == "entity" and t.keys)]


def _inherited(t) -> set:
    """Fields every binding reads from the record a row sits in ("^.question_date"): copies of the
    enclosing record's values, not the row's own."""
    read = [b.fields for b in t.bindings]
    return {f for f in {f for fields in read for f in fields}
            if all(str(fields[f]).startswith("^") for fields in read if f in fields)}


def row_key(t, row: dict) -> str:
    """A published record's identity: its key; a row of a type without one (an event: a turn, a sale)
    is named by its own knowledge values and its references (which record it belongs to), so the same
    row read again is the same node. A value copied from the enclosing record is not the row's: the
    question's date corrected leaves its turns the same turns."""
    if t.keys:
        return "|".join(str(row.get(k)) for k in t.keys)
    copied = _inherited(t) - {p.name for p in t.properties if p.role == "reference"}
    own = {f: row.get(f) for f in t.published_fields() if row.get(f) is not None and f not in copied}
    return "row:" + hashlib.sha1(json.dumps(own, sort_keys=True, default=str).encode()).hexdigest()


def fields_gone(t, specs: list, written: list) -> dict:
    """{fields to remove: node ids}: a field of the type a node still carries that its rows no longer
    give (its only file deleted, a column dropped). Nodes and specs are matched by id as text."""
    fields = {pr.name for pr in t.properties} | set(t.published_fields())
    now = {str(sp["node_id"]): sp["properties"] for sp in specs}
    gone: dict = {}
    for x in written:
        left = tuple(sorted(f for f in fields if f in (x.properties or {}) and f not in now.get(str(x.id), {})))
        if left:
            gone.setdefault(left, []).append(x.id)
    return gone


def event_label(t, row: dict):
    """What an event is called: its free text (a turn's words), else its first short text."""
    rank = {"prose": 0, "text": 1}
    roles = {p.name: p.role for p in t.properties}
    texts = [f for f in t.published_fields() if isinstance(row.get(f), str) and row[f].strip()]
    texts.sort(key=lambda f: rank.get(roles.get(f), 2))
    return str(row[texts[0]])[:120] if texts else None


async def publish(workspace_id, *, everything: bool = False) -> dict:
    """Publish the chosen entity types and their links. `everything`: every accepted entity type
    (the `knowledge_publish` setting), not only those a mapping marks `materialize`."""
    import anyio

    from opencortyx.core.dataplane import assets, source_keys
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.stores.data import Views, layout

    layer = (await store.load(workspace_id)).accepted()
    types = _chosen(layer, everything)
    if not types:
        return {"types": 0, "nodes": 0, "edges": 0}
    refs = await assets.refs(workspace_id, kinds=[layout.STRUCTURED, layout.SEMISTRUCTURED])
    taken: set = set()
    views = {r.asset_id: layout.view_name(r.name, taken) for r in refs}
    logical = {t.name: (view_name(t, taken), type_sql(t, views)) for t in layer.types}
    logical = {k: v for k, v in logical.items() if v[1]}
    names = {t.name for t in types}

    def read():
        out: dict = {"nodes": {}, "edges": [], "conflicts": {}}
        with Views(workspace_id, refs, lock=True, logical=list(logical.values())) as v:
            for t in types:
                if t.name not in logical or logical[t.name][0] not in v.names:
                    # Its view did not build: the other types still publish.
                    log.warning("semantic_layer.publish_skipped", type=t.name, reason="view did not build")
                    continue
                # Its knowledge columns (store.ObjectType.published_fields): the rest stays in the rows.
                present = {f for b in t.bindings for f in b.fields}
                keep = [f for f in t.published_fields() if f in present]
                if not keep:
                    continue
                cols = ", ".join(q(c) for c in keep) + (", _assets" if t.keys else ", _asset AS _assets")
                out["nodes"][t.name] = v.query(f"SELECT {cols} FROM {q(logical[t.name][0])} "
                                               f"LIMIT {MAX_PER_TYPE}").to_pylist()
                differ = conflicts_sql(t, views)
                if differ:
                    try:
                        out["conflicts"][t.name] = v.query(differ).to_pylist()
                    except Exception as e:                       # noqa: BLE001 - the records still publish
                        log.warning("semantic_layer.conflicts_skipped", type=t.name, error=str(e)[:200])
            for lk in layer.links:
                if lk.source in names and lk.target in names and lk.source in logical and lk.target in logical                         and logical[lk.source][0] in v.names and logical[lk.target][0] in v.names:
                    s, d = q(logical[lk.source][0]), q(logical[lk.target][0])
                    src = layer.type(lk.source)
                    # an identified source by its key; an event's row by its own values (`row_key`)
                    have = {f for b in src.bindings for f in b.fields}
                    ends = (f"CAST(s.{q(src.key)} AS VARCHAR) AS a" if src.keys else
                            ", ".join(f"s.{q(f)}" for f in src.published_fields() if f in have))
                    rows = v.query(f"SELECT DISTINCT {ends}, CAST(d.{q(lk.target_prop)} AS VARCHAR) AS b "
                                   f"FROM {s} AS s JOIN {d} AS d ON CAST(s.{q(lk.source_prop)} AS VARCHAR) = "
                                   f"CAST(d.{q(lk.target_prop)} AS VARCHAR)").to_pylist()
                    out["edges"].append((lk, rows))
        return out
    data = await anyio.to_thread.run_sync(read)

    repo = NodeRepo()
    by_name = {b.asset_name: b.asset_id for t in types for b in t.bindings}
    n_nodes = n_edges = 0
    # a type read in full: its nodes are all there is, so one it no longer yields is retired
    kept = {t.name: set() for t in types if t.name in data["nodes"] and len(data["nodes"][t.name]) < MAX_PER_TYPE}
    for t in types:
        rows = data["nodes"].get(t.name) or []
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            specs, keys = [], []
            for r in batch:
                key = row_key(t, r)
                nid = entity_id(workspace_id, t.name, key)
                carried = set(t.published_fields())          # the knowledge columns; the rest in Delta
                props = {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                         for k, v in r.items() if v is not None and k in carried}
                props["__sources"] = r.get("_assets") or ""
                named = r.get(t.label) if t.label else None
                if named in (None, "") and not t.keys:      # an event: called by its text
                    named = event_label(t, r)
                if t.name in kept:
                    kept[t.name].add(str(nid))
                specs.append({"node_id": nid, "type": t.name, "label": str(named) if named not in (None, "") else key,
                              "properties": props})
                for name in str(r.get("_assets") or "").split(", "):
                    if name in by_name:
                        keys.append((by_name[name], key, nid))
            await repo.upsert_many(workspace_id, specs, date_link=False)
            # a published record is in no asset's subtree, so no asset's embedding job reaches it
            written = [x for x in await repo.get_many(workspace_id, [sp["node_id"] for sp in specs]) if x is not None]
            for left, ids in fields_gone(t, specs, written).items():
                await repo.patch_properties(workspace_id, ids, remove=list(left))
            pending = await repo.unembedded(workspace_id, written)
            if pending:
                from opencortyx.tasks.graph import embed_later
                embed_later(workspace_id, [getattr(x, "id", x) for x in pending])
            by_asset: dict = {}
            for aid, key, nid in keys:
                by_asset.setdefault(aid, []).append((key, t.name, nid))
            for aid, items in by_asset.items():
                await source_keys.record(workspace_id, aid, items, match_method="semantic_layer")
            # Lineage: each entity points at every asset it was read from, as extracted facts do.
            await repo.add_edges(workspace_id=workspace_id, _quarantine=False, edges=[
                {"source_id": nid, "target_id": aid, "type": "FROM_ASSET", "properties": {"key": key}}
                for aid, key, nid in keys])
            n_nodes += len(specs)
    for lk, rows in data["edges"]:
        src = next(t for t in types if t.name == lk.source)
        edges = [{"source_id": entity_id(workspace_id, lk.source, r["a"] if "a" in r else row_key(src, r)),
                  "target_id": entity_id(workspace_id, lk.target, r["b"]), "type": lk.name,
                  "properties": {"origin": lk.origin, "via": f"{lk.source_prop}->{lk.target_prop}"}}
                 for r in rows if r.get("a", "") is not None and r["b"] is not None]
        for i in range(0, len(edges), BATCH):
            await repo.add_edges(workspace_id=workspace_id, edges=edges[i:i + BATCH], _quarantine=False)
        n_edges += len(edges)
    removed = await _retire(workspace_id, {t.name for t in types}, kept)
    conflicts = await _flag_conflicts(workspace_id, types, data["conflicts"], by_name)
    log.info("semantic_layer.published", types=len(types), nodes=n_nodes, edges=n_edges, removed=removed,
             conflicts=conflicts)
    return {"types": len(types), "nodes": n_nodes, "edges": n_edges, "removed": removed, "conflicts": conflicts}


def conflict_decisions(workspace_id, types, found: dict, by_name: dict, carried: dict) -> list:
    """A CONTRADICTS decision for Review per record and attribute the files disagree on: `other` the
    value the record's node carries (`carried`: node id -> its properties), `value` another file's,
    `sources` every value and the file it came from. Nothing is overwritten or chosen here."""
    from opencortyx.resolution import records
    out = []
    for t in types:
        for r in found.get(t.name) or []:
            seen = sorted(r["seen"], key=lambda s: (str(s["asset"]), str(s["value"])))
            nid = entity_id(workspace_id, t.name, "|".join(str(r.get(k)) for k in t.keys))
            has = str((carried.get(nid) or {}).get(r["attribute"], seen[0]["value"]))
            new = next((s for s in seen if str(s["value"]) != has), seen[-1])
            aid = by_name.get(new["asset"])
            out.append(records._decision(
                workspace_id, uuid.UUID(str(aid)) if aid else None, t.name, records.CONTRADICTS, nid, nid,
                attribute=r["attribute"], method="key", review=True,
                evidence={"value": new["value"], "other": has, "sources": seen}))
    return out


async def _flag_conflicts(workspace_id, types, found: dict, by_name: dict) -> int:
    """Record `conflict_decisions` once each (a decision's id is its content's)."""
    from opencortyx.core.substrate import NodeRepo
    ids = [entity_id(workspace_id, t.name, "|".join(str(r.get(k)) for k in t.keys))
           for t in types for r in found.get(t.name) or []]
    carried = {n.id: n.properties or {} for n in await NodeRepo().get_many(workspace_id, ids) if n} if ids else {}
    rows = conflict_decisions(workspace_id, types, found, by_name, carried)
    if rows:
        from sqlalchemy.dialects.postgresql import insert

        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionDecision
        async with V2Session() as db:
            await db.execute(insert(ResolutionDecision).values(rows).on_conflict_do_nothing())
            await db.commit()
    return len(rows)


async def _retire(workspace_id, current: set, kept: dict | None = None) -> int:
    """Entities an earlier publish made of a type no longer published (renamed, rejected, unmarked),
    and of a type read in full (`kept`: type -> the ids this publish wrote) whose row is gone - a
    deleted row, an event whose own values changed: found through the identity map this module
    writes, and removed with their keys."""
    from sqlalchemy import delete, select

    from opencortyx.core.db import V2Session
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.data import SourceKey
    async with V2Session() as db:
        rows = (await db.execute(select(SourceKey.node_id, SourceKey.entity_type).where(
            SourceKey.workspace_id == workspace_id, SourceKey.match_method == "semantic_layer"))).all()
        kept = kept or {}
        stale = sorted({str(n) for n, t in rows
                        if t not in current or (t in kept and str(n) not in kept[t])})
        if stale:
            await db.execute(delete(SourceKey).where(SourceKey.workspace_id == workspace_id,
                                                     SourceKey.match_method == "semantic_layer",
                                                     SourceKey.node_id.in_([uuid.UUID(n) for n in stale])))
            await db.commit()
    if stale:
        await NodeRepo().delete_nodes(workspace_id, stale)
    return len(stale)


async def link_schema(workspace_id, layer) -> int:
    """The detected links between the fields themselves: a column or key whose values are another
    type's identifiers gets a JOINS_TO edge to that identifier's column or key, for every pair of
    assets the two types live in. Schema is knowledge too, so this is drawn whatever is published -
    the same edge a declared join draws (ingest/semistructured/dataplane.link_join_rules)."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    fields: dict = {}

    async def field_node(asset_id: str, name: str):
        if asset_id not in fields:
            fields[asset_id] = {str((n.properties or {}).get("physical_column") or n.label): n.id
                                for n in await repo.find(workspace_id, types=["Attribute"],
                                                         props={"parent_table_id": asset_id})}
        return fields[asset_id].get(name)

    edges = []
    for lk in layer.links:
        src, dst = layer.type(lk.source), layer.type(lk.target)
        if src is None or dst is None:
            continue
        for a in src.bindings:
            fa = await field_node(a.asset_id, a.fields.get(lk.source_prop) or lk.source_prop)
            if fa is None:
                continue
            for b in dst.bindings:
                fb = await field_node(b.asset_id, b.fields.get(lk.target_prop) or lk.target_prop)
                if fb is not None and fb != fa:
                    edges.append({"source_id": fa, "target_id": fb, "type": "JOINS_TO", "properties": {
                        "relation": lk.name, "from_key": lk.source_prop, "to_key": lk.target_prop,
                        "coverage": lk.coverage, "cardinality": "MANY_TO_ONE",
                        "relationship_type": "INFERRED_REFERENCE" if lk.origin == "inferred" else "DECLARED_REFERENCE"}})
    for i in range(0, len(edges), BATCH):
        await repo.add_edges(workspace_id=workspace_id, edges=edges[i:i + BATCH], _quarantine=False)
    log.info("semantic_layer.schema_links", links=len(layer.links), edges=len(edges))
    return len(edges)
