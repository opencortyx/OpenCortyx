"""Routes: saved, re-runnable ingestions and their runs."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select

from opencortyx.api.schemas.ingestion.ingestions import (
    AnchorPreview,
    IngestionPatch,
    IngestionSpec,
)
from opencortyx.api.services.ingestion.ingestions import (
    clean_mappings,
    ingestion_config,
    ingestion_view,
    run_view,
    safe_uuid,
    selections_of,
)
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.control import IngestRun, Ingestion
from opencortyx.ingest.runs.progress import create_run
from opencortyx.ingest.sources import sync
from opencortyx.tasks.sync import v2_sync_ingestion

router = APIRouter()


@router.post("/ingestions/anchors/preview")
async def preview_anchors(body: AnchorPreview, user: CurrentUser = Depends(require_auth)):
    """What these rules WOULD match, without ingesting anything.

    Rules are otherwise configured blind: a mistyped glob is invisible until after a run, and by
    then it has written thousands of wrong edges or none at all. Pure path matching — no DB, no
    files read — so the editor can call it on every keystroke.

    Returns 400 with the specific rule problem, because "no matches" and "rule 2 is malformed" need
    to look different to whoever is typing.
    """
    from opencortyx.ingest.extraction import anchors as _anchors
    try:
        rules = _anchors.parse_rules(body.anchors)
    except _anchors.AnchorRuleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _anchors.preview([str(p) for p in (body.paths or [])], rules)


@router.get("/ingestions")
async def list_ingestions(user: CurrentUser = Depends(require_auth)):
    from opencortyx.ingest.runs.progress import ONE_TIME
    async with V2Session() as db:
        rows = (await db.execute(select(Ingestion).where(Ingestion.workspace_id == user.workspace_id)
                                 .order_by(Ingestion.updated_at.desc()))).scalars().all()
        items = [ingestion_view(i) for i in rows]
        # A one-time upload has no executor to write its status back, so it is derived from its runs:
        # any still going → running · any failure → failed · otherwise completed.
        run_ids = {r for it in items if it["mode"] == ONE_TIME for r in it["run_ids"]}
        if run_ids:
            stats = dict((await db.execute(select(IngestRun.id, IngestRun.status).where(
                IngestRun.id.in_([uuid.UUID(r) for r in run_ids])))).all())
            for it in items:
                if it["mode"] != ONE_TIME:
                    continue
                sts = [stats.get(uuid.UUID(r)) for r in it["run_ids"] if uuid.UUID(r) in stats]
                it["run_status"] = {s: sts.count(s) for s in set(sts)}
                it["status"] = ("running" if any(s in ("queued", "running") for s in sts)
                                else "failed" if any(s == "failed" for s in sts)
                                else "completed" if sts else it["status"])
    return {"items": items}


@router.get("/ingestions/{ingestion_id}/runs")
async def list_ingestion_runs(ingestion_id: str, user: CurrentUser = Depends(require_auth)):
    """The runs behind one saved ingestion — every file of a one-time upload, or the connector
    ingestion's run history. Full per-stage detail, so the UI can expand a row into its runs."""
    async with V2Session() as db:
        ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="Ingestion not found")
        ids = [uuid.UUID(str(r)) for r in ((ing.config or {}).get("runs") or []) if r]
        if not ids and ing.last_run_id:
            try:
                ids = [uuid.UUID(str(ing.last_run_id))]
            except ValueError:
                ids = []
        rows = []
        if ids:
            rows = (await db.execute(select(IngestRun).where(
                IngestRun.workspace_id == user.workspace_id, IngestRun.id.in_(ids))
                .order_by(IngestRun.created_at.desc()))).scalars().all()
    return {"items": [run_view(r, detail=True) for r in rows]}


