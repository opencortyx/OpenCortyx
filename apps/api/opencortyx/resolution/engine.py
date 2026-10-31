"""Reconcile: whether what a writer just wrote is something the workspace already knows.

`reconcile(nodes)` is the one place that decides it, for any writer (an asset's ingestion, a
published record, an extractor's output) and any node type:
  1. rule        each type's spec says when two of its nodes are the same (`ResolutionSpec`): equal
                 identity fields, or a similar name. A named thing (`__identity: entity`) without a
                 spec of its own is matched by its name (`named_spec`). A type with neither is
                 never matched: every node of it stands alone.
  2. decide      `records.decide`: the same (score >= auto), a person decides (>= review), or new
  3. cluster     the same ones become members of one master node (`join`): the master carries the
                 type and name, each member links to it MEMBER_OF and stays as it was, so splitting
                 one off is removing one edge
`resolve_asset` is the last step of an asset's ingestion, after its extractors and embeddings:
  1. records     reconcile the asset's nodes
  2. references  link the asset's nodes to the nodes their foreign keys name
  3. statements  the asset's statements go through the contradiction engine, unchanged

Every read and write is per node type or per asset, never per node: one identity-index query, one
node read, one edge write, one property write and one ledger insert for each type.

`Store` is the only part that touches storage, so the engine runs in tests against a fake one.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Optional

from opencortyx.core.observability import get_logger
from opencortyx.resolution import records
from opencortyx.resolution.normalize import identity_key
from opencortyx.resolution.spec import ResolutionSpec, load_specs

log = get_logger(__name__)
_CHUNK = 1000
#: Set on an asset whose resolution found embeddings still missing; the embedding job that finishes
#: them queues the resolution again when it sees it, and resolving clears it.
RESOLVE_WAITING = "__resolve_waiting"
#: A cluster: each member links to its master.
MEMBER = "MEMBER_OF"
_MASTER_NS = uuid.uuid5(uuid.NAMESPACE_URL, "opencortyx/resolution/master")

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
        masters = await join(self.repo, workspace_id, plan.same, known) if plan.same else {}
        if plan.extend:
            # what a member adds is the cluster's: written on its master (the members stay as they were)
            await self.repo.upsert_many(workspace_id, [
                {"node_id": masters.get(cid, cid), "type": known[cid].type, "properties": attrs,
                 "confidence": known[cid].confidence, "level": known[cid].level}
                for cid, attrs in plan.extend.items() if cid in known], date_link=False)
        if plan.same:
            await self.share_evidence(workspace_id, [(x, masters[c]) for a, c in plan.same
                                                     for x in (a, c) if c in masters])
        async with V2Session() as db:
            key_rows = [{"workspace_id": workspace_id, "node_type": node_type, "key_hash": k, "node_id": n} for k, n in plan.keys]
            for i in range(0, len(key_rows), _CHUNK):
                await db.execute(insert(ResolutionKey).values(key_rows[i:i + _CHUNK]).on_conflict_do_nothing())
            for i in range(0, len(plan.decisions), _CHUNK):
                await db.execute(insert(ResolutionDecision).values(plan.decisions[i:i + _CHUNK])
                                 .on_conflict_do_nothing())
            await db.commit()

    async def share_evidence(self, workspace_id, pairs) -> None:
        """A node found to be the same as a canonical one gives the canonical its evidence too, so the
        canonical cites every source that states it - both values, when they disagree - and which one
        wins stays the decision ledger's call, not whichever wrote last."""
        from opencortyx.core.dataplane import evidence
        try:
            found = await evidence.for_targets(workspace_id, [a for a, _ in pairs])
            items = [(b, found.get(str(a)) or []) for a, b in pairs]
            await evidence.record(workspace_id, [(t, ps) for t, ps in items if ps])
        except Exception as e:                                # noqa: BLE001 - never fails a resolution
            log.warning("resolution.evidence_share_failed", error=str(e)[:200])

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
        """Whether any of what the asset embeds still waits for its vectors - the same nodes and
        passages the embedding job embeds (`tasks.graph.embeddable`)."""
        from opencortyx.tasks.graph import embeddable
        nodes = await embeddable(self.repo, workspace_id, source_id)
        return bool(nodes) and bool(await self.repo.unembedded(workspace_id, nodes))

    async def wait_for_embeddings(self, workspace_id, source_id) -> bool:
        """Whether the asset must wait for its embeddings; if so it is marked waiting, so the
        embedding job that finishes them queues its resolution.

        Checked again after the mark: an embedding job that finished between the first check and the
        mark read no mark, queued nothing, and left the asset to the 30-minute backstop - and its
        ingest run unfinished that long."""
        if not await self.pending_embeddings(workspace_id, source_id):
            return False
        await self.repo.patch_properties(workspace_id, [source_id], {RESOLVE_WAITING: "true"},
                                         bump_version=False)
        if await self.pending_embeddings(workspace_id, source_id):
            return True
        await self.repo.patch_properties(workspace_id, [source_id], {}, remove=[RESOLVE_WAITING],
                                         bump_version=False)
        return False

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


