"""Routes: structured sources: SQL databases and table files."""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from opencortyx.api.deps import guard_upload_size
from opencortyx.api.schemas.ingestion.structured import (
    ConnectorFilesIngestSpec,
    DomainSpec,
    SqlIngestSpec,
)
from opencortyx.api.services.ingestion.structured import asset_view
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.domain.ontology import ASSET_TYPES
from opencortyx.core.substrate import NodeRepo
from opencortyx.domain.control import Connector
from opencortyx.ingest.runs.progress import create_run
from opencortyx.core.security.secrets import resolve_connector_dsn

router = APIRouter()


@router.post("/structured/introspect-sql")
async def introspect_sql(spec: SqlIngestSpec, user: CurrentUser = Depends(require_auth)):
    """Live-introspect a SQL source so the UI mapper shows the REAL tables/columns (not a mock).

    Runs the same introspection the ingest worker uses, in a thread so the event loop isn't
    blocked. A missing/unreachable DSN returns a clean 400 (handled error), never a 500 crash."""
    import anyio

    from opencortyx.ingest.structured.ingest import _sync_introspect

    dsn, schema = spec.connection_string, spec.schema_name
    if spec.connector_id:
        async with V2Session() as db:
            c = await db.get(Connector, uuid.UUID(spec.connector_id))
        if not c or c.workspace_id != user.workspace_id:
            raise HTTPException(status_code=404, detail="connector not found")
        dsn = resolve_connector_dsn(c.config or {})
        schema = schema or (c.config or {}).get("schema")
    if not dsn:
        raise HTTPException(status_code=400, detail="connection_string or connector_id required")

    try:
        raw = await anyio.to_thread.run_sync(lambda: _sync_introspect(dsn, schema))
    except Exception as e:   # bad DSN / driver / auth → handled error, not a server crash
        raise HTTPException(status_code=400, detail=f"introspection failed: {e}")

    return {
        "tables": [{
            "name": t["table"],
            "schema": t.get("schema"),
            "row_count": t.get("row_count"),
            "columns": [{"name": c["name"], "type": c.get("type"),
                         "primary_key": c.get("primary_key"),
                         "foreign_key": c.get("foreign_key"),
                         "references_table": c.get("references_table"),
                         "references_column": c.get("references_column"),
                         "nullable": c.get("nullable")}
                        for c in (t.get("columns") or [])],
        } for t in (raw or [])]
    }


@router.post("/structured/ingest-sql")
async def ingest_sql(spec: SqlIngestSpec, user: CurrentUser = Depends(require_write)):
    """NATIVE structured ingestion: point v2 at a SQL source (raw DSN or a saved connector_id);
    it introspects + profiles + LLM-enriches and writes Table/Attribute nodes and edges + derived business
    nodes straight into v2 (no v1 tables). Async."""
    dsn, schema, source_name = spec.connection_string, spec.schema_name, None
    if spec.connector_id:
        async with V2Session() as db:
            c = await db.get(Connector, uuid.UUID(spec.connector_id))
        if not c or c.workspace_id != user.workspace_id:
            return {"status": "connector_not_found"}
        # The worker resolves the connector's credentials itself; a resolved DSN here would carry the
        # password through the message broker, which is what `connector_id` exists to prevent.
        dsn = None
        schema = schema or (c.config or {}).get("schema")
        source_name = c.name
    elif not dsn:
        return {"status": "error", "detail": "connection_string or connector_id required"}
    pipeline = spec.pipeline
    if not pipeline and spec.table_mappings:
        # `table_mappings` ({table: mapping id}) was accepted and dropped. It binds structured
        # mappings, which the ingest reads from a base_schema stage — so say it that way.
        ids = sorted({str(v) for v in spec.table_mappings.values() if v})
        pipeline = [{"id": "stage_0", "type": "base_schema", "action": "base_schema",
                     "config": {"mapping_ids": ids}}]
    from opencortyx.tasks.ingest import v2_ingest_sql
    # The run is named after what was ingested — the saved connection — not a generic "SQL source".
    asset = " · ".join(x for x in (source_name, schema) if x) or "SQL source"
    run_id = await create_run(user.workspace_id, "structured", asset)
    task = v2_ingest_sql.delay(str(user.workspace_id), dsn, schema, None, spec.enrich, pipeline,
                               str(run_id) if run_id else None, connector_id=spec.connector_id)
    return {"task_id": task.id, "status": "dispatched", "run_id": str(run_id) if run_id else None}


