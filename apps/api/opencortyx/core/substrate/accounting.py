"""Measured accounting for the graph write funnel — turns "zero loss" from a claim into a number.

Until now `unaccounted` was reported as 0 because every branch that abandons a write had been read and
made to capture. That is an argument, not evidence: it is only as good as the reading, and a path
added next month would still report zero while dropping data.

This makes it observable instead. Every call into the write funnel is counted as `attempted`, and each
call must end in exactly one outcome — `written` or `quarantined`. The funnel records the outcome in a
`finally`, so a return path that produces NEITHER is counted as `unaccounted` automatically, without
anyone having anticipated it. A new branch that silently drops an edge therefore shows up as a
non-zero number the first time it runs, which is the whole point.

Counters live in the process and are flushed as DELTAS, so every worker adds into the same totals.
A killed process loses its unflushed counters — but it loses `attempted`, `written` and `quarantined`
together, so the balance is preserved rather than skewed; at worst a single in-flight write is
reported as unaccounted, which is honest, because it was attempted and did not land.
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import text

from opencortyx.core.config import V2_SCHEMA
from opencortyx.core.db import V2Session
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

ATTEMPTED, WRITTEN, QUARANTINED = "attempted", "written", "quarantined"
CREATED, UPDATED, DELETED = "created", "updated", "deleted"
ESCAPED = "escaped"
# Rejected before it ever reached the funnel: parked records, ontology rejections, unresolved
# endpoints. Captured and visible, but it never had a matching `attempted`.
UPSTREAM_DROPPED = "upstream_dropped"
# The only one that is always a defect: returned without writing, without capturing, without telling.
UNACCOUNTED = "unaccounted"

_counts: dict = defaultdict(lambda: defaultdict(int))
_window: dict = {}
_lock = asyncio.Lock()


def note(workspace_id, kind: str, n: int = 1) -> None:
    """Record one funnel event. Deliberately synchronous and allocation-free on the hot path."""
    if workspace_id is None:
        return
    key = str(workspace_id)
    if key not in _window:
        _window[key] = datetime.now(timezone.utc)
    _counts[key][kind] += n


async def flush() -> dict:
    """Fold this process's deltas into the shared totals. Safe to call from any worker, any time."""
    async with _lock:
        pending = {o: dict(k) for o, k in _counts.items() if any(k.values())}
        windows = {o: _window.get(o) for o in pending}
        _counts.clear()
        _window.clear()
    if not pending:
        return {"orgs": 0}
    try:
        async with V2Session() as db:
            for org, k in pending.items():
                # Drop deltas that predate a re-baseline: they describe writes the operator has already
                # accounted for by resetting, and replaying them would re-break the identity.
                base = (await db.execute(text(
                    f"SELECT baseline_at FROM {V2_SCHEMA}.write_accounting WHERE workspace_id = :o"),
                    {"o": org})).scalar()
                w = windows.get(org)
                if base is not None and w is not None and base > w:
                    continue
                await db.execute(text(f"""
                    INSERT INTO {V2_SCHEMA}.write_accounting
                        (workspace_id, attempted, written, quarantined, unaccounted,
                         escaped, upstream_dropped, created, updated, deleted, updated_at)
                    VALUES (CAST(:o AS uuid), :a, :w, :q, :u, :e, :d, :cr, :up, :dl, now())
                    ON CONFLICT (workspace_id) DO UPDATE SET
                        attempted   = {V2_SCHEMA}.write_accounting.attempted   + EXCLUDED.attempted,
                        written     = {V2_SCHEMA}.write_accounting.written     + EXCLUDED.written,
                        quarantined = {V2_SCHEMA}.write_accounting.quarantined + EXCLUDED.quarantined,
                        unaccounted = {V2_SCHEMA}.write_accounting.unaccounted + EXCLUDED.unaccounted,
                        escaped     = {V2_SCHEMA}.write_accounting.escaped     + EXCLUDED.escaped,
                        upstream_dropped = {V2_SCHEMA}.write_accounting.upstream_dropped
                                           + EXCLUDED.upstream_dropped,
                        created     = {V2_SCHEMA}.write_accounting.created     + EXCLUDED.created,
                        updated     = {V2_SCHEMA}.write_accounting.updated     + EXCLUDED.updated,
                        deleted     = {V2_SCHEMA}.write_accounting.deleted     + EXCLUDED.deleted,
                        updated_at  = now()"""),
                    {"o": org, "a": k.get(ATTEMPTED, 0), "w": k.get(WRITTEN, 0),
                     "q": k.get(QUARANTINED, 0), "u": k.get(UNACCOUNTED, 0),
                     "e": k.get(ESCAPED, 0), "d": k.get(UPSTREAM_DROPPED, 0),
                     "cr": k.get(CREATED, 0), "up": k.get(UPDATED, 0), "dl": k.get(DELETED, 0)})
            await db.commit()
    except Exception as e:                      # noqa: BLE001 — put the deltas back, try again later
        async with _lock:
            for org, k in pending.items():
                for kind, v in k.items():
                    _counts[org][kind] += v
        log.warning("accounting.flush_failed", error=str(e), note="counts retained in process")
        return {"orgs": 0, "error": str(e)[:200]}
    return {"orgs": len(pending)}


