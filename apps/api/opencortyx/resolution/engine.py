"""`resolve_asset`: the last step of an asset's ingestion, after its extractors and embeddings.

  1. records     for each node type with a resolution spec, the asset's nodes are matched by identity
                 against the organization's graph (`records.decide`), then linked, extended and
                 recorded in bulk. Any node type: what is resolved, and how, is only its spec.
  2. statements  the asset's statements go through the contradiction engine, unchanged

Every read and write is per node type or per asset, never per node: one identity-index query, one
node read, one edge write, one property write and one ledger insert for each type.

`Store` is the only part that touches storage, so the engine runs in tests against a fake one.
"""
from __future__ import annotations

import uuid
from collections import defaultdict

from opencortyx.core.observability import get_logger
from opencortyx.resolution import records
from opencortyx.resolution.normalize import identity_key
from opencortyx.resolution.spec import load_specs

log = get_logger(__name__)
_CHUNK = 1000
#: Set on an asset whose resolution found embeddings still missing; the embedding job that finishes
#: them queues the resolution again when it sees it, and resolving clears it.
RESOLVE_WAITING = "__resolve_waiting"
#: The identity index's namespaces for links: the values a reference may name ("val:Customer.id",
#: "val:Source.identifiers"), and the values nodes refer to under each relation
#: ("ref:Customer.id:PLACED_BY", "ref:Source.identifiers:OBSOLETES").
VAL, REF = "val:", "ref:"
#: A document's identifiers are what its header references name.
DOCUMENT_TARGET = ("Source", "identifiers")
REFERENCES = "__references"


