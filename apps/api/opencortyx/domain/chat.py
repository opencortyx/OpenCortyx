"""The Assistant's chat history: each person's conversations, turn by turn, as they saw them.

This is NOT the graph's conversation memory. `knowledge/memory.py` stores Session/Turn nodes that
retrieval can search and cite ("Our conversations"); those are distilled, truncated prose. A chat
history has to replay a conversation exactly — every answer with its citations, grounding and trust
badges — so someone can reopen it and carry on. That is a list of rows owned by one user, so it lives
here, ordered and pageable, and deleting a chat deletes it for real.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from opencortyx.domain.base import V2_SCHEMA, V2Base, uuid_pk


class ChatThread(V2Base):
    """One conversation in the Assistant sidebar. Private to the user who started it."""
    __tablename__ = "chat_thread"
    __table_args__ = (Index("ix_v2_chat_thread_owner", "workspace_id", "user_id", "updated_at"),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New chat")
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ChatMessage(V2Base):
    """One turn. `meta` is what the UI renders under an answer (citations, grounding, trace)."""
    __tablename__ = "chat_message"
    __table_args__ = (Index("ix_v2_chat_message_thread", "thread_id", "ordinal"),
                      {"schema": V2_SCHEMA})
    id: Mapped[uuid.UUID] = uuid_pk()
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{V2_SCHEMA}.chat_thread.id", ondelete="CASCADE"),
        nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)        # user | assistant
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta: Mapped[dict] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
