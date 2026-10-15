"""The contract every default walker implements."""
from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseWalker(ABC):
    """Reads one file of a single kind (structured, semi-structured or unstructured) into the graph."""

    kind: str

    @abstractmethod
    async def ingest_file(self, workspace_id: uuid.UUID, content: bytes, filename: str, *,
                          pipeline: Optional[list] = None, pipeline_id: Optional[str] = None,
                          collection_id: Optional[uuid.UUID] = None, **options: Any) -> dict:
        """Parse `content`, write its nodes and edges, and return the ingest summary with `source_id`."""
