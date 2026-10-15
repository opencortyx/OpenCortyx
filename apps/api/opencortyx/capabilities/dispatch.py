"""Dispatch — run the registered lifecycle hooks at the right pipeline moment.

`run_post_ingest` is the datasource-agnostic seam: EVERY ingest task (document / structured /
semi-structured) calls it once the source's nodes are written, and every enabled PostIngestHook
capability (Versioning, Contradiction Detection, …) runs scoped to that source. This is what makes
the ingest-time capabilities fire uniformly for all datasource types — not just documents.

Best-effort by contract: a failing capability is logged and skipped; it never aborts an ingest.
"""
from __future__ import annotations

import uuid
from typing import Optional

from opencortyx.capabilities.base import IngestContext
from opencortyx.capabilities.registry import registry
from opencortyx.core.observability import get_logger

log = get_logger(__name__)


async def run_post_ingest(workspace_id: uuid.UUID, source_ref: Optional[uuid.UUID], kind: str,
                          unit_type: str = "Source") -> dict:
    """Run all enabled post-ingest capabilities over one freshly-ingested source.

    `kind` is document | structured | semistructured; `source_ref` is the unit node (Source /
    dataset) id (may be None for schema-level SQL ingests, in which case per-source capabilities
    that need a unit will no-op). Returns {capability_name: result}."""
    ctx = IngestContext(workspace_id=workspace_id, source_ref=source_ref, kind=kind, unit_type=unit_type)
    results: dict = {}
    for cap in registry.post_ingest_hooks():
        try:
            if not cap.enabled(workspace_id):
                continue
            results[cap.name] = await cap.on_source_ingested(ctx)
        except Exception as e:                      # never let a capability abort the ingest
            log.warning("capability.post_ingest_failed", capability=str(cap.name), error=str(e))
            results[cap.name] = {"error": str(e)}
    return results
