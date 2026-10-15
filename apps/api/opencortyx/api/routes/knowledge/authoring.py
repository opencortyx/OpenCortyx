"""Routes: draft a mapping, a pipeline or an extractor from real data.

Every endpoint here is a READ. Drafting saves nothing — a draft is persisted by POSTing its
`payload` to the endpoint named in its `save_endpoint`, which is the CRUD that already owns that
object. That keeps one write path per object and makes these safely retryable: the same upload
drafted twice produces two equivalent proposals and no duplicate rows.

`require_auth` rather than `require_write` for the same reason. Drafting is looking at your own
file and being told what the system would do with it; a viewer is allowed to find that out.
"""
import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from opencortyx.api.deps import guard_readable, guard_upload_size
from opencortyx.api.schemas.knowledge.authoring import DraftPreviewSpec, DraftResponse, DraftSpec
from opencortyx.api.services.knowledge import authoring as service
from opencortyx.core.security.auth import CurrentUser, require_auth

router = APIRouter()


@router.get("/authoring/drafters")
async def list_drafters(user: CurrentUser = Depends(require_auth)):
    """Every registered drafter. Lets a UI offer "read this a different way" without a hard-coded
    list that drifts from what is installed."""
    from opencortyx.drafting import registry
    return {"items": [d.describe() for d in registry.all()]}


@router.post("/authoring/draft", response_model=DraftResponse)
async def draft_from_content(spec: DraftSpec, user: CurrentUser = Depends(require_auth)):
    """Draft from inline content, or from a goal alone.

    The JSON twin of the upload below, for callers that already hold the bytes — an agent over
    MCP, or the UI re-drafting after an edit without making someone pick the file again.
    """
    content = (spec.content or "").encode("utf-8") if spec.content else None
    if not content and not (spec.goal or "").strip():
        raise HTTPException(status_code=422,
                            detail="send `content` to draft from a sample, or `goal` to describe "
                                   "what you want extracted")
    return await _draft(user, spec, content, spec.filename, "")


@router.post("/authoring/draft/upload", response_model=DraftResponse)
async def draft_from_upload(
    file: UploadFile = File(...),
    kind: str = Form("mapping"),
    goal: str = Form(""),
    drafter: str = Form(""),
    use_model: bool = Form(True),
    generation_model: str = Form(""),   # `model_ref` would collide with pydantic's model_* namespace
    context: str = Form(""),            # JSON: caller-supplied steering, e.g. {"mapping_id": "…"}
    user: CurrentUser = Depends(require_auth),
):
    """Draft from an uploaded file.

    The file is read, sampled and discarded — this endpoint never ingests. It exists so the
    question "what would you do with this?" can be answered before the question "do it", which is
    the entire point: a user reviews a reading of their own data instead of authoring one blind.
    """
    content = await file.read()
    guard_upload_size(content, file.filename)
    guard_readable(content, file.filename)
    try:
        steering = json.loads(context) if context.strip() else {}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="`context` must be a JSON object") from exc
    if not isinstance(steering, dict):
        raise HTTPException(status_code=422, detail="`context` must be a JSON object")
    spec = DraftSpec(kind=kind, goal=goal, drafter=drafter or None, use_model=use_model,
                     model_ref=generation_model or None, context=steering)
    return await _draft(user, spec, content, file.filename or "upload",
                        file.content_type or "")


@router.post("/authoring/draft/preview")
async def preview_draft(spec: DraftPreviewSpec, user: CurrentUser = Depends(require_auth)):
    """Run an edited draft against a sample without saving it. See `services.authoring`."""
    try:
        return await service.preview_payload(user.workspace_id, spec.kind, spec.payload,
                                             spec.sample_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _draft(user: CurrentUser, spec: DraftSpec, content: bytes | None,
                 filename: str, mime: str) -> dict:
    try:
        req = service.make_request(
            user.workspace_id, kind=spec.kind, content=content, filename=filename, mime=mime,
            goal=spec.goal, use_model=spec.use_model, model_ref=spec.model_ref,
            context=spec.context)
    except ValueError as exc:                       # an unknown DraftKind reaching us past pydantic
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        drafts = await service.run_drafters(req, drafter=spec.drafter)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return service.response(req, drafts)