def master_id(workspace_id, canonical) -> uuid.UUID:
    """A cluster's master: one per canonical member, the same id however often it is joined."""
    return uuid.uuid5(_MASTER_NS, f"{workspace_id}:{canonical}")


async def join(repo, workspace_id, pairs, known: Optional[dict] = None, *, by: str = "resolution") -> dict:
    """Put each (member, canonical) pair in the canonical's cluster: its master node - written the
    first time with the canonical's type, name and fields - and a MEMBER_OF edge from both to it.
    Never destructive: the members stay as they were. Returns {canonical: master id}."""
    known = dict(known or {})
    masters: dict = {}
    specs = []
    for _, c in pairs:
        if c in masters:
            continue
        masters[c] = master_id(workspace_id, c)
        base = known.get(c) or await repo.get(workspace_id, c)
        if base is None:
            continue
        props = {k: v for k, v in (base.properties or {}).items() if not str(k).startswith("__")}
        props.update({"__master": True, "__identity": (base.properties or {}).get("__identity") or "record"})
        specs.append({"node_id": masters[c], "type": base.type, "label": base.label, "properties": props,
                      "confidence": base.confidence, "level": base.level})
    if specs:
        await repo.upsert_many(workspace_id, specs, date_link=False)
    edges = {(x, masters[c]): {"source_id": x, "target_id": masters[c], "type": MEMBER, "properties": {"__by": by}}
             for a, c in pairs for x in (a, c) if c in masters}
    if edges:
        await repo.add_edges(workspace_id=workspace_id, edges=list(edges.values()))
    from opencortyx.tasks.graph import embed_later            # a master is in no asset's subtree
    embed_later(workspace_id, list(masters.values()))
    return masters


def named(node) -> bool:
    """A thing whose name is its identity (an entity read from text or named by an extractor)."""
    return (getattr(node, "properties", None) or {}).get("__identity") == "entity"


def named_spec() -> ResolutionSpec:
    """The rule for a named thing whose type has none of its own: the same when the names match -
    equal after normalising, or near (`normalize.similarity`) at the `name_match_threshold` setting;
    a little below it a person decides."""
    from opencortyx.core.config import runtime as rc
    bar = rc.name_match_threshold()
    return ResolutionSpec(identify_by=("label",), scorer="similar", auto=bar, review=max(0.5, round(bar - 0.1, 2)))


def spec_for(specs: dict, node) -> Optional[ResolutionSpec]:
    spec = specs.get(node.type)
    if spec is not None and spec.active:
        return spec
    return named_spec() if named(node) else None


async def reconcile(workspace_id, nodes, *, store=None, source_id=None, specs: Optional[dict] = None) -> dict:
    """Whether each of `nodes` - just written, by any writer - is something already known, per its
    type's rule; the same ones joined into clusters. Masters are never matched themselves.
    Returns per-type summaries."""
    workspace_id = uuid.UUID(str(workspace_id))
    store = store or Store()
    specs = specs if specs is not None else await store.specs(workspace_id)
    by_type: dict = defaultdict(list)
    rule: dict = {}
    for node in nodes or []:
        if node is None or (node.properties or {}).get("__master"):
            continue
        spec = spec_for(specs, node)
        if spec is None:
            continue
        by_type[node.type].append(node)
        rule[node.type] = spec
    out: dict = {}
    for node_type, nodes_ in by_type.items():
        spec = rule[node_type]
        mine = {n.id for n in nodes_}
        keys = {k for k in (records.key_of(n, spec) for n in nodes_) if k}
        existing = await store.lookup(workspace_id, node_type, keys, exclude=mine) if keys else {}
        pool = {}
        if spec.scorer == "similar":
            blocks = {b for b in (records.block_of(n, spec) for n in nodes_) if b}
            pool = await store.candidates(workspace_id, node_type, blocks, exclude=mine) if blocks else {}
        held = set(existing.values()) | {i for ids in pool.values() for i in ids}
        known = await store.nodes(workspace_id, held)
        # The index outlives the graph: an asset deleted, or the workspace reset, leaves keys of
        # nodes that are gone. Matching one would join a cluster of nothing, so it is dropped here
        # and from the index, and this node becomes the identity's canonical one instead.
        if gone := held - set(known):
            existing = {k: v for k, v in existing.items() if v not in gone}
            pool = {b: [i for i in ids if i not in gone] for b, ids in pool.items()}
            await store.forget(workspace_id, node_type, gone)
        plan = records.decide(workspace_id, source_id, node_type, spec, nodes_, existing, known, pool)
        await store.apply(workspace_id, node_type, plan, {**known, **{n.id: n for n in nodes_}})
        out[node_type] = plan.summary()
    return out


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
    out["types"] = await reconcile(workspace_id, nodes_of_asset, store=store, source_id=source_id, specs=specs)
    out["links"] = await link_references(workspace_id, source_id, specs, nodes_of_asset, store)
    out["statements"] = await store.statements(workspace_id, source_id)
    await store.mark_resolved(workspace_id, source_id, out["types"])
    log.info("resolution.asset", workspace_id=str(workspace_id), source_id=str(source_id), types=out["types"])
    return out
