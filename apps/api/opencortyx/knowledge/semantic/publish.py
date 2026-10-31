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

import uuid

from opencortyx.core.observability import get_logger
from opencortyx.knowledge.semantic import store
from opencortyx.knowledge.semantic.views import q, type_sql, view_name

log = get_logger(__name__)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/semantic-entity")
#: Records published per type at most; past it the rest stay readable through SQL only.
MAX_PER_TYPE = 100_000
BATCH = 500


def entity_id(workspace_id, type_name: str, key) -> str:
    return str(uuid.uuid5(_NS, f"{workspace_id}:{type_name}:{key}"))


def _chosen(layer, everything: bool) -> list:
    return [t for t in layer.types if t.kind == "entity" and t.keys and (t.materialize or everything)]


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
        out: dict = {"nodes": {}, "edges": []}
        with Views(workspace_id, refs, lock=True, logical=list(logical.values())) as v:
            for t in types:
                if t.name not in logical or logical[t.name][0] not in v.names:
                    # Its view did not build: the other types still publish.
                    log.warning("semantic_layer.publish_skipped", type=t.name, reason="view did not build")
                    continue
                # Not prose: free text stays in the rows (and is what an extractor reads), not on the node.
                present = {f for b in t.bindings for f in b.fields}
                keep = [p.name for p in t.properties if p.role in ("key", "category", "text", "time", "reference")
                        and p.name in present]
                cols = ", ".join(q(c) for c in keep) + ", _assets"
                out["nodes"][t.name] = v.query(f"SELECT {cols} FROM {q(logical[t.name][0])} "
                                               f"LIMIT {MAX_PER_TYPE}").to_pylist()
            for lk in layer.links:
                if lk.source in names and lk.target in names and lk.source in logical and lk.target in logical                         and logical[lk.source][0] in v.names and logical[lk.target][0] in v.names:
                    s, d = q(logical[lk.source][0]), q(logical[lk.target][0])
                    sk = layer.type(lk.source).key
                    rows = v.query(f"SELECT DISTINCT CAST(s.{q(sk)} AS VARCHAR) AS a, CAST(d.{q(lk.target_prop)} AS VARCHAR) AS b "
                                   f"FROM {s} AS s JOIN {d} AS d ON CAST(s.{q(lk.source_prop)} AS VARCHAR) = "
                                   f"CAST(d.{q(lk.target_prop)} AS VARCHAR)").to_pylist()
                    out["edges"].append((lk, rows))
        return out
    data = await anyio.to_thread.run_sync(read)

    repo = NodeRepo()
    by_name = {b.asset_name: b.asset_id for t in types for b in t.bindings}
    n_nodes = n_edges = 0
    for t in types:
        rows = data["nodes"].get(t.name) or []
        for i in range(0, len(rows), BATCH):
            batch = rows[i:i + BATCH]
            specs, keys = [], []
            for r in batch:
                key = "|".join(str(r.get(k)) for k in t.keys)
                nid = entity_id(workspace_id, t.name, key)
                props = {k: (v if isinstance(v, (str, int, float, bool)) else str(v))
                         for k, v in r.items() if v is not None and k != "_assets"}
                props["__sources"] = r.get("_assets") or ""
                named = r.get(t.label) if t.label else None
                specs.append({"node_id": nid, "type": t.name, "label": str(named) if named not in (None, "") else key,
                              "properties": props})
                for name in str(r.get("_assets") or "").split(", "):
                    if name in by_name:
                        keys.append((by_name[name], key, nid))
            await repo.upsert_many(workspace_id, specs, date_link=False)
            # a published record is in no asset's subtree, so no asset's embedding job reaches it
            written = [x for x in await repo.get_many(workspace_id, [sp["node_id"] for sp in specs]) if x is not None]
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
        edges = [{"source_id": entity_id(workspace_id, lk.source, r["a"]),
                  "target_id": entity_id(workspace_id, lk.target, r["b"]), "type": lk.name,
                  "properties": {"origin": lk.origin, "via": f"{lk.source_prop}->{lk.target_prop}"}}
                 for r in rows if r["a"] is not None and r["b"] is not None]
        for i in range(0, len(edges), BATCH):
            await repo.add_edges(workspace_id=workspace_id, edges=edges[i:i + BATCH], _quarantine=False)
        n_edges += len(edges)
    removed = await _retire(workspace_id, {t.name for t in types})
    log.info("semantic_layer.published", types=len(types), nodes=n_nodes, edges=n_edges, removed=removed)
    return {"types": len(types), "nodes": n_nodes, "edges": n_edges, "removed": removed}


async def _retire(workspace_id, current: set) -> int:
    """Entities an earlier publish made of a type no longer published (renamed, rejected, unmarked):
    found through the identity map this module writes, and removed with their keys."""
    from sqlalchemy import delete, select

    from opencortyx.core.db import V2Session
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.data import SourceKey
    async with V2Session() as db:
        rows = (await db.execute(select(SourceKey.node_id, SourceKey.entity_type).where(
            SourceKey.workspace_id == workspace_id, SourceKey.match_method == "semantic_layer"))).all()
        stale = sorted({str(n) for n, t in rows if t not in current})
        if stale:
            await db.execute(delete(SourceKey).where(SourceKey.workspace_id == workspace_id,
                                                     SourceKey.match_method == "semantic_layer",
                                                     SourceKey.entity_type.notin_(sorted(current) or [""])))
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
