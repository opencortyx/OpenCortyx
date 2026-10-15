"""The browse tree the dashboard graph draws: root → source → tag → asset → what the asset produced.

The top three levels are questions about ASSETS (which sources exist, which tags a source's assets
carry, which assets carry a tag), and an organization can hold millions of them. So they are answered
from these rows — indexed, countable, pageable — rather than by scanning the graph. Everything below
an asset is read from the graph store directly, one page at a time.

These rows are an index, not the truth: an asset's tags live on the asset node (`properties.tags`),
and `knowledge/graph_tree.py` rebuilds everything here from the graph. Losing them loses nothing.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base


class GraphAsset(V2Base):
    """One asset (a Source or a Table node) and the source it came from."""
    __tablename__ = "graph_asset"
    __table_args__ = (Index("ix_v2_graph_asset_source", "workspace_id", "source", "label"),
                      {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="")


class GraphAssetTag(V2Base):
    """One tag on one asset. `auto` marks the default tag (the file extension), which the asset
    carries only while nobody has tagged it."""
    __tablename__ = "graph_asset_tag"
    __table_args__ = (Index("ix_v2_graph_asset_tag_tag", "workspace_id", "source", "tag", "label"),
                      {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tag: Mapped[str] = mapped_column(String(80), primary_key=True)
    source: Mapped[str] = mapped_column(String(200), nullable=False, default="")   # copied: tag counts per source
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")    # copied: page order
    auto: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class GraphRoot(V2Base):
    """A part of the graph no asset reaches — nodes someone authored, or imported without a file.
    One row per connected part, named after its best-connected node, which is where it opens."""
    __tablename__ = "graph_root"
    __table_args__ = (Index("ix_v2_graph_root_size", "workspace_id", "size"), {"schema": V2_SCHEMA})
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    label: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    type: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)          # nodes in the part
    types: Mapped[dict] = mapped_column(JSONB, default=dict)


class GraphTagRequest(V2Base):
    """Tags given with an upload, waiting for the ingest to say which asset they belong to.

    The upload is answered before the worker has read the file, so the asset id does not exist yet;
    the run id does. The next index pass moves the tags onto the asset the run produced."""
    __tablename__ = "graph_tag_request"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    tags: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphTreeRun(V2Base):
    """The last index pass of an organization: whether it ran, and what it found."""
    __tablename__ = "graph_tree_run"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")   # running | ok | error
    nodes: Mapped[int | None] = mapped_column(Integer)
    assets: Mapped[int | None] = mapped_column(Integer)
    roots: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