@router.post("/structured/preview-file")
async def preview_file_ep(
    file: UploadFile = File(...),
    overrides: Optional[str] = Form(None),
    user: CurrentUser = Depends(require_auth),
):
    """Auto-detect a tabular file's shape via DuckDB (delimiter / encoding / header sniffed) and return
    its columns + a sample WITHOUT ingesting. Powers the Ingestion Hub's File-format panel — the user
    confirms a preview rather than configuring blind. `overrides` (JSON) forces specific parse
    settings for the rare file the sniffer gets wrong."""
    from opencortyx.ingest.formats.structured import tabular as T
    if not T.is_tabular(file.filename or ""):
        return {"status": "unsupported", "detail": "Not a CSV / TSV / Parquet / Excel file."}
    content = await file.read()
    guard_upload_size(content, file.filename)
    import json as _json
    import os
    import tempfile

    import anyio
    # `json` is not imported at module scope — this line used to raise NameError, which meant EVERY
    # attempt to force parse settings (delimiter, header, encoding) 500'd and only auto-detect worked.
    ov = _json.loads(overrides) if overrides else None
    suffix = os.path.splitext(file.filename or "")[1] or ".csv"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    with open(tmp, "wb") as fh:
        fh.write(content)
    try:
        pv = await anyio.to_thread.run_sync(lambda: T.preview_file(tmp, file.filename or "file", ov))
        return {"status": "ok", **pv}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


@router.post("/structured/ingest-file")
async def ingest_file_ep(
    file: UploadFile = File(...),
    pipeline_id: Optional[str] = Form(None),
    overrides: Optional[str] = Form(None),
    collection_id: Optional[str] = Form(None),
    batch_id: Optional[str] = Form(None),          # files picked together → ONE saved ingestion
    user: CurrentUser = Depends(require_write),
):
    """Ingest a tabular file (CSV / TSV / Parquet / Excel) through the STRUCTURED pipeline via DuckDB.
    The pipeline's Step-1 config decides catalog vs one-node-per-row. `overrides` (JSON) carries manual
    parse settings; None → auto-detect. Async — the worker reads the stored file (core/uploads.py)."""
    from opencortyx.ingest.formats.structured import tabular as T
    if not T.is_tabular(file.filename or ""):
        return {"status": "unsupported", "detail": "Not a CSV / TSV / Parquet / Excel file."}
    content = await file.read()
    guard_upload_size(content, file.filename)
    from opencortyx.core import uploads
    from opencortyx.ingest.runs.progress import create_run, dispatch, record_device_upload
    from opencortyx.tasks.ingest import v2_ingest_tabular
    run_id = await create_run(user.workspace_id, "structured", file.filename or "tabular file",
                              batch_id=batch_id)
    await record_device_upload(user.workspace_id, "structured", file.filename or "tabular file",
                               run_id, batch_id, pipeline_id or None)
    task = await dispatch(
        run_id, v2_ingest_tabular,
        str(user.workspace_id), uploads.put(content), file.filename or "upload",
        pipeline_id or None, overrides or None, collection_id or None,
        str(run_id) if run_id else None)
    return {"task_id": task.id, "status": "dispatched", "filename": file.filename,
            "run_id": str(run_id) if run_id else None}