async def rebaseline(workspace_id, reason: str = "manual") -> dict:
    """Reset the counters so they agree with the graph as it is NOW. The alarm's off switch.

    Needed because `row_delta` is cumulative: once a bypass (or pre-instrumentation history) puts it
    off by N it stays off by N, so fixing the bypass does not silence the alarm. Without a way back to
    zero the signal degrades into noise people filter out — which is worse than not measuring at all.

    `created` is set to the live row count and everything else zeroed, in ONE statement so the numbers
    can never disagree with the count they were derived from. `baseline_at` then tells every other
    process to discard whatever it was still holding from before this moment; they need no restart and
    no coordination."""
    async with _lock:
        _counts.pop(str(workspace_id), None)          # this process's own pending deltas are pre-baseline too
        _window.pop(str(workspace_id), None)
    live_rows = await _rows_in_graph(workspace_id)
    async with V2Session() as db:
        await db.execute(text(f"""
            INSERT INTO {V2_SCHEMA}.write_accounting
                (workspace_id, attempted, written, quarantined, unaccounted, escaped, upstream_dropped,
                 created, updated, deleted, baseline_at, baseline_reason, updated_at)
            SELECT :o, 0, 0, 0, 0, 0, 0, :rows, 0, 0, now(), :r, now()
            ON CONFLICT (workspace_id) DO UPDATE SET
                attempted = 0, written = 0, quarantined = 0, unaccounted = 0, escaped = 0,
                upstream_dropped = 0, updated = 0, deleted = 0,
                created = EXCLUDED.created,
                baseline_at = now(), baseline_reason = EXCLUDED.baseline_reason, updated_at = now()"""),
            {"o": workspace_id, "r": reason[:200], "rows": live_rows})
        await db.commit()
    return await report(workspace_id)


async def _rows_in_graph(workspace_id) -> int:
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    return await repo.count(workspace_id) + await repo.count_edges(workspace_id)


async def report(workspace_id) -> dict:
    """Durable totals plus whatever this process has not flushed yet."""
    row = None
    try:
        async with V2Session() as db:
            row = (await db.execute(text(f"""
                SELECT attempted, written, quarantined, unaccounted, escaped, upstream_dropped,
                       created, updated, deleted
                FROM {V2_SCHEMA}.write_accounting WHERE workspace_id = :o"""), {"o": workspace_id})).first()
        rows = await _rows_in_graph(workspace_id)
    except Exception:                           # noqa: BLE001 — pre-migration install
        return {"available": False}
    live = _counts.get(str(workspace_id), {})
    a, w, q, u, e, dr, cr, up, dl = (row or (0, 0, 0, 0, 0, 0, 0, 0, 0))
    out = {
        "attempted": int(a) + live.get(ATTEMPTED, 0),
        "written": int(w) + live.get(WRITTEN, 0),
        "quarantined": int(q) + live.get(QUARANTINED, 0),
        # Handled, not lost: the exception reached a caller that recorded it.
        "escaped": int(e) + live.get(ESCAPED, 0),
        # Rejected before the funnel; captured, but never had a matching `attempted`.
        "upstream_dropped": int(dr) + live.get(UPSTREAM_DROPPED, 0),
        # Measured, not asserted, and now unambiguous: a call that wrote nothing, captured nothing and
        # told nobody. Non-zero here is always a defect.
        "unaccounted": int(u) + live.get(UNACCOUNTED, 0),
        "created": int(cr) + live.get(CREATED, 0),
        "updated": int(up) + live.get(UPDATED, 0),
        "deleted": int(dl) + live.get(DELETED, 0),
        "rows_in_graph": int(rows),
        "available": True,
    }
    out["balance_delta"] = out["attempted"] - (out["written"] + out["quarantined"]
                                               + out["escaped"] + out["unaccounted"])
    out["row_delta"] = (out["created"] - out["deleted"]) - out["rows_in_graph"]

    # Everything a monitor should page on, already evaluated. Each of these is always a defect: none
    # of them goes non-zero during healthy operation, however heavy the load or contention.
    alarms = []
    if out["unaccounted"]:
        alarms.append(f"{out['unaccounted']} write(s) returned without writing, capturing or raising "
                      f"— a code path is dropping data silently")
    if out["balance_delta"]:
        alarms.append(f"write counters disagree by {out['balance_delta']} "
                      f"(attempted != written + quarantined + escaped + unaccounted)")
    if out["row_delta"]:
        alarms.append(f"{out['row_delta']} row(s) differ from what the funnel accounts for "
                      f"— something writes or deletes the graph without going through NodeRepo")
    out["alarms"] = alarms
    out["healthy"] = not alarms
    return out
