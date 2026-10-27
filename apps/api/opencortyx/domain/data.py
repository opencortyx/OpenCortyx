"""The data plane's control rows: which assets exist, where their data lives, and what backs a fact.

Data itself never lives here. Managed data is in Delta tables under DATA_ROOT, external data stays
in the customer's lake or database, knowledge is in the graph. These three tables are the index that
ties them together (docs/knowledge-layer-implementation.md, section 2.2):

  - `data_asset`: one row per asset - its kind, where its data is (a managed Delta path, or an
    external table, path or database reference) and the version last read.
  - `evidence`: the fact ledger. One row per (knowledge node, edge or property) x the chunk, row or
    record that supports it, pinned to the version it was read at.
  - `source_key`: the identity map from a source's own id ("customer 101") to the graph node.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base, uuid_pk

#: The three asset kinds, each with its own group of Delta tables.
ASSET_KINDS = ("structured", "semistructured", "unstructured")
#: managed: the data is ours, in Delta under DATA_ROOT. external: read in place, never copied.
ASSET_STORAGE = ("managed", "external")
#: What a piece of evidence points at.
UNIT_KINDS = ("chunk", "row", "record")


class DataAsset(V2Base):
    __tablename__ = "data_asset"
    __table_args__ = (
        Index("ix_data_asset_ws_kind", "workspace_id", "kind"),
        Index("ix_data_asset_connector", "workspace_id", "connector_id"),
        {"schema": V2_SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)          # ASSET_KINDS
    storage: Mapped[str] = mapped_column(String(10), nullable=False, default="managed")
    #: delta / iceberg / hudi / parquet / csv / json / pdf / postgres / mysql / sqlite / mongodb / api ...
    format: Mapped[str] = mapped_column(String(20), nullable=False, default="delta")
    #: managed: the Delta table path. external: the table path, URI or `<schema>.<table>` in a database.
    location: Mapped[str] = mapped_column(Text, nullable=False, default="")
    connector_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: Delta version, Iceberg snapshot id, file checksum, or a sync watermark.
    version_ref: Mapped[str | None] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Evidence(V2Base):
    __tablename__ = "evidence"
    __table_args__ = (
        Index("ix_evidence_target", "workspace_id", "target_id"),
        Index("ix_evidence_unit", "workspace_id", "asset_id", "unit_key"),
        {"schema": V2_SCHEMA},
    )
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(10), nullable=False, default="node")  # node|edge
    property: Mapped[str | None] = mapped_column(String(200))      # None: the node or edge itself
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(10), nullable=False)    # UNIT_KINDS
    unit_key: Mapped[str] = mapped_column(String(300), nullable=False)
    version_ref: Mapped[str | None] = mapped_column(String(200))
    quote: Mapped[str | None] = mapped_column(Text)
    value_hash: Mapped[str | None] = mapped_column(String(64))
    extractor: Mapped[str | None] = mapped_column(String(200))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    confidence: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SourceKey(V2Base):
    __tablename__ = "source_key"
    __table_args__ = (
        Index("ix_source_key_node", "workspace_id", "node_id"),
        {"schema": V2_SCHEMA},
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    native_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(120), primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    match_method: Mapped[str] = mapped_column(String(40), nullable=False, default="declared")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["ASSET_KINDS", "ASSET_STORAGE", "UNIT_KINDS", "DataAsset", "Evidence", "SourceKey"]
