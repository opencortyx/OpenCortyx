"""The identity map: a source's own id for a record ("customer 101" in the CRM) -> the graph node.

Written when the Mapper or row mode makes a node from a row or record; read by L0 entity binding
("does customer 101 have a free pass?" binds to the canonical Customer) and by the SQL planner,
which turns a bound node back into the key it filters the source on.
"""
from __future__ import annotations

import uuid
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.core.db import V2Session
from opencortyx.domain.data import SourceKey


def _u(v):
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


async def record(workspace_id, asset_id, items: Iterable[tuple], match_method: str = "declared",
                 confidence: float = 1.0) -> int:
    """`items`: (native_id, entity_type, node_id). Idempotent; a repeat refreshes `last_seen`."""
    rows = [{"workspace_id": _u(workspace_id), "asset_id": _u(asset_id), "native_id": str(n)[:300],
             "entity_type": str(t)[:120], "node_id": _u(node), "match_method": match_method,
             "confidence": float(confidence)} for n, t, node in items if n not in (None, "")]
    if not rows:
        return 0
    async with V2Session() as db:
        stmt = pg_insert(SourceKey.__table__).values(rows)
        await db.execute(stmt.on_conflict_do_update(
            index_elements=["workspace_id", "asset_id", "native_id", "entity_type"],
            set_={"node_id": stmt.excluded.node_id, "last_seen": func.now(),
                  "match_method": stmt.excluded.match_method, "confidence": stmt.excluded.confidence}))
        await db.commit()
    return len(rows)


async def nodes_for(workspace_id, native_id: str, entity_type: Optional[str] = None) -> list[dict]:
    """Graph nodes a source id names, across every asset (case-insensitive on the id)."""
    q = select(SourceKey).where(SourceKey.workspace_id == _u(workspace_id),
                                func.lower(SourceKey.native_id) == str(native_id).lower())
    if entity_type:
        q = q.where(SourceKey.entity_type == entity_type)
    async with V2Session() as db:
        rows = (await db.execute(q)).scalars().all()
    return [{"node_id": str(r.node_id), "asset_id": str(r.asset_id), "entity_type": r.entity_type,
             "native_id": r.native_id, "confidence": r.confidence} for r in rows]


async def keys_for(workspace_id, node_id) -> list[dict]:
    """The source ids a node is known by, per asset - what the SQL planner filters on."""
    async with V2Session() as db:
        rows = (await db.execute(select(SourceKey).where(
            SourceKey.workspace_id == _u(workspace_id), SourceKey.node_id == _u(node_id)))).scalars().all()
    return [{"asset_id": str(r.asset_id), "native_id": r.native_id, "entity_type": r.entity_type}
            for r in rows]
