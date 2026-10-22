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
from opencortyx.resolution.spec import load_specs

log = get_logger(__name__)
_CHUNK = 1000
#: Set on an asset whose resolution found embeddings still missing; the embedding job that finishes
#: them queues the resolution again when it sees it, and resolving clears it.
RESOLVE_WAITING = "__resolve_waiting"


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


async def resolve_asset(workspace_id, source_id, *, store=None) -> dict:
    workspace_id, source_id = uuid.UUID(str(workspace_id)), uuid.UUID(str(source_id))
    store = store or Store()
    out: dict = {"types": {}}
    specs = await store.specs(workspace_id)
    nodes_of_asset = await store.asset_nodes(workspace_id, source_id)
    if specs:
        by_type = defaultdict(list)
        for node in nodes_of_asset:
            if node.type in specs:
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
    out["statements"] = await store.statements(workspace_id, source_id)
    await store.mark_resolved(workspace_id, source_id, out["types"])
    log.info("resolution.asset", workspace_id=str(workspace_id), source_id=str(source_id), types=out["types"])
    return out
