"""Routes: data definitions - Metrics, Segments and verified queries (knowledge/definitions.py)."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from opencortyx.api.deps import audit
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.knowledge import definitions

router = APIRouter()


class SegmentSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    table: str
    condition: str = Field(..., min_length=1)
    description: str = ""
    owner: Optional[str] = None


class MetricSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    table: str
    expression: str = Field(..., min_length=1)
    description: str = ""
    filters: str = ""
    segment: Optional[str] = None
    grain: str = ""
    unit: str = ""
    synonyms: list[str] = Field(default_factory=list)
    owner: Optional[str] = None


class VerifiedQuerySpec(BaseModel):
    question: str = Field(..., min_length=1)
    sql: str = Field(..., min_length=1)


@router.get("/definitions")
async def list_definitions_ep(user: CurrentUser = Depends(require_auth)):
    return await definitions.list_definitions(user.workspace_id)


async def _save(user, kind: str, coro):
    try:
        res = await coro
    except definitions.InvalidDefinition as e:
        raise HTTPException(status_code=422, detail=f"Not saved - {e}") from e
    await audit(user.workspace_id, user.user_id, f"define/{kind}",
                node_id=uuid.UUID(res["id"]) if res.get("id") else None, meta={"kind": kind})
    return res


@router.post("/definitions/segments")
async def add_segment_ep(spec: SegmentSpec, user: CurrentUser = Depends(require_write)):
    return await _save(user, "segment", definitions.add_segment(user.workspace_id, author=user.user_id,
                                                                **spec.model_dump()))


@router.post("/definitions/metrics")
async def add_metric_ep(spec: MetricSpec, user: CurrentUser = Depends(require_write)):
    return await _save(user, "metric", definitions.add_metric(user.workspace_id, author=user.user_id,
                                                              **spec.model_dump()))


@router.post("/definitions/verified-queries")
async def add_verified_query_ep(spec: VerifiedQuerySpec, user: CurrentUser = Depends(require_write)):
    return await _save(user, "verified_query", definitions.add_verified_query(
        user.workspace_id, question=spec.question, sql=spec.sql, verified_by=str(user.user_id),
        author=user.user_id))


@router.delete("/definitions/{node_id}")
async def delete_definition_ep(node_id: str, user: CurrentUser = Depends(require_write)):
    try:
        nid = uuid.UUID(node_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="definition not found") from None
    if not await definitions.delete_definition(user.workspace_id, nid):
        raise HTTPException(status_code=404, detail="definition not found")
    await audit(user.workspace_id, user.user_id, "define/delete", node_id=nid, meta={})
    return {"deleted": True}
