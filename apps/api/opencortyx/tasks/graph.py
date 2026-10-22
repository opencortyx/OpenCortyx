"""Graph upkeep tasks: embedding nodes and sources. (The browse tree's index is `tasks/graph_tree.py`.)"""
import uuid
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core.observability import get_logger
from opencortyx.core.inference.embedding import embed_batch
from opencortyx.core.substrate import NodeRepo

log = get_logger(__name__)


def result_source_ids(res) -> list[str]:
    """The sources an ingest result says it wrote: its own `source_ids`/`source_id`, else those of
    the per-file results it nests (a folder or archive reports one result per file)."""
    if not isinstance(res, dict):
        return []

    def own(d: dict) -> list:
        out = [str(x) for x in (d.get("source_ids") or []) if x]
        if not out and d.get("source_id"):
            out = [str(d["source_id"])]
        return out

    sids = own(res)
    if not sids:
        for v in res.values():
            if isinstance(v, dict):
                sids.extend(sid for sid in own(v) if sid not in sids)
    return sids


def _queue_embed_pass(workspace_id: str, res) -> None:
    """Queue the deferred embedding pass for whatever source this ingest just wrote.

    Called from every ingestion task rather than only the document one: a table at one node per row
    and a walked JSON payload pay the same ~110 ms per-node embed cost, so all three paths defer it
    and hand the work to the embedding queue. No-op when embedding ran inline or the result carries
    no source id.
    """
    try:
        from opencortyx.core.config import runtime as _rc
        if not _rc.defer_embedding():
            return
        sids = result_source_ids(res)
        if not sids:
            return
        jobs = [v2_embed_source.delay(str(workspace_id), sid) for sid in sids]
        res["embedding"] = "deferred"
        res["embedding_task_id"] = jobs[0].id if len(jobs) == 1 else [j.id for j in jobs]
    except Exception as e:                                    # noqa: BLE001 — never fail an ingest
        log.warning("embedding.enqueue_failed", error=str(e))         # that already succeeded


@celery_app.task(bind=True, queue="embedding", name="opencortyx.embed_source",
                 acks_late=True, max_retries=3)
def v2_embed_source(self, workspace_id: str, source_id: str):
    """Embed every un-vectored node belonging to one source — its whole SUBTREE, at any depth.

    ONE task per source, not per chunk: a 400-chunk document would otherwise enqueue 400 tasks whose
    broker overhead rivals the work. Batching also lets the embedder use its own batch API where the
    provider has one.

    Batches follow the `embed_batch_size` setting, and each batch is written before the next is
    embedded, so memory stays flat and an interrupted task resumes without redoing finished batches.

    The whole subtree, at any depth: a walked JSON payload is nested, so only its root record points
    at the Source and every other record points at its walk parent.

    Idempotent: only nodes without current embeddings are embedded.
    """
    async def _run():
        import gc
        from opencortyx.core.config import runtime as _rc
        from opencortyx.core.inference.embedding import active_embedding_spec, embed_batch

        repo = NodeRepo()
        oid, sid = uuid.UUID(workspace_id), uuid.UUID(source_id)
        root = await repo.get(oid, sid)
        subtree = ([root] if root else []) + await repo.descendants(oid, [sid])
        subtree = [n for n in subtree if str((n.properties or {}).get("__structural") or "") != "true"]
        pending = await repo.unembedded(oid, subtree)
        if pending:
            await repo.index_embeddings(oid, pending, embed_batch, batch_size=_rc.embed_batch_size())
            spec = await active_embedding_spec()
            if str((spec or {}).get("provider") or "").lower() in ("fastembed", "onnx"):
                gc.collect()
        # Read again, not from before embedding: the asset's resolution may have started waiting
        # for these vectors in the meantime.
        from opencortyx.resolution.engine import RESOLVE_WAITING
        root = await repo.get(oid, sid)
        if root and (root.properties or {}).get(RESOLVE_WAITING):
            from opencortyx.tasks.resolution import v2_resolve_asset
            v2_resolve_asset.delay(workspace_id, source_id)
        return {"status": "ok", "source_id": source_id, "embedded": len(pending)}

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=3)


