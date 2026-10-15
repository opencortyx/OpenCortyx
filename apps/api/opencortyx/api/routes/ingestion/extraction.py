"""Routes: extraction templates and the shapes ingestion creates."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opencortyx.api.schemas.ingestion.extraction import (
    TemplatePreviewSpec,
    TemplateSpec,
    TemplateStatusPatch,
)
from opencortyx.api.services.ingestion.extraction import template_view
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import ExtractionTemplate

router = APIRouter()


@router.get("/templates")
async def get_extraction_templates(user: CurrentUser = Depends(require_auth)):
    """Returns available ontology extraction templates."""
    async with V2Session() as db:
        templates = (await db.execute(
            select(ExtractionTemplate).where(ExtractionTemplate.workspace_id == user.workspace_id)
        )).scalars().all()

    if not templates:
        return {"items": [
            {"id": "t1", "name": "Extract People & Organizations", "description": "Detects entities and MENTIONS edges."},
            {"id": "t2", "name": "Financial Metrics", "description": "Extracts revenue, costs, and monetary facts."},
            {"id": "t3", "name": "Risk & Compliance", "description": "Flags PII, liabilities, and restricted clauses."},
            {"id": "t4", "name": "extract_temporal_pattern", "description": "Detects date/time expressions and normalizes each to an exact ISO date against the document's timestamp."}
        ]}

    return {"items": [{"id": str(t.id), "name": t.name,
                       "description": (t.mechanism or {}).get("prompt", "")} for t in templates]}


@router.get("/reading-shapes")
async def reading_shapes(user: CurrentUser = Depends(require_auth)):
    """What INGESTION itself creates, per paradigm — the node types, their attributes and the edges
    between them, plus where each is configured.

    This is the documentation the loader cards used to carry in the extractor list. They were removed
    because they are not extractors (an extractor runs over nodes that already exist; these MAKE
    them), and because the same fact was then stated twice — once in a card you could not act on, and
    once in the schema mapping that actually decides it. Served here so the builders can show it on
    the step where it IS configurable."""
    from opencortyx.ontology.extractors import READING_SHAPES
    return {"shapes": READING_SHAPES}


@router.get("/extraction-templates")
async def list_templates(user: CurrentUser = Depends(require_auth)):
    from opencortyx.ontology.extractors import seed_builtin_templates
    async with V2Session() as db:
        await seed_builtin_templates(db, user.workspace_id)
        rows = (await db.execute(
            select(ExtractionTemplate).where(ExtractionTemplate.workspace_id == user.workspace_id))).scalars().all()
    return {"templates": [template_view(t) for t in rows]}


@router.post("/extraction-templates")
async def create_template(spec: TemplateSpec, user: CurrentUser = Depends(require_write)):
    """Create/update an enrichment template (upsert by name).

    User templates: full upsert. Built-in (`builtin:`) templates are managed by the catalog —
    DETERMINISTIC ones are fully read-only; only the PROMPT of an executable (LLM) built-in may be
    tuned (its source/emit shape is preserved)."""
    is_builtin = (spec.name or "").startswith("builtin:")
    async with V2Session() as db:
        t = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == user.workspace_id,
            ExtractionTemplate.name == spec.name
        ))).scalar_one_or_none()

        if is_builtin:
            if not t:
                raise HTTPException(status_code=403, detail="built-in templates are read-only")
            mech = dict(t.mechanism or {})
            new_prompt = (spec.mechanism or {}).get("prompt")
            if new_prompt is not None:                 # tune the prompt; keep shape/category
                mech["prompt"] = new_prompt
            if "model" in (spec.mechanism or {}):      # choose a registry model for this built-in
                mech["model"] = (spec.mechanism or {}).get("model") or None
            if "code" in (spec.mechanism or {}):        # edit a coded (Python) extractor's function body
                mech["code"] = (spec.mechanism or {}).get("code") or ""
            if "model_capable" in (spec.mechanism or {}):   # let a Python extractor call the LLM (Way 2)
                mech["model_capable"] = bool((spec.mechanism or {}).get("model_capable"))
            if "attr_overrides" in (spec.mechanism or {}):   # per-attribute prompt+model overrides
                mech["attr_overrides"] = (spec.mechanism or {}).get("attr_overrides") or {}
            if "compare" in (spec.mechanism or {}):      # editable contradiction comparison-pass config
                mech["compare"] = (spec.mechanism or {}).get("compare") or []
            for _k in ("non_answer_phrases", "refusal_phrases"):   # editable abstention guardrail phrases
                if _k in (spec.mechanism or {}):
                    mech[_k] = (spec.mechanism or {}).get(_k) or []
            mech["_user_edited"] = True                 # so the catalog re-seed never reverts this edit
            from sqlalchemy.orm.attributes import flag_modified
            if spec.emit:                              # edited nodes/edges/attributes (relationships)
                t.emit = spec.emit
                mech["_user_emit"] = True              # so the catalog re-seed preserves this emit
                flag_modified(t, "emit")
                from opencortyx.resolution.spec import save_from_emit
                await save_from_emit(db, user.workspace_id, spec.emit)
            t.mechanism = mech
            flag_modified(t, "mechanism")
            await db.commit()
            await db.refresh(t)
            return {"id": str(t.id), "name": t.name}

        if t:
            t.source = spec.source
            t.mechanism = spec.mechanism
            t.emit = spec.emit
        else:
            t = ExtractionTemplate(workspace_id=user.workspace_id, name=spec.name, source=spec.source,
                                   mechanism=spec.mechanism, emit=spec.emit)
            db.add(t)

        # auto-add any new emitted node/edge types into the ontology dictionary (as candidates),
        # so authoring a template that emits a brand-new type also DEFINES that type.
        from opencortyx.ontology.extractors import register_emit_types
        await register_emit_types(db, user.workspace_id, spec.emit)
        # an emitted node's `id` fields and `resolve` block are how its type is resolved (stored on the type)
        from opencortyx.resolution.spec import save_from_emit
        await save_from_emit(db, user.workspace_id, spec.emit)

        await db.commit()
        await db.refresh(t)
    return {"id": str(t.id), "name": t.name}


@router.post("/extraction-templates/{tid}/reset")
async def reset_builtin_template(tid: str, user: CurrentUser = Depends(require_write)):
    """Put an edited built-in back to its catalog definition — prompt, model, code, emit and all.

    A built-in's prompt is editable and the edit survives every re-seed (that is the point of
    `_user_edited`), so without this the only way back from a bad edit was to type the original
    prompt in again from memory. The on/off status is left as the user set it."""
    from opencortyx.ontology.extractors import BUILTIN_TEMPLATES
    try:
        key = uuid.UUID(tid)
    except ValueError:
        raise HTTPException(status_code=404, detail="extractor not found")
    async with V2Session() as db:
        t = await db.get(ExtractionTemplate, key)
        if t is None or t.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="extractor not found")
        spec = next((b for b in BUILTIN_TEMPLATES if b["name"] == t.name), None)
        if spec is None:
            raise HTTPException(status_code=400, detail="only built-in extractors have a default to reset to")
        t.source, t.mechanism, t.emit = spec["source"], dict(spec["mechanism"]), spec["emit"]
        await db.commit()
    return {"status": "reset", "name": spec["name"]}


@router.post("/extraction-templates/preview")
async def preview_template_ep(spec: TemplatePreviewSpec, user: CurrentUser = Depends(require_auth)):
    """Live preview: run an UNSAVED extractor over `sample_text` and return the nodes / edges /
    attributes it would produce — each attribute annotated with mechanism, model, weight and the
    concrete value. Read-only (no DB writes) so authors can iterate before saving or running."""
    from opencortyx.ingest.extraction.templates import preview_template
    return await preview_template(user.workspace_id, {
        "name": spec.name, "source": spec.source,
        "mechanism": spec.mechanism, "emit": spec.emit,
    }, spec.sample_text or "")


@router.post("/extraction-templates/{tid}/run")
async def run_template_ep(tid: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Run an executable enrichment template over the existing graph (async). Attaches the
    LLM-generated attributes / nodes / edges onto the nodes the template selects."""
    async with V2Session() as db:
        t = await db.get(ExtractionTemplate, tid)
        if not t or t.workspace_id != user.workspace_id:
            return {"status": "not_found"}
        view = template_view(t)
    if not view["executable"]:
        raise HTTPException(status_code=400, detail="this template is deterministic / view-only and cannot be run")
    from opencortyx.tasks.enrich import v2_run_extractor
    task = v2_run_extractor.delay(str(user.workspace_id), str(tid))
    return {"task_id": task.id, "status": "dispatched"}


