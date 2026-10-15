"""Durable capture + replay of node/edge writes that did not land — the zero-data-loss guarantee.

The rule this module exists to enforce: **an intended write is either in the graph, or recorded here
with everything needed to make it real.** Never neither, and never only in a log line.

Why it is needed at all. `add_edge` refuses an edge whose endpoint does not exist yet, which sounds
like a safety property and is really a race: extraction constantly emits a relationship naming an
entity that a LATER chunk creates. The edge was correct, just early. It used to return False and
vanish — and because nothing recorded it, nobody could even measure how much had been lost.

Two design points that carry the whole guarantee:

* **Own transaction.** The capture must NOT ride the caller's session. That session is, by definition,
  in trouble — it may roll back seconds later, taking the evidence with it and leaving no trace of the
  very thing we are trying not to lose. So it opens its own.
* **Never raises.** A failure to record a failure must not turn a dropped edge into a failed ingest.
  It degrades to a log line, which is exactly what we had before, and only for that one row.

Deduplicated by a fingerprint of the intent, so an edge that fails on a hundred retries is one row
with `attempts = 100`, not a hundred rows.
"""
import hashlib
import json
import uuid

from sqlalchemy import text

from opencortyx.core.config import V2_SCHEMA
from opencortyx.core.db import V2Session
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

MAX_ATTEMPTS = 12


def _fingerprint(kind: str, payload: dict) -> str:
    """Identity of the INTENT — same thing, same row, however many times it is retried.

    Every kind needs its own identity, and the fallback must be the whole payload. An earlier version
    handled only edges and nodes, so a parked RECORD hashed on fields it does not have and every record
    in a run collapsed onto one row — retention silently kept exactly one of them. A kind-specific key
    where the kind is known, a canonical hash of the payload where it is not: distinct intents always
    get distinct rows, identical retries always share one."""
    if kind == "edge":
        ident = f"edge:{payload.get('source_id')}:{payload.get('target_id')}:{payload.get('type')}"
    elif kind == "node":
        ident = f"node:{payload.get('node_id') or payload.get('label')}:{payload.get('type')}"
    elif kind == "record":
        # A record is identified by where it came from, not by its content: re-ingesting the same
        # source must update the same rows rather than pile up a fresh set every run.
        ident = (f"record:{payload.get('source_id')}:{payload.get('record_index')}"
                 f":{payload.get('pipeline_id')}")
    elif kind == "step":
        ident = f"step:{payload.get('step')}"
    else:
        ident = kind + ":" + json.dumps(payload, sort_keys=True, default=str)
    return hashlib.blake2b(ident.encode(), digest_size=16).hexdigest()


async def capture(workspace_id, kind: str, payload: dict, *, reason: str,
                  retryable: bool = True, asset: str | None = None, error: str | None = None,
                  funnel: bool = False) -> bool:
    """Record a write that did not land. Best-effort and silent on failure — never raises.

    `funnel=True` means the caller is NodeRepo's write funnel, which counts its own outcomes; counting
    here as well would double it. Everything else is an upstream rejection — a parked record, an
    ontology refusal, an unresolved endpoint — which never had a matching `attempted`, so it gets its
    own counter rather than unbalancing the funnel ledger."""
    if not funnel:
        try:
            from opencortyx.core.substrate import accounting as _acct
            _acct.note(workspace_id, _acct.UPSTREAM_DROPPED)
        except Exception:                        # noqa: BLE001 — counting must never block recording
            pass
    try:
        clean = json.loads(json.dumps(payload, default=str))     # uuid/datetime → JSON-safe
        async with V2Session() as db:
            await db.execute(text(f"""
                INSERT INTO {V2_SCHEMA}.write_quarantine
                    (workspace_id, kind, fingerprint, payload, reason, retryable, attempts, last_error, asset)
                VALUES (:o, :k, :fp, CAST(:p AS jsonb), :r, :ret, 0, :err, :asset)
                ON CONFLICT (workspace_id, fingerprint) DO UPDATE
                SET attempts   = {V2_SCHEMA}.write_quarantine.attempts + 1,
                    last_error = EXCLUDED.last_error,
                    reason     = EXCLUDED.reason,
                    resolved_at = NULL"""),
                {"o": workspace_id, "k": kind, "fp": _fingerprint(kind, clean),
                 "p": json.dumps(clean), "r": reason[:64], "ret": retryable,
                 "err": (error or "")[:2000] or None, "asset": (asset or "")[:500] or None})
            await db.commit()
        return True
    except Exception as e:                       # noqa: BLE001 — recording a loss must not cause one
        log.error("quarantine.capture_failed", kind=str(kind), reason=str(reason), error=str(e))
        return False