@celery_app.task(bind=True, queue="embedding",
                 name="opencortyx.index_node_attrs", max_retries=3)
def v2_index_node_attrs(self, workspace_id: str, since_iso: str = None, limit: int = 20000,
                        batch: int = 16, backfill: bool = False):
    """Embed the attributes of nodes that have none yet.

    Scoped to nodes created since `since_iso` (the run that dispatched it), so one upload never queues
    the whole org's backlog; `backfill=True` covers every node. At most `limit` nodes per pass, and a
    partial pass reports what remains."""
    async def _run():
        from datetime import datetime
        repo, oid = NodeRepo(), uuid.UUID(workspace_id)
        since = datetime.fromisoformat(since_iso) if since_iso and not backfill else None
        pending = await repo.unembedded_ids(oid, created_after=since)
        counts: dict = {}
        for start in range(0, min(len(pending), limit), PAGE):
            page = pending[start:min(start + PAGE, limit)]
            for attr, n in (await repo.index_embeddings(oid, await repo.get_many(oid, page), embed_batch,
                                                        batch_size=batch)).items():
                counts[attr] = counts.get(attr, 0) + n
        rest = max(0, len(pending) - limit)
        if rest:
            log.warning("index.attributes_incomplete", workspace_id=str(workspace_id), unindexed=rest)
        return {"status": "ok" if not rest else "partial", "nodes": min(len(pending), limit),
                "attrs": len(counts), "remaining": rest,
                "scope": "backfill" if backfill else (since_iso or "org")}

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=3)


@celery_app.task(bind=True, queue="embedding", name="opencortyx.embed_node", max_retries=3)
def v2_embed_node(self, workspace_id: str, node_id: str):
    """Embed one node in the background, so authoring and memory writes return before the embedding
    model runs. Idempotent: re-running recomputes the vectors."""
    async def _run():
        repo, oid = NodeRepo(), uuid.UUID(workspace_id)
        node = await repo.get(oid, uuid.UUID(node_id))
        if node is None:
            return {"status": "gone"}
        await repo.index_embeddings(oid, [node], embed_batch)
        return {"status": "ok"}

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=10, max_retries=3)


#: Nodes loaded and embedded at a time by the passes that find their work by id.
PAGE = 500
#: Nodes younger than this are left to the job that is embedding them right now.
SWEEP_SETTLE_MINUTES = 10
#: How far back the sweep looks. Older gaps are a backfill (`index_node_attrs` with backfill=True).
SWEEP_WINDOW_HOURS = 24


@celery_app.task(bind=True, queue="embedding", name="opencortyx.sweep_unembedded", max_retries=0)
def v2_sweep_unembedded(self):
    """Embed recent nodes that still have no vectors, in every workspace.

    The safety net under `embed_source`: a job lost in flight (a worker stopped or killed mid-way)
    left an upload findable by keywords only, with nothing to notice. Run on a schedule; a node is
    only taken once it is SWEEP_SETTLE_MINUTES old, so it never races the job that is embedding it.
    Idempotent and cheap when there is nothing to do.
    """
    async def _run():
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import select

        from opencortyx.core.db import V2Session
        from opencortyx.domain.control import Workspace

        now = datetime.now(timezone.utc)
        settled = now - timedelta(minutes=SWEEP_SETTLE_MINUTES)
        async with V2Session() as db:
            workspaces = [w for (w,) in (await db.execute(select(Workspace.workspace_id))).all()]
        repo, done = NodeRepo(), {}
        for oid in workspaces:
            pending = await repo.unembedded_ids(oid, created_after=now - timedelta(hours=SWEEP_WINDOW_HOURS),
                                                created_before=settled)
            for start in range(0, len(pending), PAGE):
                await repo.index_embeddings(oid, await repo.get_many(oid, pending[start:start + PAGE]),
                                            embed_batch)
            if pending:
                done[str(oid)] = len(pending)
                log.warning("embedding.sweep_recovered", workspace_id=str(oid), nodes=len(pending))
        return {"status": "ok", "recovered": done}

    return async_to_sync(_run)()
