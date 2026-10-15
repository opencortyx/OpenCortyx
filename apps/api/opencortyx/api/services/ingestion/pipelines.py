"""Services: reusable ingestion pipelines."""
from opencortyx.core.observability import get_logger

log = get_logger(__name__)



def pipeline_view(p):
    from opencortyx.ingest.pipelines.defaults import normalize_kind
    return {"id": str(p.id), "name": p.name, "description": p.description or "",
            "kind": normalize_kind(p.kind), "stages": p.stages or [],
            "stage_count": len(p.stages or []), "is_system_default": False,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None}


PIPELINE_KINDS = ("document", "structured", "semistructured")


async def register_semistructured_types(db, workspace_id, stages: list) -> None:
    """A semi-structured pipeline's structure cards ARE its schema — publish their node/edge types
    into the ontology so extractors can bind to them (source-node selector, graph, validation).
    This is what makes 'define a JSON structure' also 'declare its types'. Best-effort."""
    try:
        from opencortyx.ingest.semistructured import (
            STRUCTURES_STAGE,
            register_structures,
        )
        cfg = next((s.get("config") or {} for s in (stages or [])
                    if isinstance(s, dict) and s.get("type") == STRUCTURES_STAGE), {})
        await register_structures(db, workspace_id, cfg.get("structures") or [])
    except Exception as e:                         # noqa: BLE001 — never block a pipeline save
        log.warning("pipelines.semistructured_type_registration_failed", error=str(e))