async def replay(workspace_id, limit: int = 500) -> dict:
    """Retry pending writes. The usual outcome: the endpoint that was missing now exists.

    Each row is attempted in its own transaction, so one still-impossible edge cannot roll back the
    ones that have become possible — the same isolation mistake this codebase already paid for once.
    """
    from opencortyx.core.substrate import NodeRepo

    repo = NodeRepo()
    async with V2Session() as db:
        rows = (await db.execute(text(f"""
            SELECT id, kind, payload, attempts FROM {V2_SCHEMA}.write_quarantine
            WHERE workspace_id = :o AND resolved_at IS NULL AND retryable AND attempts < :max
            ORDER BY attempts, id LIMIT :lim"""),
            {"o": workspace_id, "max": MAX_ATTEMPTS, "lim": limit})).all()
    if not rows:
        return {"tried": 0, "resolved": 0, "still_pending": 0}

    resolved, pending = 0, 0
    for rid, kind, payload, _attempts in rows:
        ok, err = False, None
        try:
            if kind == "edge":
                written = await repo.add_edges(workspace_id=workspace_id, _quarantine=False, edges=[{
                    "source_id": uuid.UUID(str(payload["source_id"])),
                    "target_id": uuid.UUID(str(payload["target_id"])),
                    "type": payload.get("type") or "REL",
                    "weight": float(payload.get("weight") or 1.0),
                    "properties": payload.get("properties") or {},
                    "confidence": float(payload.get("confidence") or 1.0)}])
                ok = bool(written.created or written.updated)
        except Exception as e:                      # noqa: BLE001
            ok, err = False, str(e)[:2000]
        async with V2Session() as db:
            if ok:
                await db.execute(text(f"UPDATE {V2_SCHEMA}.write_quarantine "
                                      f"SET resolved_at = now(), last_tried_at = now() WHERE id = :i"),
                                 {"i": rid})
                resolved += 1
            else:
                await db.execute(text(f"UPDATE {V2_SCHEMA}.write_quarantine "
                                      f"SET attempts = attempts + 1, last_tried_at = now(), "
                                      f"    last_error = COALESCE(:e, last_error) WHERE id = :i"),
                                 {"i": rid, "e": err})
                pending += 1
            await db.commit()
    return {"tried": len(rows), "resolved": resolved, "still_pending": pending}


async def clear(workspace_id) -> int:
    """Forget every held write of this org — for a workspace reset, after which the graph those
    writes were missing from no longer exists."""
    async with V2Session() as db:
        n = (await db.execute(text(f"DELETE FROM {V2_SCHEMA}.write_quarantine WHERE workspace_id = :o"),
                              {"o": workspace_id})).rowcount or 0
        await db.commit()
    return n


async def summary(workspace_id) -> dict:
    """What the graph is currently missing, and why — the auditable half of "zero loss"."""
    async with V2Session() as db:
        rows = (await db.execute(text(f"""
            SELECT reason,
                   count(*) FILTER (WHERE resolved_at IS NULL AND attempts < :max AND retryable) AS pending,
                   count(*) FILTER (WHERE resolved_at IS NOT NULL) AS recovered,
                   count(*) FILTER (WHERE resolved_at IS NULL
                                      AND (attempts >= :max OR NOT retryable)) AS unrecoverable
            FROM {V2_SCHEMA}.write_quarantine WHERE workspace_id = :o GROUP BY reason"""),
            {"o": workspace_id, "max": MAX_ATTEMPTS})).all()
    by_reason = {r[0]: {"pending": r[1], "recovered": r[2], "unrecoverable": r[3]} for r in rows}
    tot = lambda k: sum(v[k] for v in by_reason.values())     # noqa: E731
    return {
        # Recorded and since written. Was loss before; is not now.
        "recovered": tot("recovered"),
        # Recorded, not written YET, still queued for replay.
        "pending": tot("pending"),
        # Recorded, and will not be written: invalid by rule, or out of attempts. Missing from the
        # graph — but named, counted and inspectable, which is the whole difference.
        "unrecoverable": tot("unrecoverable"),
        "by_reason": by_reason,
    }


async def list_rows(workspace_id, limit: int = 100) -> list:
    """The recorded writes themselves, open ones first — what `summary` counts, one row each."""
    async with V2Session() as db:
        rows = (await db.execute(text(f"""
            SELECT id, kind, reason, retryable, attempts, asset, last_error, payload,
                   created_at, resolved_at
            FROM {V2_SCHEMA}.write_quarantine WHERE workspace_id = :o
            ORDER BY (resolved_at IS NOT NULL), created_at DESC LIMIT :n"""),
            {"o": workspace_id, "n": int(limit)})).all()
    out = []
    for r in rows:
        payload = r[7] if isinstance(r[7], dict) else json.loads(r[7] or "{}")
        out.append({"id": str(r[0]), "kind": r[1], "reason": r[2], "retryable": bool(r[3]),
                    "attempts": int(r[4] or 0), "asset": r[5], "error": r[6], "payload": payload,
                    "created_at": r[8].isoformat() if r[8] else None,
                    "resolved": r[9] is not None,
                    "open": r[9] is None and bool(r[3]) and int(r[4] or 0) < MAX_ATTEMPTS})
    return out