class Store:
    """Production storage: the graph through NodeRepo, the index and the ledger in PostgreSQL."""

    def __init__(self):
        from opencortyx.core.substrate import NodeRepo
        self.repo = NodeRepo()

    async def specs(self, workspace_id) -> dict:
        from opencortyx.core.db import V2Session
        async with V2Session() as db:
            return await load_specs(db, workspace_id)

    async def asset_nodes(self, workspace_id, source_id) -> list:
        return await self.repo.descendants(workspace_id, [source_id])

    async def lookup(self, workspace_id, node_type: str, keys, exclude) -> dict:
        """{key: the node that first held it}, among nodes outside `exclude`."""
        from sqlalchemy import select
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionKey as K
        keys, out = list(keys), {}
        async with V2Session() as db:
            for i in range(0, len(keys), _CHUNK):
                rows = (await db.execute(select(K.key_hash, K.node_id).where(
                    K.workspace_id == workspace_id, K.node_type == node_type, K.key_hash.in_(keys[i:i + _CHUNK]),
                    K.node_id.notin_(list(exclude))).order_by(K.created_at, K.node_id))).all()
                for key, node_id in rows:
                    out.setdefault(key, node_id)
        return out

    async def candidates(self, workspace_id, node_type: str, blocks, exclude, per_block: int = 50) -> dict:
        """{block: [node ids]} of nodes outside `exclude` sharing each block (the "similar" scorer's
        candidates), at most `per_block` each so one crowded block cannot blow up the comparison."""
        from sqlalchemy import select
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionKey as K
        blocks, out = list(blocks), {}
        async with V2Session() as db:
            for i in range(0, len(blocks), _CHUNK):
                rows = (await db.execute(select(K.key_hash, K.node_id).where(
                    K.workspace_id == workspace_id, K.node_type == node_type, K.key_hash.in_(blocks[i:i + _CHUNK]),
                    K.node_id.notin_(list(exclude))).order_by(K.created_at, K.node_id))).all()
                for block, node_id in rows:
                    ids = out.setdefault(block, [])
                    if len(ids) < per_block:
                        ids.append(node_id)
        return out

    async def nodes(self, workspace_id, ids) -> dict:
        return {n.id: n for n in await self.repo.get_many(workspace_id, list(ids))} if ids else {}

    async def forget(self, workspace_id, node_type: str, node_ids) -> None:
        """Drop index keys held by nodes that no longer exist."""
        from sqlalchemy import delete
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionKey as K
        async with V2Session() as db:
            await db.execute(delete(K).where(K.workspace_id == workspace_id, K.node_type == node_type,
                                             K.node_id.in_(list(node_ids))))
            await db.commit()

    async def apply(self, workspace_id, node_type: str, plan: records.Plan, known: dict) -> None:
        from sqlalchemy.dialects.postgresql import insert
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionDecision, ResolutionKey
        if plan.same:
            await self.repo.add_edges(workspace_id=workspace_id, edges=[
                {"source_id": a, "target_id": b, "type": "SAME_AS", "properties": {"__by": "resolution"}}
                for a, b in plan.same])
        if plan.extend:
            await self.repo.upsert_many(workspace_id, [
                {"node_id": cid, "type": known[cid].type, "properties": attrs,
                 "confidence": known[cid].confidence, "level": known[cid].level}
                for cid, attrs in plan.extend.items() if cid in known], date_link=False)
        async with V2Session() as db:
            key_rows = [{"workspace_id": workspace_id, "node_type": node_type, "key_hash": k, "node_id": n} for k, n in plan.keys]
            for i in range(0, len(key_rows), _CHUNK):
                await db.execute(insert(ResolutionKey).values(key_rows[i:i + _CHUNK]).on_conflict_do_nothing())
            for i in range(0, len(plan.decisions), _CHUNK):
                await db.execute(insert(ResolutionDecision).values(plan.decisions[i:i + _CHUNK])
                                 .on_conflict_do_nothing())
            await db.commit()

    async def document(self, workspace_id, source_id):
        return await self.repo.get(workspace_id, source_id)

    async def node_ids(self, workspace_id, type_name: str) -> list:
        from opencortyx.stores.graph import NodeQuery
        return await self.repo.graph.find_node_ids(workspace_id, NodeQuery(exact_types=[type_name]))

    async def index(self, workspace_id, rows: list) -> None:
        """Add (namespace, key, node id) rows to the identity index; existing rows are kept."""
        from sqlalchemy.dialects.postgresql import insert
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionKey
        if not rows:
            return
        async with V2Session() as db:
            values = [{"workspace_id": workspace_id, "node_type": t, "key_hash": k, "node_id": n} for t, k, n in rows]
            for i in range(0, len(values), _CHUNK):
                await db.execute(insert(ResolutionKey).values(values[i:i + _CHUNK]).on_conflict_do_nothing())
            await db.commit()

    async def referrers(self, workspace_id, prefix: str, keys, exclude) -> list:
        """(relation, node id, key) of nodes whose references under `prefix` name one of `keys`."""
        from sqlalchemy import select
        from opencortyx.core.db import V2Session
        from opencortyx.domain.resolution import ResolutionKey as K
        keys, out = list(keys), []
        async with V2Session() as db:
            for i in range(0, len(keys), _CHUNK):
                out += [(t[len(prefix):], n, k) for t, n, k in (await db.execute(select(K.node_type, K.node_id, K.key_hash).where(
                    K.workspace_id == workspace_id, K.node_type.like(prefix + "%"),
                    K.key_hash.in_(keys[i:i + _CHUNK]), K.node_id.notin_(list(exclude))))).all()]
        return out

    async def link(self, workspace_id, links: list) -> int:
        """Add the (from, relation, to) edges that are not there yet. A mapping's join edge in the same
        file was made while it was read; it is not made twice. Returns how many were added."""
        if not links:
            return 0
        from opencortyx.stores.graph import EdgeQuery
        have = {(str(e.source_id), e.type, str(e.target_id)) for e in await self.repo.edges(
            workspace_id, EdgeQuery(source_ids=list({a for a, _, _ in links}), types=list({r for _, r, _ in links})))}
        new = [(a, r, b) for a, r, b in links if (str(a), r, str(b)) not in have]
        if new:
            await self.repo.add_edges(workspace_id=workspace_id, edges=[
                {"source_id": a, "target_id": b, "type": r, "properties": {"__by": "resolution"}} for a, r, b in new])
        return len(new)

    async def pending_embeddings(self, workspace_id, source_id) -> bool:
        """Whether any of the asset's nodes still wait for their embeddings (structural nodes, which are
        never embedded, do not count)."""
        root = await self.repo.get(workspace_id, source_id)
        nodes = [n for n in ([root] if root else []) + await self.repo.descendants(workspace_id, [source_id])
                 if str((n.properties or {}).get("__structural") or "") != "true"]
        return bool(nodes) and bool(await self.repo.unembedded(workspace_id, nodes))

    async def wait_for_embeddings(self, workspace_id, source_id) -> bool:
        """Whether the asset must wait for its embeddings; if so it is marked waiting, so the
        embedding job that finishes them queues its resolution."""
        if not await self.pending_embeddings(workspace_id, source_id):
            return False
        await self.repo.patch_properties(workspace_id, [source_id], {RESOLVE_WAITING: "true"},
                                         bump_version=False)
        return True

    async def waiting(self, workspace_id, source_id) -> bool:
        root = await self.repo.get(workspace_id, source_id)
        return bool(root and (root.properties or {}).get(RESOLVE_WAITING))

    async def mark_resolved(self, workspace_id, source_id, summary: dict) -> None:
        """Stamp the asset, so the UI and the operator can see when and how it was resolved."""
        from datetime import datetime, timezone
        await self.repo.patch_properties(workspace_id, [source_id], {
            "__resolved_at": datetime.now(timezone.utc).isoformat(), "__resolution": summary},
            remove=[RESOLVE_WAITING], bump_version=False)

    async def statements(self, workspace_id, source_id) -> dict:
        from opencortyx.capabilities.modules import contradiction as c
        if not c.ContradictionDetection().enabled(workspace_id):
            return {"skipped": "disabled"}
        cfg = c.Settings.load()
        ids = await c._statements_of_source(workspace_id, source_id, cfg)
        return await c.resolve_many(workspace_id, ids, cfg=cfg)


