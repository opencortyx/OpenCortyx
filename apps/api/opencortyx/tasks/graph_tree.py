"""Graph upkeep: re-index the dashboard's browse tree after the graph changes (`knowledge.graph_tree`)."""
from opencortyx.core.config import settings
from opencortyx.core.observability import get_logger
from opencortyx.core.substrate.repo import on_graph_change
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync

log = get_logger(__name__)


#: A follow-up pass waits at least this many times as long as the last pass took, so passes take
#: at most a sixth of the time however large the graph grows (a pass reads all of it, ~90 s for
#: 179,000 nodes); during a long upload they ran back to back.
FOLLOW_UP_FACTOR = 5


def schedule_tree_index(workspace_id: str, *, now: bool = False, delay: int | None = None) -> bool:
    """Book a re-index of an organization's browse tree — debounced.

    Every graph write reports here (via `on_graph_change`). The first change in a quiet window books
    one pass `GRAPH_TREE_DEBOUNCE_S` out and every change inside the window rides on it; a change that
    lands while a pass runs sets the dirty flag and the running pass books the next one. `now` skips
    the wait (first view, or the user asked). Best-effort: no Redis means no automatic pass, never a
    failed write."""
    debounce = int(getattr(settings, "GRAPH_TREE_DEBOUNCE_S", 0) or 0)
    # Capped under the broker's visibility timeout (40 min), past which a held message is delivered twice.
    delay = 0 if now else min(30 * 60, max(debounce, int(delay or 0)))
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        r.set(f"v2:tree_dirty:{workspace_id}", b"1", ex=24 * 3600)
        # The booking holds until the booked pass STARTS (it clears it), not just for the debounce
        # window: behind a long ingest queue the pass waits far longer than the window, and a booking
        # that lapsed with it queued one more pass per window (52 behind a single PDF). The hour is
        # only a backstop for a pass that was lost.
        if not now and not r.set(f"v2:tree:{workspace_id}", b"1", nx=True, ex=3600):
            return False
        v2_index_graph_tree.apply_async(args=[str(workspace_id)], countdown=delay)
        return True
    except Exception as e:                    # noqa: BLE001 — never let graph upkeep break a write
        log.warning("graph_tree.schedule_skipped", error=str(e))
        return False


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.index_graph_tree")
def v2_index_graph_tree(self, workspace_id: str):
    """Rebuild an organization's browse-tree index. One pass per organization at a time: a request
    while one runs leaves the dirty flag set, and the running pass books a follow-up when it sees it."""
    from opencortyx.knowledge.graph_tree import reindex

    r = None
    lock, dirty = f"v2:tree_lock:{workspace_id}", f"v2:tree_dirty:{workspace_id}"
    try:
        import redis as _redis
        r = _redis.Redis.from_url(settings.CELERY_BROKER_URL)
        r.delete(f"v2:tree:{workspace_id}")           # this pass is running: changes book the next
        if not r.set(lock, b"1", nx=True, ex=3600):
            r.set(dirty, b"1", ex=24 * 3600)
            return {"status": "deferred", "reason": "an index pass is already running"}
        r.delete(dirty)
    except Exception:                          # noqa: BLE001 — no Redis: run unguarded
        r = None
    import time as _time
    started = _time.monotonic()
    try:
        return async_to_sync(reindex)(workspace_id)
    finally:
        if r is not None:
            try:
                r.delete(lock)
                if r.get(dirty):
                    r.delete(f"v2:tree:{workspace_id}")
                    schedule_tree_index(workspace_id,
                                        delay=int((_time.monotonic() - started) * FOLLOW_UP_FACTOR))
            except Exception:                  # noqa: BLE001
                pass


on_graph_change(schedule_tree_index)
