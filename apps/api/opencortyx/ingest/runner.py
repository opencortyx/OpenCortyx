"""One entry point for ingesting a file: decide its kind, then run the default walker for that kind."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from opencortyx.ingest.sources import sync
from opencortyx.ingest.walkers import walker_for


async def ingest_file(workspace_id: uuid.UUID, content: bytes, filename: str, *, kind: Optional[str] = None,
                      pipeline: Optional[list] = None, pipeline_id: Optional[str] = None,
                      collection_id: Optional[uuid.UUID] = None, **options: Any) -> dict:
    """Ingest one file with the default walker for `kind`, detected from the file when not given."""
    walker = walker_for(sync.normalize_kind(kind) if kind else sync.file_kind(filename, content))
    return await walker.ingest_file(workspace_id, content, filename, pipeline=pipeline, pipeline_id=pipeline_id,
                                    collection_id=collection_id, **options)
