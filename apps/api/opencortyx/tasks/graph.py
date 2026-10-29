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


async def embeddable(repo, oid, sid) -> list:
    """Everything one asset embeds: its graph subtree at any depth and, in a Delta workspace, its
    document's passages (in the data store, not the graph). One definition, read by the embedding
    job and by resolution's "are the embeddings done?" - which, reading the graph alone, resolved a
    document (and called its run complete) before its passages were searchable."""
    from opencortyx.ingest.structured.dataplane import uses_delta
    root = await repo.get(oid, sid)
    subtree = ([root] if root else []) + await repo.descendants(oid, [sid])
    if await uses_delta(oid):
        from opencortyx.ingest.unstructured import dataplane as _udata
        # In the document's collection: a passage point without one is visible to every
        # collection's search (a question scoped to one collection read another's passages).
        subtree += list((await _udata.load_records(
            oid, sid, collection_id=getattr(root, "collection_id", None))).values())
    return [n for n in subtree if str((n.properties or {}).get("__structural") or "") != "true"]


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
        subtree = await embeddable(repo, oid, sid)
        pending = await repo.unembedded(oid, subtree)
        if pending:
            await repo.index_embeddings(oid, pending, embed_batch, batch_size=_rc.embed_batch_size())
            spec = await active_embedding_spec()
            if str((spec or {}).get("provider") or "").lower() in ("fastembed", "onnx"):
                gc.collect()
        # Read again, not from before embedding: the asset's resolution may have started waiting
        # for these vectors in the meantime. An asset not resolved yet is queued too: keyed on the
        # waiting mark alone, a resolution that marked it just after this read waited for the
        # 30-minute backstop - and its ingest run stayed unfinished that long. Resolution is
        # idempotent, so one queued twice costs a second (cheap) pass, nothing more.
        from opencortyx.resolution.engine import RESOLVE_WAITING
        root = await repo.get(oid, sid)
        props = (root.properties or {}) if root else {}
        if root and (props.get(RESOLVE_WAITING) or not props.get("__resolved_at")):
            from opencortyx.tasks.resolution import v2_resolve_asset
            v2_resolve_asset.delay(workspace_id, source_id)
        return {"status": "ok", "source_id": source_id, "embedded": len(pending)}

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        # A store restarting is waited out (~16 minutes, as ingest does): 3 tries 30 s apart lost 50
        # files' vectors for good while Qdrant restarted during a thousand-file upload.
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 3)
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)


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
        # A store restarting is waited out (~16 minutes, as ingest does): 3 tries 30 s apart lost 50
        # files' vectors for good while Qdrant restarted during a thousand-file upload.
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 3)
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)


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
#: Most nodes without vectors one sweep looks at; the rest are found by the next.
SWEEP_SCAN = 200_000


@celery_app.task(bind=True, queue="embedding", name="opencortyx.sweep_unembedded", max_retries=0)
def v2_sweep_unembedded(self):
    """Embed nodes that still have no vectors, in every workspace.

    The safety net under `embed_source`: a job lost in flight (a worker stopped, a store restarting
    past its retries) left an upload findable by keywords only, with nothing to notice. What is
    owed is read from the vector store (`keyword_only_ids`), not found by scanning the graph: a
    scan of the last day's nodes read all of a thousand-file upload every 10 minutes. A node is
    taken only when the previous sweep also found it, so it never races the job embedding it now.
    Idempotent and cheap when there is nothing to do.
    """
    async def _run():
        from sqlalchemy import select

        from opencortyx.core.config import settings
        from opencortyx.core.db import V2Session
        from opencortyx.domain.control import Workspace

        async with V2Session() as db:
            workspaces = [w for (w,) in (await db.execute(select(Workspace.workspace_id))).all()]
        try:
            import redis as _redis
            r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        except Exception:                                          # noqa: BLE001 — no Redis: no settling
            r = None
        repo, done = NodeRepo(), {}
        for oid in workspaces:
            owed = await repo.keyword_only_ids(oid, limit=SWEEP_SCAN)
            key = f"v2:sweep_owed:{oid}"
            seen = {m.decode() for m in r.smembers(key)} if r is not None else None
            if r is not None:
                r.delete(key)
                if owed:
                    r.sadd(key, *owed)
                    r.expire(key, 3600)
            pending = [i for i in owed if seen is None or i in seen]
            for start in range(0, len(pending), PAGE):
                await repo.index_embeddings(oid, await repo.get_many(oid, pending[start:start + PAGE]),
                                            embed_batch)
            if pending:
                done[str(oid)] = len(pending)
                log.warning("embedding.sweep_recovered", workspace_id=str(oid), nodes=len(pending))
        return {"status": "ok", "recovered": done}

    return async_to_sync(_run)()