@router.post("/ingestions")
async def create_ingestion(spec: IngestionSpec, user: CurrentUser = Depends(require_write)):
    if not spec.name.strip():
        raise HTTPException(status_code=400, detail="name required")
    cfg = ingestion_config(spec)
    maps = cfg.get("mappings") or []
    # primary connector/pipeline = the explicit single one, else the first mapping (back-compat columns)
    primary_conn = spec.connector_id or (maps[0]["connector_id"] if maps else None)
    primary_pipe = spec.pipeline_id or (maps[0]["pipeline_id"] if maps else None)
    if not primary_conn:
        raise HTTPException(status_code=400, detail="a connector (or at least one mapping) is required")
    async with V2Session() as db:
        ing = Ingestion(workspace_id=user.workspace_id, name=spec.name.strip(),
                        kind=sync.normalize_kind(spec.kind),
                        connector_id=safe_uuid(primary_conn, user.workspace_id),
                        pipeline_id=safe_uuid(primary_pipe, user.workspace_id),
                        config=cfg, schedule=spec.schedule)
        db.add(ing)
        await db.commit()
        await db.refresh(ing)
    return ingestion_view(ing)


@router.get("/ingestions/{ingestion_id}")
async def get_ingestion(ingestion_id: str, user: CurrentUser = Depends(require_auth)):
    async with V2Session() as db:
        ing = await db.get(Ingestion, safe_uuid(ingestion_id, user.workspace_id))
    if not ing or ing.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="ingestion not found")
    return ingestion_view(ing)


