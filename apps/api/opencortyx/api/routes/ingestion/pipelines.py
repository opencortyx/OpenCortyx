"""Routes: reusable ingestion pipelines."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.schemas.ingestion.pipelines import PipelinePatch, PipelineSpec
from opencortyx.api.services.ingestion.pipelines import (
    PIPELINE_KINDS,
    pipeline_view,
    register_semistructured_types,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import Pipeline
from opencortyx.ingest.pipelines.defaults import is_default_pipeline_id

router = APIRouter()


@router.get("/pipelines")
async def list_pipelines(kind: Optional[str] = None, user: CurrentUser = Depends(require_auth)):
    """List pipelines (optionally filtered by kind) for the Pipelines dashboard + ingest assign.

    The 3 SYSTEM DEFAULTS are merged in ahead of the saved ones. They are virtual (never rows), so
    they must come from the server — the ids listed here are the ids every ingest path resolves."""
    from opencortyx.ingest.pipelines.defaults import (
        default_pipelines,
        is_default_pipeline_id,
        normalize_kind,
    )
    want = normalize_kind(kind) if kind else None
    async with V2Session() as db:
        q = select(Pipeline).where(Pipeline.workspace_id == user.workspace_id)
        rows = (await db.execute(q.order_by(Pipeline.updated_at.desc()))).scalars().all()
    saved = [pipeline_view(p) for p in rows
             # defensive: an older build seeded the defaults as per-org rows; those ids are
             # unreachable duplicates of the virtual ones, so never list them twice.
             if not is_default_pipeline_id(p.id, user.workspace_id)]
    if want:
        saved = [v for v in saved if v["kind"] == want]
    defaults = [d.view() for d in default_pipelines(user.workspace_id, want)]
    return {"items": defaults + saved}


@router.post("/pipelines")
async def create_pipeline(spec: PipelineSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    kind = spec.kind if spec.kind in PIPELINE_KINDS else "document"
    async with V2Session() as db:
        p = Pipeline(workspace_id=user.workspace_id, name=spec.name.strip(), description=spec.description or "",
                     kind=kind, stages=spec.stages or [])
        db.add(p)
        if kind == "semistructured":
            await register_semistructured_types(db, user.workspace_id, spec.stages or [])
        await db.commit()
        await db.refresh(p)
        return pipeline_view(p)


@router.get("/pipelines/{pipeline_id}")
async def get_pipeline(pipeline_id: str, user: CurrentUser = Depends(require_auth)):
    from opencortyx.ingest.pipelines.defaults import resolve_pipeline
    async with V2Session() as db:
        p = await resolve_pipeline(db, user.workspace_id, pipeline_id)
    if p is None:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return p.view()                      # ResolvedPipeline covers both a saved row and a default


@router.put("/pipelines/{pipeline_id}")
async def update_pipeline(pipeline_id: str, patch: PipelinePatch,
                          user: CurrentUser = Depends(require_write)):
    if is_default_pipeline_id(pipeline_id, user.workspace_id):
        # A system default is shared and virtual — editing it would silently change everyone's
        # zero-config behaviour. Save the edit as a new pipeline instead (the UI already does this).
        raise HTTPException(status_code=400,
                            detail="System default pipelines can't be edited — save a copy instead.")
    async with V2Session() as db:
        p = await db.get(Pipeline, uuid.UUID(pipeline_id))
        if not p or p.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="pipeline not found")
        if patch.name is not None:
            p.name = patch.name.strip() or p.name
        if patch.description is not None:
            p.description = patch.description
        if patch.kind in PIPELINE_KINDS:
            p.kind = patch.kind
        if patch.stages is not None:
            p.stages = patch.stages
        if p.kind == "semistructured" and patch.stages is not None:
            await register_semistructured_types(db, p.workspace_id, patch.stages)   # re-publish types on every save
        await db.commit()
        await db.refresh(p)
        return pipeline_view(p)


@router.delete("/pipelines/{pipeline_id}")
async def delete_pipeline(pipeline_id: str, user: CurrentUser = Depends(require_write)):
    if is_default_pipeline_id(pipeline_id, user.workspace_id):
        raise HTTPException(status_code=400, detail="System default pipelines can't be deleted.")
    async with V2Session() as db:
        res = await db.execute(delete(Pipeline).where(
            Pipeline.id == uuid.UUID(pipeline_id), Pipeline.workspace_id == user.workspace_id))
        await db.commit()
    return {"status": "ok", "deleted": res.rowcount or 0}