@router.post("/extraction/run-all")
async def run_all_extractors_ep(user: CurrentUser = Depends(require_write)):
    """Run every executable enrichment template over the existing graph (excludes chunk-time ones)."""
    from opencortyx.tasks.enrich import v2_run_all_extractors
    task = v2_run_all_extractors.delay(str(user.workspace_id))
    return {"task_id": task.id, "status": "dispatched"}


@router.patch("/extraction-templates/{tid}")
async def patch_template_status(tid: uuid.UUID, spec: TemplateStatusPatch,
                                user: CurrentUser = Depends(require_write)):
    """Enable/disable an extractor. Disabled templates are skipped by the engine everywhere
    (is_executable checks status) — this is what the Settings→Extraction selection saves."""
    if spec.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status must be 'active' or 'disabled'")
    async with V2Session() as db:
        t = await db.get(ExtractionTemplate, tid)
        if not t or t.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="template not found")
        t.status = spec.status
        await db.commit()
    return {"id": str(tid), "status": spec.status}


@router.delete("/extraction-templates/{tid}")
async def delete_template(tid: uuid.UUID, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        t = await db.get(ExtractionTemplate, tid)
        if not t or t.workspace_id != user.workspace_id:
            return {"status": "not_found"}
        if (t.name or "").startswith("builtin:"):
            raise HTTPException(status_code=403, detail="built-in templates cannot be deleted")
        await db.delete(t)
        await db.commit()
    return {"status": "deleted"}