def _values(node, attribute: str) -> list:
    """The values of one attribute of a node, as identity keys: a list gives one each."""
    raw = node.label if attribute == "label" else (node.properties or {}).get(attribute)
    items = raw if isinstance(raw, list) else [raw]
    return [k for k in (identity_key([v]) for v in items if isinstance(v, (str, int, float)) and str(v).strip()) if k]


def _types(node) -> set:
    from opencortyx.domain.ontology import base_type_of
    return {node.type, base_type_of(node)}


async def link_references(workspace_id, source_id, specs: dict, nodes: list, store) -> int:
    """Link an asset's nodes to the nodes their references name, and the nodes that name them to them.

    References are the specs' declared foreign keys (`Reference`) and a document's header references
    (`REFERENCES`, unless its spec turns them off). The values a reference may name go into the
    identity index under VAL, what nodes refer to under REF plus the relation; each reference is then
    looked up among values (links out) and each value among references (links in, from assets resolved
    earlier). Keys are written before they are looked up, so two assets naming each other link
    whichever is resolved first. Returns the number of links made."""
    rules = [(t, r) for t, spec in specs.items() for r in spec.references if spec.enabled]
    targets = {(r.target_type, r.target_attribute) for _, r in rules} | {DOCUMENT_TARGET}
    header_refs = (specs.get(DOCUMENT_TARGET[0]).header_references if specs.get(DOCUMENT_TARGET[0]) else True)
    root = await store.document(workspace_id, source_id) if source_id else None
    nodes = ([root] if root else []) + [n for n in nodes if not root or n.id != root.id]
    mine = {n.id for n in nodes}
    values: dict = {}                       # VAL namespace -> {key: node id}
    refs: list = []                         # (REF namespace, relation, key, node id)
    for node in nodes:
        types = _types(node)
        for t, a in targets:
            if t in types:
                for k in _values(node, a):
                    values.setdefault(f"{VAL}{t}.{a}", {})[k] = node.id
        for t, r in rules:
            if t in types:
                # A reversed rule is drawn from the named node into this one; "~" keeps that with the key.
                rel = ("~" if r.reverse else "") + r.relation
                refs += [(f"{REF}{r.target_type}.{r.target_attribute}:", rel, k, node.id)
                         for k in _values(node, r.attribute)]
        if header_refs and DOCUMENT_TARGET[0] in types:
            for item in (node.properties or {}).get(REFERENCES) or []:
                if isinstance(item, (list, tuple)) and len(item) == 2 and identity_key([item[1]]):
                    refs.append((f"{REF}{DOCUMENT_TARGET[0]}.{DOCUMENT_TARGET[1]}:", str(item[0]),
                                 identity_key([item[1]]), node.id))
    if not values and not refs:
        return 0
    await store.index(workspace_id, [(ns, k, n) for ns, keys in values.items() for k, n in keys.items()]
                      + [(prefix + rel, k, n) for prefix, rel, k, n in refs])
    links = []
    for prefix in {p for p, _, _, _ in refs}:
        wanted = [(rel, k, n) for p, rel, k, n in refs if p == prefix]
        found = await store.lookup(workspace_id, VAL + prefix[len(REF):-1], {k for _, k, _ in wanted}, exclude=set())
        links += [_edge(n, rel, found[k]) for rel, k, n in wanted if k in found and found[k] != n]
    for ns, keys in values.items():
        prefix = REF + ns[len(VAL):] + ":"
        by_key = dict(keys)
        for rel, referrer, k in await store.referrers(workspace_id, prefix, list(by_key), mine):
            links.append(_edge(referrer, rel, by_key[k]))
    return await store.link(workspace_id, list(dict.fromkeys(links)))


