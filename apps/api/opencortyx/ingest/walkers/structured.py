"""Default walker for tables."""
from __future__ import annotations

import os
import tempfile
import uuid
from typing import Any, Optional

from .base import BaseWalker


class StructuredWalker(BaseWalker):
    """Tables in files (CSV, TSV, Parquet, Excel, Avro, ...).

    The table becomes an `Table` node with a `Attribute` node per column; rows become nodes with their
    columns as attributes, and foreign-key columns become edges. The pipeline's reading rule or a
    bound mapping decides column roles, and "Extract only" keeps the schema without rows.
    """

    kind = "structured"

    async def ingest_file(self, workspace_id: uuid.UUID, content: bytes, filename: str, *,
                          pipeline: Optional[list] = None, pipeline_id: Optional[str] = None,
                          collection_id: Optional[uuid.UUID] = None, step1_cfg: Optional[dict] = None,
                          **options: Any) -> dict:
        from opencortyx.ingest.structured.ingest import (
            ingest_tabular_file, resolve_structured_step1)
        if step1_cfg is None:
            step1_cfg = await resolve_structured_step1(workspace_id, pipeline)
        fd, path = tempfile.mkstemp(suffix=os.path.splitext(filename)[1] or ".csv")   # DuckDB reads a path
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            return await ingest_tabular_file(workspace_id, path, filename, step1_cfg=step1_cfg,
                                             collection_id=collection_id, **options)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @classmethod
    def introspect(cls, content: bytes, filename: str = "", sample_n: int = 20) -> dict:
        """Introspect a tabular file using DuckDB and role detection without persisting."""
        from opencortyx.ingest.formats.structured import tabular

        suffix = os.path.splitext(filename or "")[1] or ".csv"
        fd, path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(content)
            return tabular.preview_file(path, filename or f"upload{suffix}", sample_n=sample_n)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
