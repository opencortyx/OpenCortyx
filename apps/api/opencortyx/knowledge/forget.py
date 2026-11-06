"""Forgetting: an asset, or a subject (a person, a company - any record), from EVERY store, with proof.

The stores something can be in, and what forgetting does in each (`STORES`, the data map - a store
added to the platform without a step here is a store a deletion misses; tests/test_forget.py holds
the list):

  graph       the nodes and their edges (NodeRepo.delete_nodes; vectors go with them)
  vectors     search points, passages included (with the nodes; passages by asset)
  data        the asset's Delta rows or table, then an immediate VACUUM of the table, so deleted
              rows are not readable through time travel for the usual retention week
  evidence    the ledger rows pointing at the asset or at the forgotten nodes
  masters     a merged record the forgotten nodes were members of is rebuilt from the rest
              (resolution/master.py), so their values leave it too
  resolution  identity keys, decisions and labels about the forgotten nodes or naming the subject
  cache       cached extraction answers read from the asset or mentioning the subject
  uploads     the stored original file
  chats       messages that cite the asset or name the subject are redacted
  audit       audit rows about the forgotten nodes or naming the subject are redacted
  runs        the asset's ingest runs and change-detection manifest entries
  tombstone   a hash of the asset id / the subject's names, so nothing reads it back: an asset with
              a tombstoned id is refused when written again (core/substrate/repo.py), and a named
              record a new file creates for a tombstoned name is removed after the file is
              resolved (resolution/engine.resolve_asset)

Every request is a `deletion_job` row: each store's step says done / failed with its count, a failed
step can be retried, and the job is `incomplete` until every step is done. After the steps, `proof`
searches the stores again for what was forgotten; the job is `done` only when nothing is found.
What a person chose to KEEP (a document that also mentions the subject, which they did not delete)
is listed in the proof as kept, not as a failure.

External data (a customer's own database or lake) is never written to: the asset's reading of it is
forgotten and tombstoned, and the response says the source itself still holds it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from opencortyx.core.observability import get_logger

log = get_logger(__name__)

STORES = ("graph", "vectors", "data", "evidence", "masters", "resolution", "cache", "uploads", "chats",
          "audit", "runs", "indexes", "tombstone")
_REDACTED = "[removed]"
_TTL = 30.0


from opencortyx.core.dataplane.tombstones import (  # noqa: E402 - the primitives live in core
    Forgotten, add_tombstones, guard_assets, is_tombstoned, key_hash, lift_tombstone,
)
from opencortyx.core.dataplane import tombstones as _tomb  # noqa: E402


async def drop_tombstoned_names(workspace_id, nodes: list) -> list:
    """Named records (an extractor's entities) whose name was forgotten, among `nodes`: deleted.
    Called after an asset is resolved, so a new file naming a forgotten subject does not bring it
    back. Returns the ids deleted."""
    tombs = await _tomb._tombstones(workspace_id)
    if not any(k == "subject" for k, _ in tombs):
        return []
    gone = [n.id for n in nodes if (n.properties or {}).get("__identity") == "entity"
            and n.label and ("subject", key_hash(n.label)) in tombs]
    if gone:
        from opencortyx.core.substrate import NodeRepo
        await NodeRepo().delete_nodes(workspace_id, gone)
        log.info("forget.dropped_tombstoned_names", workspace_id=str(workspace_id), count=len(gone))
    return gone


async def restorable(workspace_id, nodes: list) -> tuple[list, int]:
    """A backup being restored, without what was forgotten since it was made: tombstoned assets,
    every node derived from one (`source_ref`, transitively within the backup), and named records
    with a forgotten name. Returns (nodes to restore, how many were left out)."""
    tombs = await _tomb._tombstones(workspace_id)
    if not tombs:
        return list(nodes), 0
    gone = {str(n.id) for n in nodes if ("asset", key_hash(uuid.UUID(str(n.id)))) in tombs}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            if str(n.id) not in gone and n.source_ref and str(n.source_ref) in gone:
                gone.add(str(n.id))
                changed = True
    for n in nodes:
        if (n.properties or {}).get("__identity") == "entity" and n.label and ("subject", key_hash(n.label)) in tombs:
            gone.add(str(n.id))
    keep = [n for n in nodes if str(n.id) not in gone]
    return keep, len(nodes) - len(keep)


# ── the job ───────────────────────────────────────────────────────────────────────────────────

class _Job:
    def __init__(self, row):
        self.row = row
        self.steps: dict = dict(row.steps or {})

    async def step(self, name: str, fn) -> None:
        """Run one store's step once (a retry skips the steps already done)."""
        if (self.steps.get(name) or {}).get("status") == "done":
            return
        try:
            n = await fn()
            self.steps[name] = {"status": "done", "count": int(n or 0)}
        except Exception as e:                                # noqa: BLE001 - recorded, retried later
            log.warning("forget.step_failed", step=name, error=repr(e)[:300])
            self.steps[name] = {"status": "failed", "error": str(e)[:300]}


async def _new_job(workspace_id, kind: str, target: dict, by: Optional[str]):
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import DeletionJob
    async with V2Session() as db:
        row = DeletionJob(workspace_id=uuid.UUID(str(workspace_id)), kind=kind, target=target, status="running",
                          created_by=by, attempts=1)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _finish(job: _Job, proof: dict) -> dict:
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import DeletionJob
    failed = [k for k, v in job.steps.items() if v.get("status") != "done"]
    found = {k: v for k, v in (proof.get("remaining") or {}).items() if v}
    status = "done" if not failed and not found else "incomplete"
    async with V2Session() as db:
        row = await db.get(DeletionJob, job.row.id)
        row.steps, row.proof, row.status = job.steps, proof, status
        if status == "done" and "_names" in (row.target or {}):
            # the job must not itself remember what it forgot
            row.target = {k: v for k, v in row.target.items() if k != "_names"}
        row.error = None if status == "done" else (
            ("failed: " + ", ".join(failed) + ". ") if failed else "") + (
            ("still found in: " + ", ".join(found)) if found else "")
        row.finished_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
        return job_view(row)


def job_view(row) -> dict:
    target = {k: v for k, v in (row.target or {}).items() if not str(k).startswith("_")}
    return {"id": str(row.id), "kind": row.kind, "target": target, "status": row.status,
            "steps": row.steps or {}, "proof": row.proof or {}, "attempts": row.attempts, "error": row.error,
            "created_by": row.created_by,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


# ── shared steps ──────────────────────────────────────────────────────────────────────────────

def _uuids(ids) -> list:
    return [uuid.UUID(str(i)) for i in dict.fromkeys(ids) if i]


async def _drop_resolution(workspace_id, node_ids: list, names: list) -> int:
    from sqlalchemy import String, cast, delete, or_
    from opencortyx.core.db import V2Session
    from opencortyx.domain.resolution import ResolutionDecision as D, ResolutionKey as K, ResolutionLabel as L
    ws, ids, n = uuid.UUID(str(workspace_id)), _uuids(node_ids), 0
    async with V2Session() as db:
        if ids:
            n += (await db.execute(delete(K).where(K.workspace_id == ws, K.node_id.in_(ids)))).rowcount or 0
            n += (await db.execute(delete(D).where(D.workspace_id == ws, or_(D.subject.in_(ids), D.target.in_(ids))))).rowcount or 0
        for name in names:
            like = f"%{name}%"
            n += (await db.execute(delete(L).where(L.workspace_id == ws, or_(
                cast(L.a, String).ilike(like), cast(L.b, String).ilike(like))))).rowcount or 0
            n += (await db.execute(delete(D).where(D.workspace_id == ws, cast(D.evidence, String).ilike(like)))).rowcount or 0
        await db.commit()
    return n


async def _drop_labels_of_asset(workspace_id, asset_id) -> int:
    from sqlalchemy import String, cast, delete, or_
    from opencortyx.core.db import V2Session
    from opencortyx.domain.resolution import ResolutionLabel as L
    like = f'%"asset_id": "{asset_id}"%'
    async with V2Session() as db:
        n = (await db.execute(delete(L).where(L.workspace_id == uuid.UUID(str(workspace_id)), or_(
            cast(L.a, String).like(like), cast(L.b, String).like(like))))).rowcount or 0
        await db.commit()
    return n


async def _redact_chats(workspace_id, *, asset_id=None, names: list = ()) -> int:
    """Redact messages that cite the asset (their citations name it) or that name the subject."""
    from sqlalchemy import String, cast, or_, select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.chat import ChatMessage as M, ChatThread as T
    ws = uuid.UUID(str(workspace_id))
    conds = []
    if asset_id:
        conds.append(cast(M.meta, String).like(f"%{asset_id}%"))
    for name in names:
        conds += [M.text.ilike(f"%{name}%"), cast(M.meta, String).ilike(f"%{name}%")]
    if not conds:
        return 0
    n = 0
    async with V2Session() as db:
        rows = (await db.execute(select(M).join(T, T.id == M.thread_id).where(
            T.workspace_id == ws, or_(*conds)))).scalars().all()
        for m in rows:
            text = m.text or ""
            for name in names:
                text = _replace_ci(text, name)
            if asset_id and str(asset_id) in str(m.meta or {}):
                text = _REDACTED if m.role == "assistant" else text
            m.text = text
            m.meta = {"redacted": True}
            n += 1
        for name in names:                                   # a chat titled with the subject's name
            for t in (await db.execute(select(T).where(T.workspace_id == ws, T.title.ilike(f"%{name}%")))).scalars().all():
                t.title = _replace_ci(t.title, name)
                n += 1
        await db.commit()
    return n


def _replace_ci(text: str, needle: str) -> str:
    import re
    return re.sub(re.escape(needle), _REDACTED, text or "", flags=re.I) if needle else text


async def _redact_audit(workspace_id, *, node_ids: list = (), names: list = ()) -> int:
    from sqlalchemy import String, cast, or_, update
    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import AuditLog as A
    ws, conds = uuid.UUID(str(workspace_id)), []
    ids = _uuids(node_ids)
    if ids:
        conds.append(A.node_id.in_(ids))
    for name in names:
        conds.append(cast(A.meta, String).ilike(f"%{name}%"))
    if not conds:
        return 0
    async with V2Session() as db:
        n = (await db.execute(update(A).where(A.workspace_id == ws, or_(*conds))
                              .values(meta={"redacted": True}, node_id=None))).rowcount or 0
        await db.commit()
    return n


async def _drop_cache(workspace_id, *, asset_id=None, names: list = ()) -> int:
    from sqlalchemy import delete, or_
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import LlmCache as C
    ws, conds = uuid.UUID(str(workspace_id)), []
    if asset_id:
        conds.append(C.asset_id == uuid.UUID(str(asset_id)))
    for name in names:
        conds.append(C.response.ilike(f"%{name}%"))
    if not conds:
        return 0
    async with V2Session() as db:
        n = (await db.execute(delete(C).where(C.workspace_id == ws, or_(*conds)))).rowcount or 0
        await db.commit()
    return n


async def _drop_evidence(workspace_id, node_ids: list) -> int:
    from sqlalchemy import delete
    from opencortyx.core.db import V2Session
    from opencortyx.domain.data import Evidence
    ids = _uuids(node_ids)
    if not ids:
        return 0
    async with V2Session() as db:
        n = (await db.execute(delete(Evidence).where(Evidence.workspace_id == uuid.UUID(str(workspace_id)),
                                                     Evidence.target_id.in_(ids)))).rowcount or 0
        await db.commit()
    return n


async def _rebuild(workspace_id, masters: list) -> int:
    if not masters:
        return 0
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.resolution import master
    out = await master.rebuild(NodeRepo(), workspace_id, masters)
    return out["rebuilt"] + out["removed"]


async def _drop_indexes(workspace_id, *, asset_id=None, node_ids: list = (), names: list = ()) -> int:
    """The browse indexes that copy labels: the graph tree's asset rows, and the (legacy) clusters'
    members and previews."""
    from sqlalchemy import delete, select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.graph_clusters import GraphCluster, GraphClusterMember
    ws, n = uuid.UUID(str(workspace_id)), 0
    if asset_id is not None:
        from opencortyx.core.security.scope import unscoped
        from opencortyx.knowledge.graph_tree import reindex_asset
        with unscoped():
            await reindex_asset(ws, uuid.UUID(str(asset_id)))
        n += 1
    ids = _uuids(node_ids)
    async with V2Session() as db:
        if ids:
            n += (await db.execute(delete(GraphClusterMember).where(
                GraphClusterMember.workspace_id == ws, GraphClusterMember.node_id.in_(ids)))).rowcount or 0
        if names:
            low = [x.lower() for x in names]
            for c in (await db.execute(select(GraphCluster).where(GraphCluster.workspace_id == ws))).scalars().all():
                keep = [p for p in (c.preview or []) if not any(x in str(p).lower() for x in low)]
                if keep != list(c.preview or []) or any(x in (c.label or "").lower() for x in low):
                    c.preview = keep
                    if any(x in (c.label or "").lower() for x in low):
                        c.label = _REDACTED
                    n += 1
        await db.commit()
    return n


async def _vacuum_documents(workspace_id) -> int:
    """VACUUM the shared document table now (a deleted document's rows live in it), so its old
    versions stop being readable at once rather than after the retention week."""
    import anyio
    from opencortyx.stores.data import DataStore, delta
    from opencortyx.stores.data.layout import UNSTRUCTURED
    path = DataStore(uuid.UUID(str(workspace_id))).path(UNSTRUCTURED)
    return await anyio.to_thread.run_sync(lambda: delta.vacuum(path, retention_hours=0))


# ── an asset ──────────────────────────────────────────────────────────────────────────────────

async def preview_asset(workspace_id, asset_id) -> Optional[dict]:
    from opencortyx.ingest.runs.asset_purge import preview_asset_delete
    out = await preview_asset_delete(uuid.UUID(str(workspace_id)), uuid.UUID(str(asset_id)))
    if out is None:
        return None
    try:
        from opencortyx.core.dataplane import assets
        row = await assets.get(workspace_id, asset_id)
        out["external"] = bool(row is not None and row.storage == "external")
    except Exception:                                         # noqa: BLE001
        out["external"] = False
    out["stores"] = list(STORES)
    return out


async def forget_asset(workspace_id, asset_id, *, by: Optional[str] = None, job_row=None,
                        tombstone: bool = True) -> Optional[dict]:
    """Remove one asset from every store. With `tombstone` it is also FORGOTTEN: never read back (a
    file deleted to upload a corrected one must not be tombstoned). None when it does not exist."""
    from opencortyx.core.security.scope import unscoped
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.ingest.runs.asset_purge import collect_asset_nodes, purge_asset_full
    from opencortyx.resolution.master import masters_of
    ws, aid = uuid.UUID(str(workspace_id)), uuid.UUID(str(asset_id))
    repo = NodeRepo()
    with unscoped():
        closure = await collect_asset_nodes(ws, aid)
    if not closure and job_row is None:
        return None
    if job_row is None:
        asset = closure.get(str(aid))
        job_row = await _new_job(ws, "asset", {"asset_id": str(aid), "label": getattr(asset, "label", None),
                                                "type": getattr(asset, "type", None), "tombstone": tombstone}, by)
    tombstone = bool((job_row.target or {}).get("tombstone", tombstone))
    job = _Job(job_row)
    ids = list(closure)
    state: dict = {}

    async def graph():
        from opencortyx.core import uploads
        from opencortyx.core.dataplane import assets
        state["masters"] = list(set((await masters_of(repo, ws, [uuid.UUID(i) for i in ids])).values()))
        state["digests"] = set()
        for n in closure.values():
            state["digests"] |= uploads.digests_in(n.properties or {})
        row = await assets.get(ws, aid)
        state["external"] = bool(row is not None and row.storage == "external")
        state["kind"] = row.kind if row is not None else None
        with unscoped():
            res = await purge_asset_full(ws, aid)
        state["purge"] = res or {}
        return (res or {}).get("nodes", 0)
    await job.step("graph", graph)
    job.steps.setdefault("vectors", {"status": "done", "count": 0, "note": "deleted with their nodes"})

    async def data():
        n = 0
        if state.get("kind") == "unstructured" or state.get("kind") is None:
            n = await _vacuum_documents(ws)
        return n
    await job.step("data", data)
    await job.step("evidence", lambda: _drop_evidence(ws, ids))
    await job.step("masters", lambda: _rebuild(ws, [m for m in state.get("masters", []) if str(m) not in closure]))
    await job.step("resolution", lambda: _drop_resolution(ws, ids, []))
    await job.step("resolution_labels", lambda: _drop_labels_of_asset(ws, aid))
    await job.step("cache", lambda: _drop_cache(ws, asset_id=aid))

    async def uploads_step():
        from opencortyx.core import uploads
        return uploads.remove(state.get("digests") or set())
    await job.step("uploads", uploads_step)
    await job.step("chats", lambda: _redact_chats(ws, asset_id=aid))
    await job.step("audit", lambda: _redact_audit(ws, node_ids=ids))
    job.steps.setdefault("runs", {"status": "done", "count": int((state.get("purge") or {}).get("runs") or 0),
                                  "note": "with the graph step"})
    await job.step("indexes", lambda: _drop_indexes(ws, asset_id=aid, node_ids=ids))
    if tombstone:
        await job.step("tombstone", lambda: add_tombstones(ws, "asset", [aid], job_id=job.row.id, by=by))
    else:
        job.steps.setdefault("tombstone", {"status": "done", "count": 0, "note": "deleted, not forgotten: it may be added again"})
    proof = await prove_asset(ws, aid, ids)
    if state.get("external"):
        proof["note"] = "The source system still holds this data: OpenCortyx never writes to it."
    return await _finish(job, proof)


async def prove_asset(workspace_id, asset_id, node_ids: list) -> dict:
    """Search the stores again for the asset: what is left (should be nothing)."""
    from sqlalchemy import String, cast, func, select
    from opencortyx.core.db import V2Session
    from opencortyx.core.security.scope import unscoped
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.data import DataAsset, Evidence
    from opencortyx.domain.governance import LlmCache
    from opencortyx.domain.chat import ChatMessage, ChatThread
    from opencortyx.stores.vector.models import VectorFilter
    ws, aid = uuid.UUID(str(workspace_id)), uuid.UUID(str(asset_id))
    remaining: dict = {}
    with unscoped():
        remaining["graph"] = len(await NodeRepo().get_many(ws, _uuids(node_ids)))
    try:
        repo = NodeRepo()
        remaining["vectors"] = (await repo.vectors.count(VectorFilter(workspace_id=ws, node_ids=_uuids(node_ids)))
                                if node_ids else 0) + await repo.vectors.count(VectorFilter(workspace_id=ws, source_ref=aid))
    except Exception as e:                                    # noqa: BLE001
        remaining["vectors_error"] = str(e)[:200]
    async with V2Session() as db:
        remaining["data"] = int((await db.execute(select(func.count()).select_from(DataAsset).where(
            DataAsset.workspace_id == ws, DataAsset.id == aid))).scalar() or 0)
        remaining["evidence"] = int((await db.execute(select(func.count()).select_from(Evidence).where(
            Evidence.workspace_id == ws, Evidence.asset_id == aid))).scalar() or 0)
        remaining["cache"] = int((await db.execute(select(func.count()).select_from(LlmCache).where(
            LlmCache.workspace_id == ws, LlmCache.asset_id == aid))).scalar() or 0)
        remaining["chats"] = int((await db.execute(select(func.count()).select_from(ChatMessage)
                                                   .join(ChatThread, ChatThread.id == ChatMessage.thread_id).where(
            ChatThread.workspace_id == ws, cast(ChatMessage.meta, String).like(f"%{aid}%")))).scalar() or 0)
    return {"remaining": remaining, "checked_at": datetime.now(timezone.utc).isoformat()}


# ── a subject ─────────────────────────────────────────────────────────────────────────────────

def _names(nodes: list) -> list[str]:
    """The names a subject goes by: its records' labels and the values identifying them."""
    out = []
    for n in nodes:
        for v in [n.label] + [v for k, v in (n.properties or {}).items()
                              if k in ("name", "email", "full_name", "username") and isinstance(v, str)]:
            v = (v or "").strip()
            if len(v) >= 3 and v.lower() not in {x.lower() for x in out}:
                out.append(v)
    return out[:20]


async def _cluster(workspace_id, node_id) -> list:
    """The subject's records: a merged record and its members, or a member with its cluster."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.resolution.master import masters_of, members_of
    repo = NodeRepo()
    node = await repo.get(workspace_id, node_id)
    if node is None:
        return []
    master = node.id if (node.properties or {}).get("__master") else \
        (await masters_of(repo, workspace_id, [node.id])).get(node.id)
    if master is None:
        return [node]
    members = (await members_of(repo, workspace_id, [master])).get(master) or []
    m = await repo.get(workspace_id, master)
    return [x for x in [m, *members] if x is not None]


async def _mentions(workspace_id, names: list, exclude: set) -> list[dict]:
    """Assets whose content mentions one of `names`: candidates to forget whole."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.core.substrate.provenance import assets_of
    repo = NodeRepo(include_passages=True)
    hits: dict = {}
    for name in names:
        try:
            found = await repo.keyword_search(workspace_id=workspace_id, query=name, k=50, scope="all")
        except Exception:                                     # noqa: BLE001 - no search: no candidates
            found = []
        texts = [(n, s) for n, s in found if name.lower() in str((n.properties or {}).get("text") or n.label or "").lower()
                 and str(n.id) not in exclude]
        if not texts:
            continue
        for a in await assets_of(workspace_id, [n.id for n, _ in texts]):
            hits.setdefault(str(a), set()).add(name)
    if not hits:
        return []
    assets = {str(n.id): n for n in await repo.get_many(workspace_id, _uuids(hits))}
    return [{"asset_id": a, "label": getattr(assets.get(a), "label", None), "type": getattr(assets.get(a), "type", None),
             "mentions": sorted(v)} for a, v in hits.items()]


async def preview_subject(workspace_id, node_id) -> Optional[dict]:
    from sqlalchemy import String, cast, func, or_, select
    from opencortyx.core.db import V2Session
    from opencortyx.domain.chat import ChatMessage, ChatThread
    from opencortyx.domain.resolution import ResolutionLabel as L
    cluster = await _cluster(workspace_id, node_id)
    if not cluster:
        return None
    names = _names([n for n in cluster if not (n.properties or {}).get("__master")] or cluster)
    ids = {str(n.id) for n in cluster}
    owned = await _owned_neighbors(workspace_id, [n.id for n in cluster])
    ws = uuid.UUID(str(workspace_id))
    async with V2Session() as db:
        chats = int((await db.execute(select(func.count()).select_from(ChatMessage).join(
            ChatThread, ChatThread.id == ChatMessage.thread_id).where(
            ChatThread.workspace_id == ws, or_(*[ChatMessage.text.ilike(f"%{n}%") for n in names])))).scalar() or 0) \
            if names else 0
        labels = int((await db.execute(select(func.count()).select_from(L).where(
            L.workspace_id == ws, or_(*[cast(L.a, String).ilike(f"%{n}%") for n in names],
                                      *[cast(L.b, String).ilike(f"%{n}%") for n in names])))).scalar() or 0) \
            if names else 0
    return {"node_id": str(node_id), "names": names,
            "records": [{"id": str(n.id), "label": n.label, "type": n.type,
                         "merged": bool((n.properties or {}).get("__master"))} for n in cluster],
            "linked_records_only_about_it": len(owned),
            "documents_mentioning": await _mentions(workspace_id, names, ids | {str(o) for o in owned}),
            "chat_messages": chats, "labels": labels, "stores": list(STORES)}


async def _owned_neighbors(workspace_id, ids: list) -> list:
    """Records linked to the subject that are about nothing else: a node whose every other link is
    bookkeeping and that is no asset. Deleted with the subject."""
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.ontology import is_asset_node
    repo = NodeRepo()
    mine = {str(i) for i in ids}
    hops = await repo.neighbors(workspace_id, list(ids), direction="both", limit=2000)
    # never another named thing (a person, a company) or a merged record: only what hangs off this one
    cand = {str(n.id): n for _, n in hops if str(n.id) not in mine and not is_asset_node(n)
            and n.type not in ("Date", "Workspace")
            and (n.properties or {}).get("__identity") != "entity" and not (n.properties or {}).get("__master")}
    out = []
    bookkeeping = {"CREATED_ON", "MODIFIED_ON", "FROM_ASSET", "MEMBER_OF"}
    for cid, n in cand.items():
        others = [e for e, far in await repo.neighbors(workspace_id, [n.id], direction="both", limit=50)
                  if str(far.id) not in mine and e.type not in bookkeeping]
        if not others and not (n.properties or {}).get("__master"):
            out.append(n.id)
    return out


async def forget_subject(workspace_id, node_id, *, also_assets: Iterable = (), by: Optional[str] = None,
                         job_row=None) -> Optional[dict]:
    """Forget a subject: its records (and a merged record of them), records only about it, what the
    platform remembers about it (labels, decisions, cache, chats, audit), the documents a person chose
    to delete with it, and a tombstone of its names."""
    from opencortyx.core.substrate import NodeRepo
    ws = uuid.UUID(str(workspace_id))
    if job_row is None:
        cluster = await _cluster(ws, node_id)
        if not cluster:
            return None
        names = _names([n for n in cluster if not (n.properties or {}).get("__master")] or cluster)
        owned = await _owned_neighbors(ws, [n.id for n in cluster])
        ids = [str(n.id) for n in cluster] + [str(o) for o in owned]
        job_row = await _new_job(ws, "subject", {"node_id": str(node_id), "label": cluster[0].label,
                                                  "node_ids": ids, "names_count": len(names),
                                                  "name_hashes": [key_hash(n) for n in names],
                                                  "also_assets": [str(a) for a in also_assets],
                                                  # kept only until the job is done, for a retry
                                                  "_names": names}, by)
        state = {"names": names}
    else:
        state = {"names": list(job_row.target.get("_names") or [])}
    job = _Job(job_row)
    target = job_row.target or {}
    ids = list(target.get("node_ids") or [])
    names = state["names"]
    repo = NodeRepo()

    async def graph():
        from opencortyx.core.security.scope import unscoped
        from opencortyx.resolution.master import masters_of
        with unscoped():
            outside = [m for m in set((await masters_of(repo, ws, _uuids(ids))).values()) if str(m) not in ids]
            state["masters"] = outside
            return await repo.delete_nodes(ws, _uuids(ids))
    await job.step("graph", graph)
    job.steps.setdefault("vectors", {"status": "done", "count": 0, "note": "deleted with their nodes"})
    await job.step("evidence", lambda: _drop_evidence(ws, ids))
    await job.step("masters", lambda: _rebuild(ws, state.get("masters") or []))
    await job.step("resolution", lambda: _drop_resolution(ws, ids, names))
    await job.step("cache", lambda: _drop_cache(ws, names=names))
    await job.step("chats", lambda: _redact_chats(ws, names=names))
    await job.step("audit", lambda: _redact_audit(ws, node_ids=ids, names=names))

    async def conversations():
        # captured conversation records (session capture) that name the subject
        n = 0
        for name in names:
            found = await repo.keyword_search(workspace_id=ws, query=name, k=200, scope="conversations")
            gone = [x.id for x, _ in found if name.lower() in str((x.properties or {}).get("text") or x.label or "").lower()]
            n += await repo.delete_nodes(ws, gone) if gone else 0
        return n
    await job.step("conversations", conversations)
    await job.step("indexes", lambda: _drop_indexes(ws, node_ids=ids, names=names))

    async def assets_step():
        n = 0
        for a in target.get("also_assets") or []:
            out = await forget_asset(ws, a, by=by)
            n += 1 if out else 0
        return n
    await job.step("documents", assets_step)
    job.steps.setdefault("data", {"status": "done", "count": 0, "note": "with the documents chosen"})
    job.steps.setdefault("uploads", {"status": "done", "count": 0, "note": "with the documents chosen"})
    job.steps.setdefault("runs", {"status": "done", "count": 0, "note": "with the documents chosen"})
    await job.step("tombstone", lambda: add_tombstones(ws, "subject", names, job_id=job.row.id, by=by))
    proof = await prove_subject(ws, ids, names, kept_assets=set())
    return await _finish(job, proof)


async def prove_subject(workspace_id, node_ids: list, names: list, *, kept_assets: set) -> dict:
    from sqlalchemy import String, cast, func, or_, select
    from opencortyx.core.db import V2Session
    from opencortyx.core.security.scope import unscoped
    from opencortyx.core.substrate import NodeRepo
    from opencortyx.domain.chat import ChatMessage, ChatThread
    from opencortyx.domain.control import AuditLog
    from opencortyx.domain.governance import LlmCache
    from opencortyx.domain.resolution import ResolutionLabel as L
    ws = uuid.UUID(str(workspace_id))
    remaining: dict = {}
    with unscoped():
        remaining["graph"] = len(await NodeRepo().get_many(ws, _uuids(node_ids)))
    if names:
        async with V2Session() as db:
            remaining["chats"] = int((await db.execute(select(func.count()).select_from(ChatMessage).join(
                ChatThread, ChatThread.id == ChatMessage.thread_id).where(
                ChatThread.workspace_id == ws, or_(*[ChatMessage.text.ilike(f"%{n}%") for n in names])))).scalar() or 0)
            remaining["audit"] = int((await db.execute(select(func.count()).select_from(AuditLog).where(
                AuditLog.workspace_id == ws, or_(*[cast(AuditLog.meta, String).ilike(f"%{n}%") for n in names])))).scalar() or 0)
            remaining["labels"] = int((await db.execute(select(func.count()).select_from(L).where(
                L.workspace_id == ws, or_(*[cast(L.a, String).ilike(f"%{n}%") for n in names],
                                          *[cast(L.b, String).ilike(f"%{n}%") for n in names])))).scalar() or 0)
            remaining["cache"] = int((await db.execute(select(func.count()).select_from(LlmCache).where(
                LlmCache.workspace_id == ws, or_(*[LlmCache.response.ilike(f"%{n}%") for n in names])))).scalar() or 0)
    kept = await _mentions(ws, names, set()) if names else []
    return {"remaining": remaining, "kept_documents_mentioning": kept,
            "checked_at": datetime.now(timezone.utc).isoformat()}


async def retry(workspace_id, job_id) -> Optional[dict]:
    """Run an incomplete job's failed steps again."""
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import DeletionJob
    async with V2Session() as db:
        row = await db.get(DeletionJob, uuid.UUID(str(job_id)))
        if row is None or row.workspace_id != uuid.UUID(str(workspace_id)):
            return None
        if row.status == "done":
            return job_view(row)
        row.attempts = (row.attempts or 0) + 1
        row.status = "running"
        await db.commit()
        await db.refresh(row)
    if row.kind == "asset":
        return await forget_asset(workspace_id, row.target.get("asset_id"), job_row=row)
    return await forget_subject(workspace_id, row.target.get("node_id"), job_row=row)


__all__ = ["Forgotten", "STORES", "add_tombstones", "drop_tombstoned_names", "forget_asset", "forget_subject",
           "guard_assets", "is_tombstoned", "job_view", "key_hash", "lift_tombstone", "preview_asset",
           "preview_subject", "prove_asset", "prove_subject", "retry"]
