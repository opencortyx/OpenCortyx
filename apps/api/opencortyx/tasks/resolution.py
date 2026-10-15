"""Celery task: resolve one asset once it is ingested, extracted and embedded."""
import uuid

from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync

#: How long an asset waits for its embeddings before it is resolved anyway (an asset that is never
#: embedded, with no embedding model configured, must still be resolved): WAIT_TRIES × WAIT_SECONDS.
WAIT_TRIES, WAIT_SECONDS = 40, 15


class _NotEmbeddedYet(Exception):
    pass


@celery_app.task(bind=True, queue="resolution", name="opencortyx.resolve_asset", acks_late=True,
                 max_retries=WAIT_TRIES + 2)
def v2_resolve_asset(self, workspace_id: str, source_id: str):
    """Queued when an asset completes. On its own queue, so a long resolution (thousands of statements)
    never holds up embeddings or another asset; it waits for this asset's embeddings first, because the
    semantic comparisons need its vectors. Idempotent: decisions and index rows are keyed by content."""
    from opencortyx.resolution import resolve_asset
    from opencortyx.resolution.engine import Store
    oid, sid = uuid.UUID(workspace_id), uuid.UUID(source_id)
    try:
        if self.request.retries < WAIT_TRIES and async_to_sync(Store().pending_embeddings)(oid, sid):
            raise _NotEmbeddedYet()
        return async_to_sync(resolve_asset)(oid, sid)
    except _NotEmbeddedYet:
        raise self.retry(countdown=WAIT_SECONDS)
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30)
