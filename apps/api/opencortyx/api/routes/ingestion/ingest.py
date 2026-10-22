"""Routes: file and JSON uploads, supported formats and ingestion integrity."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from opencortyx.api.deps import guard_readable, guard_upload_size
from opencortyx.api.schemas.ingestion.ingest import JsonInlineSpec, JsonStreamSpec
from opencortyx.api.services.ingestion.ingest import parse_anchor_form
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.core import uploads
from opencortyx.ingest.runs.progress import batch_status, create_run, dispatch, record_device_upload
from opencortyx.tasks.ingest import v2_ingest_document_flow, v2_ingest_json

router = APIRouter()


async def _remember_tags(workspace_id, run_id, tags: Optional[str]) -> None:
    """Tags given with an upload wait on its run: the asset does not exist until the worker has read
    the file (see `knowledge.graph_tree.request_tags`)."""
    if tags and run_id:
        from opencortyx.knowledge.graph_tree import parse_tags, request_tags
        await request_tags(workspace_id, run_id, parse_tags(tags))


@router.post("/sources/upload")
async def upload_source(
    file: UploadFile = File(...),
    features: Optional[str] = Form(None),
    collection_id: Optional[str] = Form(None),
    batch_id: Optional[str] = Form(None),      # files dropped together → ONE saved ingestion
    pipeline_id: Optional[str] = Form(None),
    pipeline_ids: Optional[str] = Form(None),  # auto-detect: JSON {kind: pipeline id}, per detected kind
    route: Optional[str] = Form(None),         # force a pipeline; None → decide from the file
    max_rows: Optional[str] = Form(None),      # tabular: rows → nodes. a number, or "all"
    anchors: Optional[str] = Form(None),       # JSON list of anchor rules (see ingest/extraction/anchors.py)
    asset_path: Optional[str] = Form(None),    # folder-relative path the rules match on
    replace: bool = Form(False),               # read it again even if this exact file is already in
    tags: Optional[str] = Form(None),          # JSON list or comma-separated: the asset's tags
    user: CurrentUser = Depends(require_write)
):
    """NATIVE ingestion: upload a file and it is sent to the pipeline that SUITS IT.

    Routing (see `ingest.formats`): records (.json/.jsonl/.xml/.yaml/…) go to the semi-structured
    walker, tables (.csv/.parquet/.xlsx/…) to the structured/DuckDB path, and prose to the document
    chunker. This endpoint used to send everything to the document pipeline, which meant a 29 MB
    JSONL of 100k rows was chunked as if it were an essay and scheduled hundreds of thousands of
    per-chunk LLM calls. Pass `route` to override the decision.

    The file is stored once (core/uploads.py) and the job carries a reference to it, so a queued job
    stays small however large its file. Async. `collection_id` scopes everything extracted from the file to
    one collection, so retrieval can be scoped to it alone.

    `anchors` attaches the file to nodes that already exist and/or states what it IS
    (`aadhaar/** → Aadhaar`), matched against `asset_path` — the folder-relative path the browser
    reports for a folder upload, falling back to the bare filename. It applies to ALL THREE routes:
    a table and a JSON record set belong to an entity the same way a document does.
    """
    from opencortyx.ingest.formats import catalog as formats
    content = await file.read()
    guard_upload_size(content, file.filename)
    fname = file.filename or "upload"
    decision = guard_readable(content, fname, route)
    ref = uploads.put(content)
    chosen = decision["route"]
    if pipeline_ids:
        from opencortyx.ingest.sources import sync
        try:
            per_kind = sync.kind_pipelines(json.loads(pipeline_ids))
        except ValueError:
            raise HTTPException(status_code=400, detail="pipeline_ids must be a JSON object")
        pipeline_id = per_kind.get(sync.normalize_kind(chosen)) or pipeline_id
    # Validated HERE so a malformed rule is a 400 on the upload, not a per-file failure discovered
    # in a worker log. `_anchor_extras` is the blob every route below folds into its own options.
    _anchor_extras = parse_anchor_form(anchors, asset_path)

    if chosen == formats.SEMISTRUCTURED:
        run_id = await create_run(user.workspace_id, "semistructured", fname, batch_id=batch_id)
        await record_device_upload(user.workspace_id, "semistructured", fname, run_id, batch_id,
                                   pipeline_id or None)
        opts = {k: v for k, v in {"collection_id": collection_id,
                                  "pipeline_id": pipeline_id}.items() if v}
        opts.update(_anchor_extras)
        if replace:
            opts["replace"] = True
        task = await dispatch(run_id, v2_ingest_json, str(user.workspace_id), ref, fname,
                              json.dumps(opts) if opts else None, str(run_id) if run_id else None)
    elif chosen == formats.STRUCTURED:
        from opencortyx.tasks.ingest import v2_ingest_tabular
        run_id = await create_run(user.workspace_id, "structured", fname, batch_id=batch_id)
        await record_device_upload(user.workspace_id, "structured", fname, run_id, batch_id,
                                   pipeline_id or None)
        # `max_rows` rides on the overrides blob (the established channel for extras) and is
        # stripped there before the rest reaches DuckDB's parse settings.
        _ovd = dict(_anchor_extras)
        if max_rows is not None:
            _ovd["max_rows"] = max_rows
        _ov = json.dumps(_ovd) if _ovd else None
        task = await dispatch(run_id, v2_ingest_tabular, str(user.workspace_id), ref, fname,
                              pipeline_id or None, _ov, collection_id, str(run_id) if run_id else None)
    else:
        run_id = await create_run(user.workspace_id, "document", fname, batch_id=batch_id)
        await record_device_upload(user.workspace_id, "unstructured", fname, run_id, batch_id,
                                   pipeline_id or None)
        if pipeline_id:
            from opencortyx.ingest.pipelines.defaults import resolve_pipeline
            async with V2Session() as db:
                pl = await resolve_pipeline(db, user.workspace_id, pipeline_id)
            if pl is None:
                raise HTTPException(status_code=404,
                                    detail=f"pipeline {pipeline_id} not found for this workspace")
            task = await dispatch(
                run_id, v2_ingest_document_flow,
                str(user.workspace_id), ref, fname, file.content_type or "text/plain",
                json.dumps({"pipeline": pl.stages or [], **_anchor_extras,
                            **({"replace": True} if replace else {})}), collection_id,
                str(run_id) if run_id else None)
        else:
            from opencortyx.tasks.ingest import v2_ingest_document
            task = await dispatch(
                run_id, v2_ingest_document,
                str(user.workspace_id), ref, fname, file.content_type or "text/plain",
                collection_id, features, str(run_id) if run_id else None,
                None, _anchor_extras.get("anchors"),
                _anchor_extras.get("asset_path") or fname, replace=bool(replace))

    await _remember_tags(user.workspace_id, run_id, tags)

    # `routing` is returned so an unexpected route is visible at the call site rather than being
    # discovered later by wondering why a CSV produced Chunk nodes.
    return {"task_id": task.id, "status": "dispatched", "filename": file.filename,
            "run_id": str(run_id) if run_id else None, "routing": decision}


@router.post("/json/ingest")
async def ingest_json_source(
    file: UploadFile = File(...),
    options: Optional[str] = Form(None),     # JSON: {root_type, max_depth, max_array, max_records, embed_nodes}
    batch_id: Optional[str] = Form(None),    # files dropped together → ONE saved ingestion
    tags: Optional[str] = Form(None),        # JSON list or comma-separated: the asset's tags
    user: CurrentUser = Depends(require_write),
):
    """AUTO JSON ingestion (Mode B): upload a .json file; the worker walks its hierarchy into
    nodes/edges (objects→nodes, scalars→attributes, arrays→children + NEXT, parallel arrays zipped,
    ids deterministic, *_id values → REFERENCES). `options` is an optional JSON string of knobs."""
    content = await file.read()
    guard_upload_size(content, file.filename)
    fname = file.filename or "payload.json"
    run_id = await create_run(user.workspace_id, "semistructured", fname, batch_id=batch_id)
    pipe = None
    try:                                     # the semi-structured pipeline rides in `options`
        import json as _j
        pipe = (_j.loads(options) if options else {}).get("pipeline_id")
    except Exception:
        pipe = None
    await record_device_upload(user.workspace_id, "semistructured", fname, run_id, batch_id, pipe)
    task = await dispatch(run_id, v2_ingest_json, str(user.workspace_id), uploads.put(content), fname,
                          options or None, str(run_id) if run_id else None)
    await _remember_tags(user.workspace_id, run_id, tags)
    return {"task_id": task.id, "status": "dispatched", "filename": fname,
            "run_id": str(run_id) if run_id else None}


@router.post("/json/ingest_stream")
async def ingest_json_stream_ep(spec: JsonStreamSpec, user: CurrentUser = Depends(require_write)):
    """STREAM a LARGE local .json file (a top-level array of records) into the graph at CONSTANT memory
    (ijson) — the path for GB-scale files / streaming APIs. The file must live under the mounted host
    folder (LOCAL_FS_HOST_ROOT) and Settings → 'Allow local file access' must be ON. Nothing is uploaded:
    the worker reads the file directly and walks it record-by-record."""
    import json as _json

    from opencortyx.ingest.runs.progress import create_run
    from opencortyx.tasks.ingest import v2_ingest_json_stream
    opts = dict(spec.options or {})
    label = spec.source_label or spec.path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if spec.source_label:
        opts["source_label"] = spec.source_label
    run_id = await create_run(user.workspace_id, "semistructured", label)
    task = v2_ingest_json_stream.delay(str(user.workspace_id), spec.path, _json.dumps(opts),
                                       str(run_id) if run_id else None)
    return {"task_id": task.id, "status": "dispatched", "path": spec.path,
            "run_id": str(run_id) if run_id else None}


@router.post("/json/ingest_inline")
async def ingest_json_inline(spec: JsonInlineSpec, user: CurrentUser = Depends(require_write)):
    """Ingest JSON sent as a request BODY (the browser parses + slices the file by `max_records`, so a
    huge file never has to be uploaded whole). Same walker/options as /json/ingest."""
    import json as _json
    raw = _json.dumps(spec.data).encode("utf-8")
    guard_upload_size(raw, spec.source_label)            # clear 413 if the slice is still too big
    run_id = await create_run(user.workspace_id, "semistructured", spec.source_label,
                              batch_id=spec.batch_id)
    await record_device_upload(user.workspace_id, "semistructured", spec.source_label, run_id,
                               spec.batch_id, (spec.options or {}).get("pipeline_id"))
    task = await dispatch(run_id, v2_ingest_json, str(user.workspace_id), uploads.put(raw),
                          spec.source_label, _json.dumps(spec.options or {}),
                          str(run_id) if run_id else None)
    return {"task_id": task.id, "status": "dispatched", "filename": spec.source_label,
            "run_id": str(run_id) if run_id else None}


@router.post("/sources/upload_flow")
async def upload_source_flow(
    file: UploadFile = File(...),
    pipeline_config: Optional[str] = Form(None),
    batch_id: Optional[str] = Form(None),      # files dropped together → ONE saved ingestion
    tags: Optional[str] = Form(None),          # JSON list or comma-separated: the asset's tags
    user: CurrentUser = Depends(require_write)
):
    """NATIVE document ingestion (Modular Pipeline): upload a file and an explicit pipeline
    flow configuration JSON. The worker will execute the DAG described in pipeline_config."""
    content = await file.read()
    guard_upload_size(content, file.filename)
    fname = file.filename or "upload"
    run_id = await create_run(user.workspace_id, "document", fname, batch_id=batch_id)
    await record_device_upload(user.workspace_id, "unstructured", fname, run_id, batch_id)
    task = await dispatch(run_id, v2_ingest_document_flow, str(user.workspace_id), uploads.put(content),
                          fname, file.content_type or "text/plain", pipeline_config,
                          None, str(run_id) if run_id else None)
    await _remember_tags(user.workspace_id, run_id, tags)
    return {"task_id": task.id, "status": "dispatched", "filename": file.filename, "flow": True,
            "run_id": str(run_id) if run_id else None}


@router.get("/uploads/batches/{batch_id}")
async def upload_batch_status(batch_id: str, user: CurrentUser = Depends(require_auth)):
    """A batch of uploads at a glance: runs per state and the ones that failed, with why. What Add data
    polls for a multi-file drop, instead of asking about every file in it."""
    return await batch_status(user.workspace_id, batch_id[:80])


@router.post("/ingest/integrity/rebaseline")
async def ingest_integrity_rebaseline(user: CurrentUser = Depends(require_write),
                                      reason: str = "manual"):
    """Reset the write counters to agree with the graph as it stands now — the alarm's off switch.

    `row_delta` is cumulative, so once something has written the graph outside the funnel it stays
    off by that amount forever: fixing the cause does NOT silence the alarm. An alarm that cannot
    return to silence is one people learn to ignore, which is worse than not having it — so clearing
    it has to be a supported operation rather than a hand-written UPDATE.

    Use it after you have dealt with the cause (fixed a bypass, accepted a bulk load done outside
    NodeRepo). It does not hide anything: `baseline_reason` and `baseline_at` record that a reset
    happened and why, and every other process discards its pre-reset deltas rather than replaying
    them into the fresh totals."""
    from opencortyx.core.substrate import accounting as _acct
    try:
        return {"status": "rebaselined", **(await _acct.rebaseline(user.workspace_id, reason=reason))}
    except Exception as e:                     # noqa: BLE001 — pre-migration install
        raise HTTPException(status_code=503, detail=f"accounting unavailable: {e}")


@router.get("/ingest/integrity")
async def ingest_integrity(user: CurrentUser = Depends(require_auth)):
    """Prove nothing was written-and-forgotten: what the graph is missing, and why.

    "Zero data loss" is only meaningful if it can be checked, so this is the check. Every intended
    node/edge write that did not land is captured with its payload, which puts each one in exactly one
    of three states:

      recovered      — it failed once (usually an edge whose endpoint a later chunk created) and a
                       replay pass has since written it. Nothing is missing.
      pending        — captured and still queued for replay. Not in the graph YET, but not lost.
      unrecoverable  — it will never be valid (a self-edge) or has exhausted its retries. Still here,
                       still listed; the row is never deleted, because an unwritable edge is
                       information rather than something to hide.

    `unaccounted` is now MEASURED, not asserted. The write funnel counts every call and records its
    outcome in a `finally`, so a return path that produces neither a write nor a quarantine row counts
    itself — including one added later that nobody anticipated. A non-zero value means a code path is
    dropping data, and points at it."""
    from sqlalchemy import text as _text

    from opencortyx.core.substrate import accounting as _acct
    from opencortyx.core.config import V2_SCHEMA
    from opencortyx.core.substrate.quarantine import summary
    try:
        s = await summary(user.workspace_id)
    except Exception:                          # noqa: BLE001 — pre-migration install
        return {"available": False, "unaccounted": 0, "pending": 0,
                "recovered": 0, "unrecoverable": 0}
    acct = await _acct.report(user.workspace_id)
    s["writes"] = acct
    s["unaccounted"] = acct.get("unaccounted", 0) if acct.get("available") else 0
    async with V2Session() as db:
        runs = (await db.execute(_text(f"""
            SELECT status, count(*) FROM {V2_SCHEMA}.ingest_run
            WHERE workspace_id = :o AND status IN ('failed', 'running', 'queued')
            GROUP BY status"""), {"o": user.workspace_id})).all()
    by_status = {r[0]: r[1] for r in runs}
    s["incomplete_assets"] = {
        "failed": by_status.get("failed", 0),        # re-runnable; re-ingestion is idempotent
        "in_progress": by_status.get("running", 0) + by_status.get("queued", 0),
    }
    s["available"] = True
    return s


@router.get("/ingest/formats")
async def ingest_formats(user: CurrentUser = Depends(require_auth)):
    """Every file type ingestion accepts, grouped by the pipeline that will handle it.

    Served from `ingest.formats.FORMATS` — the SAME registry that routes an upload — so the list the
    user reads and the list the code obeys cannot drift. Hardcoding it in the UI would mean adding a
    format in one place and telling people about it in another."""
    from opencortyx.ingest.formats import catalog as _fmt
    groups: dict = {r: [] for r in _fmt.ROUTES}
    for ext, f in sorted(_fmt.FORMATS.items()):
        groups[f.route].append({"ext": ext, "name": f.name, "decoder": f.decoder or None})
    meta = {
        _fmt.DOCUMENT: {"label": "Unstructured · documents",
                        "reads": "Parsed → split into chunks → extractors run per chunk."},
        _fmt.SEMISTRUCTURED: {"label": "Semi-structured · records",
                              "reads": "Walked object-by-object into nodes and edges. No chunking."},
        _fmt.STRUCTURED: {"label": "Structured · tables",
                          "reads": "Read as tables — a catalogue, or one node per row."},
    }
    return {"routes": [{"route": r, **meta.get(r, {}), "count": len(groups[r]),
                        "accept": ",".join("." + f["ext"] for f in groups[r]),
                        "formats": groups[r]} for r in _fmt.ROUTES],
            "total": len(_fmt.FORMATS)}
