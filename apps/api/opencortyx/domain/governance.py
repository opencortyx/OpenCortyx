"""What the platform keeps about its own conduct: what the models cost, and what was forgotten.

`llm_usage`   one row per model call: who asked (workspace, run, purpose), which model, how many
              tokens went in and out, and whether those numbers were MEASURED (the provider or the
              local tokenizer said so) or ESTIMATED (characters / 4). Cost per run, per extractor and
              per 1,000 records is a GROUP BY over it.
`llm_cache`   a model's answer to one exact request (prompt, system, model, parameters), for the
              deterministic calls extraction makes, so reading an unchanged record again costs
              nothing. It holds what the model said about a record, so it carries the asset it was
              read from and is deleted with it.
`tombstone`   something that was forgotten: an asset or a subject. Kept as hashes only, so the
              tombstone does not itself remember what it forgot. Sync, connectors and Add data refuse
              to read a tombstoned asset back; a backup restore replays the list.
`deletion_job` one forget request and what each store reported, so a partial failure is a state
              someone can see and retry, not a log line.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base, uuid_pk


class LlmUsage(V2Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_ws_ts", "workspace_id", "ts"),
                      Index("ix_llm_usage_run", "workspace_id", "run_id"),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[str | None] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(120), nullable=False, default="other")
    role: Mapped[str | None] = mapped_column(String(20))
    provider: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(200))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    measured: Mapped[bool] = mapped_column(Boolean, default=False)
    paid: Mapped[bool] = mapped_column(Boolean, default=False)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmCache(V2Base):
    __tablename__ = "llm_cache"
    __table_args__ = (Index("ix_llm_cache_asset", "workspace_id", "asset_id"),
                      {"schema": V2_SCHEMA})
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    response: Mapped[str] = mapped_column(Text, nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Tombstone(V2Base):
    __tablename__ = "tombstone"
    __table_args__ = (Index("ix_tombstone_lookup", "workspace_id", "kind", "key_hash", unique=True),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)       # asset | content | subject
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeletionJob(V2Base):
    __tablename__ = "deletion_job"
    __table_args__ = (Index("ix_deletion_job_ws", "workspace_id", "created_at"),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)       # asset | subject
    #: What was asked, by reference only (ids and hashes): the job outlives what it deleted.
    target: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued")   # queued|running|done|incomplete
    #: {store: {"status": "done"|"failed"|"skipped", "count": n, "error": "..."}}
    steps: Mapped[dict] = mapped_column(JSONB, default=dict)
    #: The proof: what a search of every store found afterwards (should be all zeros).
    proof: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["DeletionJob", "LlmCache", "LlmUsage", "Tombstone"]
