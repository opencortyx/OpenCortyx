"""Tombstones: hashes of what was forgotten, so nothing reads it back (knowledge/forget.py).

Kept in core because the one place every ingestion writes an asset through, NodeRepo, refuses a
tombstoned asset here - and core may not import the layers above it. Only hashes are stored: the
tombstone does not itself remember what it forgot.
"""
from __future__ import annotations

import hashlib
import uuid
from typing import Iterable

def key_hash(value) -> str:
    from opencortyx.resolution.normalize import norm
    v = norm(value) if not isinstance(value, uuid.UUID) else str(value)
    return hashlib.sha256(str(v if v is not None else value).encode("utf-8")).hexdigest()


class Forgotten(RuntimeError):
    """Something being written was forgotten on purpose (a tombstone)."""


# ── tombstones ────────────────────────────────────────────────────────────────────────────────

# Read fresh every time, never cached: forget runs in the API while ingestion runs in the workers,
# and a per-process cache let a worker accept a forgotten file again for as long as its copy lived.
# Every caller runs once per file, per restore or per question, against a unique index.

async def _rows(workspace_id, kind=None, hashes=None) -> set:
    try:
        from sqlalchemy import select
        from opencortyx.core.db import V2Session
        from opencortyx.domain.governance import Tombstone
        q = select(Tombstone.kind, Tombstone.key_hash).where(Tombstone.workspace_id == uuid.UUID(str(workspace_id)))
        if kind is not None:
            q = q.where(Tombstone.kind == kind)
        if hashes is not None:
            q = q.where(Tombstone.key_hash.in_(list(hashes)))
        async with V2Session() as db:
            return {(k, h) for k, h in (await db.execute(q)).all()}
    except Exception as e:                                    # noqa: BLE001
        # No table yet (before the migration) or no database (offline tests): nothing is
        # tombstoned. Ingestion cannot run without the database anyway, so this opens nothing.
        import structlog
        structlog.get_logger(__name__).warning("tombstones.unreadable", error=f"{type(e).__name__}: {e}"[:200])
        return set()


async def _tombstones(workspace_id) -> set:
    return await _rows(workspace_id)


async def is_tombstoned(workspace_id, kind: str, value) -> bool:
    return bool(await _rows(workspace_id, kind, [key_hash(value)]))


async def guard_assets(workspace_id, node_ids: Iterable) -> None:
    """Raise Forgotten when one of these asset ids was forgotten."""
    hashes = {key_hash(uuid.UUID(str(nid))): nid for nid in node_ids}
    if not hashes:
        return
    for _, h in sorted(await _rows(workspace_id, "asset", list(hashes))):
        raise Forgotten(f"This asset ({hashes[h]}) was forgotten and is not read again. Remove it from its "
                        "source, or lift the tombstone under Settings → Data deletion.")


async def add_tombstones(workspace_id, kind: str, values: Iterable, *, job_id=None, by=None) -> int:
    from sqlalchemy.dialects.postgresql import insert
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import Tombstone
    rows = [{"id": uuid.uuid4(), "workspace_id": uuid.UUID(str(workspace_id)), "kind": kind,
             "key_hash": key_hash(v), "job_id": job_id, "created_by": by}
            for v in dict.fromkeys(values) if v not in (None, "")]
    if not rows:
        return 0
    async with V2Session() as db:
        await db.execute(insert(Tombstone).values(rows).on_conflict_do_nothing())
        await db.commit()
    return len(rows)


async def lift_tombstone(workspace_id, tombstone_id) -> bool:
    from sqlalchemy import delete
    from opencortyx.core.db import V2Session
    from opencortyx.domain.governance import Tombstone
    async with V2Session() as db:
        res = await db.execute(delete(Tombstone).where(Tombstone.workspace_id == uuid.UUID(str(workspace_id)),
                                                       Tombstone.id == uuid.UUID(str(tombstone_id))))
        await db.commit()
    return bool(res.rowcount)


