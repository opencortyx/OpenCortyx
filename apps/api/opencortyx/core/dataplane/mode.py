"""Which store a workspace's data is written to: `delta` (the data store) or `graph` (before 0.2.0).

Read on every ingest and every retrieval read, so cached for a minute per process; a change to the
setting (knowledge/workspace.save_profile) drops the cached value at once in the process that made it.
A workspace with no profile row yet is a new one, and new workspaces start on `delta`.
"""
from __future__ import annotations

import time
import uuid

DATA_STORES = ("delta", "graph")
DEFAULT = "delta"
_TTL = 60.0
_cache: dict = {}


async def data_store(workspace_id) -> str:
    key = str(workspace_id)
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    from opencortyx.core.db import V2Session
    from opencortyx.domain.control import Workspace
    try:
        async with V2Session() as db:
            row = await db.get(Workspace, uuid.UUID(key))
        value = getattr(row, "data_store", None) if row is not None else DEFAULT
        value = value if value in DATA_STORES else DEFAULT
    except Exception:                                         # noqa: BLE001 - unknown: the old path
        return "graph"
    _cache[key] = (time.monotonic(), value)
    return value


async def uses_delta(workspace_id) -> bool:
    return (await data_store(workspace_id)) == "delta"


def forget(workspace_id) -> None:
    _cache.pop(str(workspace_id), None)
