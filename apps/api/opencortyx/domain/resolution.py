"""The resolution engine's two tables: an identity index and a decision ledger.

`resolution_key` answers "which existing nodes of this type have this identity?" in one indexed
query per type, instead of reading every node of the type out of the graph.

`resolution_decision` records every decision with its evidence: which node was found to be the same
as which, and what happened to each compared attribute. It is also the attribute history: an
attribute decision keeps both values, so nothing a source said is lost when two records disagree.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base


class ResolutionKey(V2Base):
    """One identity of one node: its type and the hash of its normalised identity values."""
    __tablename__ = "resolution_key"
    __table_args__ = ({"schema": V2_SCHEMA},)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    node_type: Mapped[str] = mapped_column(String(80), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResolutionDecision(V2Base):
    """One decision. `target` is the node it was compared with; `attribute` is set for attribute
    decisions. The id is derived from the decision's content, so resolving again is a no-op."""
    __tablename__ = "resolution_decision"
    __table_args__ = (Index("ix_v2_resolution_subject", "workspace_id", "subject"),
                      Index("ix_v2_resolution_review", "workspace_id", "needs_review"),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))   # the asset being resolved
    node_type: Mapped[str] = mapped_column(String(80), nullable=False)
    relation: Mapped[str] = mapped_column(String(20), nullable=False)          # SAME | SUPPORTS | EXTENDS | CONTRADICTS
    subject: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    target: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attribute: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    method: Mapped[str] = mapped_column(String(20), default="key")
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResolutionLabel(V2Base):
    """A person's answer to "are these two the same thing?", kept so resolution can be MEASURED.

    Keyed by what the two records say (their identity values and the asset each came from), not by
    node id: node ids change when a file is read again, and a label must still apply afterwards.
    `pair_hash` is order-free (a,b == b,a), so the same pair is labelled once. Labels come from
    Review decisions, from checking automatic merges, from splits and merges a person made, and
    from an imported benchmark (`origin`). A label holds identity values, which can be personal
    data, so forgetting a subject removes its labels too."""
    __tablename__ = "resolution_label"
    __table_args__ = (Index("ix_v2_resolution_label_pair", "workspace_id", "node_type", "pair_hash", unique=True),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    node_type: Mapped[str] = mapped_column(String(80), nullable=False)
    pair_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: {"label": ..., "properties": {...}, "asset_id": ...} - what each record said when labelled
    a: Mapped[dict] = mapped_column(JSONB, default=dict)
    b: Mapped[dict] = mapped_column(JSONB, default=dict)
    same: Mapped[bool] = mapped_column(Boolean, nullable=False)
    origin: Mapped[str] = mapped_column(String(20), default="manual")   # review|audit|split|merge|manual|import
    author: Mapped[str | None] = mapped_column(String(120))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["ResolutionDecision", "ResolutionKey", "ResolutionLabel"]
