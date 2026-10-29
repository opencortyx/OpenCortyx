"""Celery task: resolve one asset once it is ingested, extracted and embedded."""
import uuid

from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync

#: An asset still waiting for its embeddings is resolved when the embedding job that finishes them
#: queues it again. In case none ever does (embedding failed for good), one backstop run this much
#: later resolves it anyway. Kept under the broker's visibility timeout (40 minutes), past which a
#: held message would be delivered twice.
BACKSTOP_SECONDS = 30 * 60


@celery_app.task(bind=True, queue="resolution", name="opencortyx.resolve_asset", acks_late=True,
                 max_retries=5)
def v2_resolve_asset(self, workspace_id: str, source_id: str, backstop: bool = False):
    """Queued when an asset completes. On its own queue, so a long resolution (thousands of statements)
    never holds up another asset. It needs the asset's embeddings (the semantic comparisons use its
    vectors), so it runs once they exist: queued by the ingest's last step, and again by the
    embedding job if it found them missing. Not polled: every asset of a large upload re-checking
    every 15 seconds walked the graph store faster than the embedding it waited for could finish,
    on the same worker. Idempotent: decisions and index rows are keyed by content."""
    from opencortyx.ingest.runs.progress import finalize_source
    from opencortyx.resolution import resolve_asset
    from opencortyx.resolution.engine import Store
    oid, sid = uuid.UUID(workspace_id), uuid.UUID(source_id)
    store = Store()

    def done(out, note=None):
        """Resolution is the asset's last step: its ingest runs are complete now, not before."""
        async_to_sync(finalize_source)(oid, sid, note=note)
        return out

    try:
        if backstop:
            if not async_to_sync(store.waiting)(oid, sid):
                return done({"status": "skipped", "reason": "resolved since"})
            return done(async_to_sync(resolve_asset)(oid, sid))
        wait = async_to_sync(store.wait_for_embeddings)(oid, sid)
        if not wait:
            return done(async_to_sync(resolve_asset)(oid, sid))
    except Exception as exc:
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 3)
        if self.request.retries >= _max:
            done(None, note=f"resolution failed: {type(exc).__name__}")
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)
    raise self.retry(args=(workspace_id, source_id), kwargs={"backstop": True}, countdown=BACKSTOP_SECONDS)


@celery_app.task(bind=True, queue="resolution", name="opencortyx.link_existing", acks_late=True, max_retries=6)
def v2_link_existing(self, workspace_id: str, types: list):
    """Apply new reference rules to the nodes already in the graph (`engine.link_existing`): queued
    when a mapping declares foreign keys, so data read before them is linked too."""
    from opencortyx.resolution.engine import Store, link_existing
    try:
        return {"links": async_to_sync(link_existing)(workspace_id, list(types or []), Store())}
    except Exception as exc:
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 3)
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)
