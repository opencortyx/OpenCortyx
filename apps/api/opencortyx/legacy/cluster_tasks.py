"""Legacy: rebuild the community hierarchy (`legacy.graph_clusters.recluster`) — see `legacy/__init__.py`.

Not in the worker's task list, so nothing runs it. To run a pass by hand:
`async_to_sync(recluster)(workspace_id)` from a shell in the api container.
"""
from opencortyx.core.config import settings
from opencortyx.core.observability import get_logger
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync

log = get_logger(__name__)


def schedule_recluster(workspace_id: str) -> bool:
    """Book one debounced rebuild `GRAPH_CLUSTER_DEBOUNCE_S` out; changes inside the window ride on it."""
    delay = int(getattr(settings, "GRAPH_CLUSTER_DEBOUNCE_S", 0) or 0)
    if delay <= 0:
        return False
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        r.set(f"v2:recluster_dirty:{workspace_id}", b"1", ex=24 * 3600)
        if not r.set(f"v2:recluster:{workspace_id}", b"1", nx=True, ex=delay):
            return False
        v2_cluster_graph.apply_async(args=[str(workspace_id)], countdown=delay)
        return True
    except Exception as e:                    # noqa: BLE001
        log.warning("clusters.schedule_skipped", error=str(e))
        return False


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.cluster_graph")
def v2_cluster_graph(self, workspace_id: str):
    """One pass per organization at a time: a request while one runs leaves the dirty flag set, and
    the running pass books a follow-up when it sees it."""
    from opencortyx.legacy.graph_clusters import recluster

    r = None
    lock = f"v2:recluster_lock:{workspace_id}"
    dirty = f"v2:recluster_dirty:{workspace_id}"
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        if not r.set(lock, b"1", nx=True, ex=3600):
            r.set(dirty, b"1", ex=24 * 3600)
            return {"status": "deferred", "reason": "a clustering pass is already running"}
        r.delete(dirty)
    except Exception:                          # noqa: BLE001 — no Redis: run unguarded
        r = None
    try:
        return async_to_sync(recluster)(workspace_id)
    finally:
        if r is not None:
            try:
                r.delete(lock)
                if r.get(dirty):
                    r.delete(f"v2:recluster:{workspace_id}")
                    schedule_recluster(workspace_id)
            except Exception:                  # noqa: BLE001
                pass