@router.put("/ingestions/{ingestion_id}")
async def update_ingestion(ingestion_id: str, patch: IngestionPatch,
                           user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        ing = await db.get(Ingestion, safe_uuid(ingestion_id, user.workspace_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="ingestion not found")
        if patch.name is not None and patch.name.strip():
            ing.name = patch.name.strip()
        if patch.pipeline_id is not None:
            ing.pipeline_id = safe_uuid(patch.pipeline_id, user.workspace_id)
        if patch.schedule is not None:
            ing.schedule = patch.schedule or None
        cfg = dict(ing.config or {})
        if patch.mode is not None:
            cfg["mode"] = sync.normalize_mode(patch.mode)
        if patch.paused is not None:
            cfg["paused"] = bool(patch.paused)
        if patch.mappings is not None:
            cfg["mappings"] = clean_mappings(patch.mappings)
            if cfg["mappings"]:                       # keep the back-compat columns pointing at mapping[0]
                ing.connector_id = safe_uuid(cfg["mappings"][0]["connector_id"], user.workspace_id)
                ing.pipeline_id = safe_uuid(cfg["mappings"][0]["pipeline_id"], user.workspace_id)
        if patch.selections is not None:
            cfg["selections"] = patch.selections
            for k in ("schema", "tables", "views"):       # drop legacy single-schema keys
                cfg.pop(k, None)
        if patch.directories is not None:
            cfg["directories"] = [d for d in patch.directories if d and str(d).strip()]
        if patch.directory_nodes is not None:
            cfg["directory_nodes"] = bool(patch.directory_nodes)
        if patch.formats is not None:
            cfg["formats"] = [f for f in patch.formats if f]
        if patch.enrich is not None:
            cfg["enrich"] = bool(patch.enrich)
        ing.config = cfg
        await db.commit()
        await db.refresh(ing)
    return ingestion_view(ing)


@router.delete("/ingestions/{ingestion_id}")
async def delete_ingestion(ingestion_id: str, user: CurrentUser = Depends(require_write)):
    async with V2Session() as db:
        res = await db.execute(delete(Ingestion).where(
            Ingestion.id == uuid.UUID(ingestion_id), Ingestion.workspace_id == user.workspace_id))
        await db.commit()
    return {"status": "ok", "deleted": res.rowcount or 0}


@router.post("/ingestions/{ingestion_id}/run")
async def run_ingestion(ingestion_id: str, user: CurrentUser = Depends(require_write)):
    """Manual trigger — anyone can fire a saved ingestion anytime. Dispatches ONE scoped worker task
    PER selected schema, so multi-schema ingestions run in PARALLEL across the Celery worker pool
    (parallelism = the worker's configured concurrency). Each schema gets its own Progress run.
    Credentials resolve in-worker from the connector; pruning is disabled (an ingestion is a scoped,
    additive job — it never deletes tables it isn't responsible for)."""
    import copy
    from datetime import datetime, timezone

    from opencortyx.ingest.pipelines.defaults import resolve_pipeline
    from opencortyx.tasks.ingest import v2_ingest_sql

    def _stages_for(pl, targets):
        stages = copy.deepcopy(pl.stages) if (pl and pl.stages) else [{"id": "stage_0", "action": "base_schema", "config": {}}]
        has_base = False
        for s in stages:
            if isinstance(s, dict) and s.get("action") == "base_schema":
                has_base = True
                if targets:
                    s.setdefault("config", {})["target_tables"] = targets
        if not has_base:
            stages = [{"id": "stage_0", "action": "base_schema",
                       "config": ({"target_tables": targets} if targets else {})}] + stages
        return stages

    from opencortyx.domain.control import Connector
    from opencortyx.ingest.sources import datalake, sync
    async with V2Session() as db:
        ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="ingestion not found")
        if (ing.config or {}).get("mode") == "one_time":
            raise HTTPException(
                status_code=400,
                detail="This was a one-time upload from your device — the files aren't on the server, "
                       "so it can't be re-run. Upload them again, or point an ingestion at a connector "
                       "folder to make it re-runnable.")
        if not ing.connector_id and not (ing.config or {}).get("mappings"):
            raise HTTPException(status_code=400, detail="ingestion has no connector")
        # resolve_pipeline, not db.get: a SYSTEM DEFAULT is virtual, so a bare row lookup returns
        # None and the run would silently drop the assigned pipeline's stages.
        pl = await resolve_pipeline(db, user.workspace_id, ing.pipeline_id) if ing.pipeline_id else None
        conn = await db.get(Connector, ing.connector_id) if ing.connector_id else None
        ckind = (conn.config or {}).get("kind") if conn else None
        norm_kind = sync.normalize_kind(ing.kind)
        is_file = (sync.is_file_sync(ing) or ckind in datalake.DOC_FORMATS
                   or ckind in datalake.LAKE_STORAGE)
        if is_file:
            rid = await create_run(user.workspace_id, norm_kind, ing.name)
            task = v2_sync_ingestion.delay(str(user.workspace_id), str(ing.id), str(rid) if rid else None)
            ing.status = "running"
            ing.last_run_id = str(rid) if rid else None
            ing.last_run_at = datetime.now(timezone.utc)
            await db.commit()
            return {"status": "dispatched", "runs": 1, "run_ids": [str(rid) if rid else None],
                    "task_ids": [task.id]}
        cfg = ing.config or {}
        enrich = bool(cfg.get("enrich", True))
        sels = selections_of(cfg) or [{"schema": None, "tables": [], "views": []}]  # none → all tables
        multi = len(sels) > 1
        run_ids, tasks = [], []
        for sel in sels:
            targets = list(sel.get("tables") or []) + list(sel.get("views") or [])
            stages = _stages_for(pl, targets)
            label = f"{ing.name} · {sel.get('schema') or 'default'}" if multi else ing.name
            rid = await create_run(user.workspace_id, "structured", label)
            task = v2_ingest_sql.delay(
                str(user.workspace_id), None, sel.get("schema"), None, enrich, stages,
                str(rid) if rid else None, str(ing.connector_id), False,   # prune=False (scoped)
                anchors=cfg.get("anchors"))
            run_ids.append(str(rid) if rid else None)
            tasks.append(task.id)
        ing.status = "running"
        ing.last_run_id = run_ids[0] if run_ids else None
        ing.last_run_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "dispatched", "runs": len(run_ids), "run_ids": run_ids, "task_ids": tasks}