@router.post("/structured/ingest-connector-files")
async def ingest_connector_files(spec: ConnectorFilesIngestSpec, user: CurrentUser = Depends(require_write)):
    """Ingest tabular files that live in a file/lake connector (server-side, streamed by DuckDB —
    never uploaded to the browser) through the structured pipeline. Accepts explicit file `paths`
    and/or `directories` (each walked recursively). Async."""
    async with V2Session() as db:
        c = await db.get(Connector, uuid.UUID(spec.connector_id))
    if not c or c.workspace_id != user.workspace_id:
        return {"status": "connector_not_found"}
    paths = [p for p in (spec.paths or []) if str(p).strip()]
    dirs = [d for d in (spec.directories or []) if str(d).strip()]
    if not paths and not dirs:
        return {"status": "error", "detail": "no files or folders selected"}
    from opencortyx.tasks.ingest import v2_ingest_connector_tabular
    if spec.anchors:                               # compile now → a bad glob is a 400, not a log line
        from opencortyx.ingest.extraction import anchors as _anchors
        try:
            _anchors.parse_rules(spec.anchors)
        except _anchors.AnchorRuleError as e:
            raise HTTPException(status_code=400, detail=str(e))
    label = f"{len(paths) or len(dirs)} {'file' if paths else 'folder'}(s) — {c.name}"
    run_id = await create_run(user.workspace_id, "structured", label)
    task = v2_ingest_connector_tabular.delay(
        str(user.workspace_id), spec.connector_id, paths, spec.pipeline_id or None,
        spec.collection_id or None, str(run_id) if run_id else None, dirs,
        spec.anchors or None)
    return {"task_id": task.id, "status": "dispatched", "run_id": str(run_id) if run_id else None}


@router.get("/structured/extractors")
async def structured_extractor_attrs(user: CurrentUser = Depends(require_auth)):
    """Per-extractor OPTIONAL attribute catalog → the Structured Pipeline builder renders these as
    enable/disable toggles per stage (config.attributes = enabled keys). Built-in code generators
    come from STRUCT_EXTRACTOR_ATTRS; EVERY template (built-in + custom) has its attributes DERIVED
    from its `emit` (not hardcoded) and keyed by template name — so custom extractors get toggles too."""
    from opencortyx.domain.control import ExtractionTemplate
    from opencortyx.ingest.extraction.templates import template_attr_catalog
    from opencortyx.ingest.structured.ingest import STRUCT_EXTRACTOR_ATTRS
    out = dict(STRUCT_EXTRACTOR_ATTRS)
    async with V2Session() as db:
        tmpls = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == user.workspace_id))).scalars().all()
    for t in tmpls:
        attrs = template_attr_catalog(t)
        if attrs:
            out[t.name] = attrs          # keyed by template name = the stage's `action` for custom extractors
    return {"attributes": out}


@router.post("/structured/seed-domain")
async def seed_domain_ep(spec: DomainSpec, user: CurrentUser = Depends(require_write)):
    """LLM-populate a full catalogue (tables/columns/FKs/synonyms + all derived nodes) from a domain
    name, no database connected. Great for bootstrapping or demos."""
    from opencortyx.tasks.enrich import v2_seed_domain
    task = v2_seed_domain.delay(str(user.workspace_id), spec.domain, spec.n_tables)
    return {"task_id": task.id, "status": "dispatched"}


@router.post("/structured/enrich-native")
async def structured_enrich_native(user: CurrentUser = Depends(require_write)):
    """Re-generate derived business nodes (segments/computations/reports/…) from the v2 catalogue,
    no v1. Run after native SQL ingestion or any time to refresh. Async."""
    from opencortyx.tasks.enrich import v2_structured_enrich_native
    task = v2_structured_enrich_native.delay(str(user.workspace_id))
    return {"task_id": task.id, "status": "dispatched"}


@router.get("/structured")
async def list_structured(user: CurrentUser = Depends(require_auth), limit: int = 500):
    """List the ingested assets: tables and documents, including assets an anchor rule retyped (an
    `Aadhaar` rather than a generic `Source`), matched by the type they were ingested as."""
    repo = NodeRepo()
    rows = await repo.find(user.workspace_id, types=list(ASSET_TYPES), order_by="-created_at", limit=limit)
    total = await repo.count(user.workspace_id, types=list(ASSET_TYPES))
    return {"total": total, "items": [asset_view(n) for n in rows]}
