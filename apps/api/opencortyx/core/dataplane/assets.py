"""The asset registry (`data_asset`): which assets a workspace has and where each one's data is.

An asset keeps the id it already has - the graph's asset node (a document's source, a table) - so a
row here, its Delta table and its node are one asset under one id, and purge, versioning and the
graph tree need no mapping between them.
"""
from __future__ import annotations

import uuid
from typing import Iterable, Optional

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from opencortyx.core.db import V2Session
from opencortyx.domain.data import ASSET_KINDS, ASSET_STORAGE, DataAsset, Evidence, SourceKey


def _uuid(v) -> uuid.UUID:
    return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))


async def register(workspace_id, asset_id, *, kind: str, name: str, storage: str = "managed",
                   format: str = "delta", location: str = "", connector_id=None,
                   version_ref: Optional[str] = None, metadata: Optional[dict] = None,
                   tags: Optional[list] = None) -> uuid.UUID:
    """Create or update an asset's row. Idempotent: a re-ingest updates the same row."""
    if kind not in ASSET_KINDS:
        raise ValueError(f"kind must be one of {ASSET_KINDS}, got {kind!r}")
    if storage not in ASSET_STORAGE:
        raise ValueError(f"storage must be one of {ASSET_STORAGE}, got {storage!r}")
    aid = _uuid(asset_id)
    # Column names, not attribute names: `metadata` is `metadata_` on the model (SQLAlchemy reserves it).
    vals = {"workspace_id": _uuid(workspace_id), "kind": kind, "storage": storage, "format": format,
            "location": location or "", "name": (name or "")[:300], "status": "active",
            "connector_id": _uuid(connector_id) if connector_id else None,
            "version_ref": None if version_ref is None else str(version_ref),
            "metadata": dict(metadata or {}), "tags": list(tags or [])}
    async with V2Session() as db:
        stmt = pg_insert(DataAsset.__table__).values(id=aid, **vals)
        update = {k: v for k, v in vals.items() if k != "workspace_id"}
        update["updated_at"] = func.now()
        await db.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=update))
        await db.commit()
    return aid


async def get(workspace_id, asset_id) -> Optional[DataAsset]:
    async with V2Session() as db:
        row = await db.get(DataAsset, _uuid(asset_id))
    return row if row is not None and row.workspace_id == _uuid(workspace_id) else None


async def list_assets(workspace_id, kinds: Optional[Iterable[str]] = None,
                      ids: Optional[Iterable] = None, status: str = "active") -> list[DataAsset]:
    q = select(DataAsset).where(DataAsset.workspace_id == _uuid(workspace_id))
    if status:
        q = q.where(DataAsset.status == status)
    if kinds:
        q = q.where(DataAsset.kind.in_(list(kinds)))
    if ids is not None:
        q = q.where(DataAsset.id.in_([_uuid(i) for i in ids]))
    async with V2Session() as db:
        return list((await db.execute(q.order_by(DataAsset.name))).scalars().all())


async def set_version(workspace_id, asset_id, version_ref) -> None:
    async with V2Session() as db:
        row = await db.get(DataAsset, _uuid(asset_id))
        if row is not None and row.workspace_id == _uuid(workspace_id):
            row.version_ref = None if version_ref is None else str(version_ref)
            await db.commit()


async def remove(workspace_id, asset_id) -> bool:
    """An asset gone: its managed data, its evidence, its source keys and its row. The customer's
    own data (an external asset) is never touched. Returns whether there was a row."""
    ws, aid = _uuid(workspace_id), _uuid(asset_id)
    row = await get(ws, aid)
    if row is not None and row.storage == "managed":
        from opencortyx.stores.data import DataStore
        DataStore(ws).delete_asset(row.kind, aid)
    async with V2Session() as db:
        await db.execute(delete(Evidence).where(Evidence.workspace_id == ws, Evidence.asset_id == aid))
        await db.execute(delete(SourceKey).where(SourceKey.workspace_id == ws, SourceKey.asset_id == aid))
        await db.execute(delete(DataAsset).where(DataAsset.workspace_id == ws, DataAsset.id == aid))
        await db.commit()
    return row is not None


async def refs(workspace_id, kinds: Optional[Iterable[str]] = None, ids: Optional[Iterable] = None):
    """The workspace's assets as view definitions for stores/data. External database assets get
    their connection string resolved here, for one connection only (never stored or logged)."""
    from opencortyx.stores.data import AssetRef
    out = []
    for row in await list_assets(workspace_id, kinds=kinds, ids=ids):
        ref = AssetRef.from_row(row)
        if row.storage == "external" and row.connector_id:
            from opencortyx.core.dataplane.connectors import attach_dsn, connector_url
            from opencortyx.stores.data.duck import ADAPTED
            if row.format in ("postgres", "mysql", "sqlite"):
                ref.dsn = await attach_dsn(workspace_id, row.connector_id, row.format)
            elif row.format in ADAPTED:
                ref.dsn = await connector_url(workspace_id, row.connector_id)
        out.append(ref)
    return out