@router.post("/ingestions/{ingestion_id}/sync")
async def sync_ingestion_ep(ingestion_id: str, user: CurrentUser = Depends(require_write)):
    """Manual change-detection SYNC for a document/lake ingestion: stat the source, diff against the
    stored manifest, and ingest only the delta (add/update) + soft-remove vanished files. This is the
    cheap counterpart to /run (which re-ingests everything). It's also what realtime/scheduled fire
    automatically. Structured (DB-table) ingestions have no file manifest — use /run for those."""
    from datetime import datetime, timezone

    async with V2Session() as db:
        ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="ingestion not found")
        if (ing.config or {}).get("mode") == "one_time":
            raise HTTPException(
                status_code=400,
                detail="This was a one-time upload from your device — the files aren't on the server, "
                       "so it can't be re-run. Upload them again, or point an ingestion at a connector "
                       "folder to make it re-runnable.")
        if not ing.connector_id and not (ing.config or {}).get("mappings"):
            raise HTTPException(status_code=400, detail="ingestion has no connector")
        kind = sync.normalize_kind(ing.kind)
        if not sync.is_file_sync(ing):
            raise HTTPException(status_code=400,
                                detail="sync is for file ingestions (unstructured, semi-structured, or structured watch-folders); use /run for a structured database connector")
        rid = await create_run(user.workspace_id, kind, f"{ing.name} · sync")
        task = v2_sync_ingestion.delay(str(user.workspace_id), str(ing.id), str(rid) if rid else None)
        ing.status = "running"
        ing.last_run_id = str(rid) if rid else None
        ing.last_run_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "dispatched", "runs": 1, "run_ids": [str(rid) if rid else None],
            "task_ids": [task.id]}


@router.post("/ingestions/{ingestion_id}/pause")
async def pause_ingestion_ep(ingestion_id: str, user: CurrentUser = Depends(require_write)):
    """Pause a scheduled/realtime ingestion. The beat tick will not auto-run it while paused. Its saved
    manifest is untouched, so resuming continues exactly where it left off (no re-ingest from scratch)."""
    async with V2Session() as db:
        ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="ingestion not found")
        cfg = dict(ing.config or {})
        cfg["paused"] = True
        ing.config = cfg
        if (ing.status or "") != "running":
            ing.status = "paused"
        await db.commit()
    return {"status": "paused", "id": ingestion_id}


@router.post("/ingestions/{ingestion_id}/resume")
async def resume_ingestion_ep(ingestion_id: str, user: CurrentUser = Depends(require_write)):
    """Resume a paused or failure-HIBERNATED ingestion. Clears the paused/hibernated flags and, for
    file kinds, immediately dispatches a change-detection sync that continues from the manifest —
    processing only what's still outstanding. Scheduled/realtime then keep ticking normally."""
    from datetime import datetime, timezone

    async with V2Session() as db:
        ing = await db.get(Ingestion, uuid.UUID(ingestion_id))
        if not ing or ing.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="ingestion not found")
        cfg = dict(ing.config or {})
        cfg["paused"] = False
        cfg.pop("hibernated", None)
        cfg.pop("hibernated_at", None)
        ing.config = cfg
        kind = sync.normalize_kind(ing.kind)
        dispatched = None
        if kind in sync.FILE_KINDS and (ing.connector_id or cfg.get("mappings")):
            rid = await create_run(user.workspace_id, kind, f"{ing.name} · resume")
            v2_sync_ingestion.delay(str(user.workspace_id), str(ing.id), str(rid) if rid else None)
            ing.status = "running"
            ing.last_run_id = str(rid) if rid else None
            ing.last_run_at = datetime.now(timezone.utc)
            dispatched = str(rid) if rid else None
        else:
            ing.status = "idle"
        await db.commit()
    return {"status": "resumed", "id": ingestion_id, "run_id": dispatched}
