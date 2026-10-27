"""The ontology table and the rules for reading a node's type.

The ontology says which node, edge and entity types exist and what they mean; those descriptions
are what drive extraction, so they are data the operator edits rather than constants in code.

Each organization has its own ontology on top of a shared, read-only platform one:

  * PLATFORM rows (`workspace_id = PLATFORM_ORG`) are the built-in vocabulary seeded at start-up. Every
    organization sees them; none can change or delete them.
  * An organization's rows are its own: types it registers, candidates its extraction discovers,
    and its edits. Changing a platform type copies it into the organization first (`own_type`), and
    that copy then shadows the platform row for this organization only (`effective`).

One organization's ontology therefore never shows up in, or changes the extraction of, another's.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text, func, select, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base

#: Owner of the built-in types every organization shares (read-only to organizations).
PLATFORM_ORG = uuid.UUID(int=0)

#: Node types that are ingested assets: a file (Source) or a table (Table).
#: The node an asset is, in the graph. `Source` and `Table` in a workspace whose data lives in the graph;
#: the three kinds in one whose data lives in Delta tables (docs/knowledge-layer-implementation.md).
ASSET_TYPES = ("Table", "Source", "StructuredAsset", "SemiStructuredAsset", "UnstructuredAsset")
#: The asset node type for each kind of asset, when data lives in Delta tables.
ASSET_TYPE_BY_KIND = {"structured": "StructuredAsset", "semistructured": "SemiStructuredAsset",
                      "unstructured": "UnstructuredAsset"}


def base_type_of(node) -> str:
    """What a node was ingested as, seeing through an anchor retype."""
    return (node.properties or {}).get("__base_type") or node.type


def is_asset_node(node) -> bool:
    return base_type_of(node) in ASSET_TYPES


class TypeRegistry(V2Base):
    """The ontology: allowed node, edge and entity types and the descriptions that drive extraction."""
    __tablename__ = "type_registry"
    __table_args__ = ({"schema": V2_SCHEMA},)
    # first in the key, so `db.get(TypeRegistry, (workspace_id, name, kind))`
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=PLATFORM_ORG,
                                              server_default=text(f"'{PLATFORM_ORG}'::uuid"))
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)     # 'node' | 'edge' | 'entity'
    description: Mapped[str] = mapped_column(Text, nullable=False)
    properties_schema: Mapped[dict] = mapped_column(JSONB, default=dict)
    allowed_source_types: Mapped[list | None] = mapped_column(ARRAY(String))  # edges only
    allowed_target_types: Mapped[list | None] = mapped_column(ARRAY(String))  # edges only
    is_open: Mapped[bool] = mapped_column(default=True)
    source: Mapped[str] = mapped_column(String(20), default="system")   # system|user|discovered
    status: Mapped[str] = mapped_column(String(20), default="active")   # active|candidate
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


#: The unique key an upsert conflicts on.
TYPE_KEY = ["workspace_id", "name", "kind"]


def _org(workspace_id) -> uuid.UUID:
    return workspace_id if isinstance(workspace_id, uuid.UUID) else uuid.UUID(str(workspace_id))


def visible_to(workspace_id):
    """Filter: the rows an organization sees, the platform's and its own."""
    return TypeRegistry.workspace_id.in_([PLATFORM_ORG, _org(workspace_id)])


def effective(rows) -> list:
    """One row per (name, kind): an organization's own row shadows the platform's."""
    out: dict = {}
    for r in rows:
        key = (r.name, r.kind)
        if key not in out or out[key].workspace_id == PLATFORM_ORG:
            out[key] = r
    return list(out.values())


async def visible_types(db, workspace_id, *conditions) -> list:
    """The organization's effective types, optionally narrowed by extra filter conditions."""
    rows = (await db.execute(select(TypeRegistry).where(visible_to(workspace_id), *conditions))).scalars().all()
    return effective(rows)


async def find_type(db, workspace_id, name: str, kind: str):
    """The type this organization sees under (name, kind): its own, else the platform's, else None."""
    return (await db.get(TypeRegistry, (_org(workspace_id), name, kind))
            or await db.get(TypeRegistry, (PLATFORM_ORG, name, kind)))


async def own_type(db, workspace_id, name: str, kind: str):
    """This organization's own row for (name, kind), ready to change. A platform type is copied into
    the organization first (copy-on-write), so the change never reaches another organization.
    None when the organization sees no such type."""
    org = _org(workspace_id)
    own = await db.get(TypeRegistry, (org, name, kind))
    if own is not None:
        return own
    base = await db.get(TypeRegistry, (PLATFORM_ORG, name, kind))
    if base is None:
        return None
    own = TypeRegistry(workspace_id=org, name=base.name, kind=base.kind, description=base.description,
                       properties_schema=copy.deepcopy(base.properties_schema or {}),
                       allowed_source_types=list(base.allowed_source_types or []) or None,
                       allowed_target_types=list(base.allowed_target_types or []) or None,
                       is_open=base.is_open, source=base.source, status=base.status)
    db.add(own)
    return own


__all__ = ["ASSET_TYPES", "ASSET_TYPE_BY_KIND", "PLATFORM_ORG", "TYPE_KEY", "TypeRegistry", "base_type_of", "effective",
           "find_type", "is_asset_node", "own_type", "visible_to", "visible_types"]
