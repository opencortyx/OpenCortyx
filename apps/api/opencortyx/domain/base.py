"""The declarative base and the schema every OpenCortyx table lives in.

This module imports nothing from the application. It is the bottom of the dependency graph, so
`domain` can define tables without reaching back into `core` for the schema name — which is what
made `core` and `control` import each other before.
"""
from __future__ import annotations

import uuid

from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, mapped_column

#: Every OpenCortyx table lives in this isolated PostgreSQL schema, never in `public`.
V2_SCHEMA = "opencortyx_v2"


class V2Base(DeclarativeBase):
    pass


def uuid_pk():
    """A UUID primary key generated client-side, so a row knows its id before it is flushed."""
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


__all__ = ["V2_SCHEMA", "V2Base", "uuid_pk"]