def _edge(referrer, relation: str, named) -> tuple:
    """(from, type, to) of a link: from the node holding the reference, unless the rule is reversed."""
    return (named, relation[1:], referrer) if relation.startswith("~") else (referrer, relation, named)


async def link_existing(workspace_id, type_names, store, page: int = 500) -> int:
    """Apply the reference rules to nodes already in the graph: those of `type_names` and of the
    types their rules name, a page at a time. Run when a mapping declares new foreign keys, so data
    read before the rule existed is linked too. Returns the number of links made."""
    workspace_id = uuid.UUID(str(workspace_id))
    specs = await store.specs(workspace_id)
    types = set(type_names)
    for name in list(types):
        spec = specs.get(name)
        types |= {r.target_type for r in (spec.references if spec else ())}
    made = 0
    for name in sorted(types):
        ids = await store.node_ids(workspace_id, name)
        for i in range(0, len(ids), page):
            nodes = list((await store.nodes(workspace_id, ids[i:i + page])).values())
            made += await link_references(workspace_id, None, specs, nodes, store)
    return made


async def resolve_asset(workspace_id, source_id, *, store=None) -> dict:
    workspace_id, source_id = uuid.UUID(str(workspace_id)), uuid.UUID(str(source_id))
    store = store or Store()
    out: dict = {"types": {}}
    specs = await store.specs(workspace_id)
    nodes_of_asset = await store.asset_nodes(workspace_id, source_id)
    if specs:
        by_type = defaultdict(list)
        for node in nodes_of_asset:
            if node.type in specs and specs[node.type].active:      # identity fields: matched as records
                by_type[node.type].append(node)
        for node_type, nodes in by_type.items():
            spec = specs[node_type]
            mine = {n.id for n in nodes}
            keys = {k for k in (records.key_of(n, spec) for n in nodes) if k}
            existing = await store.lookup(workspace_id, node_type, keys, exclude=mine) if keys else {}
            pool = {}
            if spec.scorer == "similar":
                blocks = {b for b in (records.block_of(n, spec) for n in nodes) if b}
                pool = await store.candidates(workspace_id, node_type, blocks, exclude=mine) if blocks else {}
            held = set(existing.values()) | {i for ids in pool.values() for i in ids}
            known = await store.nodes(workspace_id, held)
            # The index outlives the graph: an asset deleted, or the workspace reset, leaves keys of
            # nodes that are gone. Matching one would link SAME_AS to nothing, so it is dropped here
            # and from the index, and this node becomes the identity's canonical one instead.
            if gone := held - set(known):
                existing = {k: v for k, v in existing.items() if v not in gone}
                pool = {b: [i for i in ids if i not in gone] for b, ids in pool.items()}
                await store.forget(workspace_id, node_type, gone)
            plan = records.decide(workspace_id, source_id, node_type, spec, nodes, existing, known, pool)
            await store.apply(workspace_id, node_type, plan, {**known, **{n.id: n for n in nodes}})
            out["types"][node_type] = plan.summary()
    out["links"] = await link_references(workspace_id, source_id, specs, nodes_of_asset, store)
    out["statements"] = await store.statements(workspace_id, source_id)
    await store.mark_resolved(workspace_id, source_id, out["types"])
    log.info("resolution.asset", workspace_id=str(workspace_id), source_id=str(source_id), types=out["types"])
    return out
