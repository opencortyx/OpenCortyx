"""Ingestion tasks, one per source kind: documents, JSON, tables, SQL, lake files."""
import json
import uuid
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core import uploads
from opencortyx.core.observability import get_logger
from opencortyx.bootstrap import init_v2_schema
from opencortyx.capabilities.modules.contradiction import sweep
from opencortyx.domain.control import Connector
from opencortyx.core.db import V2Session
from opencortyx.ontology.extractors import seed_builtin_templates
from opencortyx.core.substrate import NodeRepo
from opencortyx.ingest.sources import datalake
from opencortyx.ingest.pipelines.defaults import resolve_pipeline
from opencortyx.ingest.extraction.templates import run_relationship_templates_for_source
from opencortyx.ingest.walkers import walker_for
from opencortyx.tasks._shared import _anchor_opts, _apply_anchors, _apply_anchors_sql, _close_ingestion, _doc_plan_from_features, _merge_mapping_file_settings, _post_ingest, _reporter, _roll_up_anchors
from opencortyx.tasks.graph import _queue_embed_pass
from opencortyx.tasks.graph_tree import schedule_tree_index

log = get_logger(__name__)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_document")
def v2_ingest_document(self, workspace_id: str, content_b64: str, filename: str,
                       mime_type: str = "text/plain", collection_id: str = None, features: str = None,
                       run_id: str = None, defer_extraction: bool = None,
                       anchors: list = None, asset_path: str = None, replace: bool = False):
    """Native document ingestion: parse + chunk a file → Source/Chunk nodes → extract + enrich.
    `features` (csv from the upload modal) selects which stages run. `content_b64` is the stored
    file's reference (core/uploads.py; base64 from older versions still reads). `run_id` drives the
    Progress tab.

    `defer_extraction` (None → the `defer_extraction` runtime setting) commits the chunks and
    embeddings here, then hands the LLM extraction to the `extraction` queue. The document is
    searchable as soon as this task returns; the graph fills in behind it. This is what stops one
    large file from holding every worker slot while small uploads wait."""
    async def _run():
        await init_v2_schema()
        content = uploads.load(content_b64)
        coll = uuid.UUID(collection_id) if collection_id else None
        rep = _reporter(run_id)
        try:
            from opencortyx.core.config import runtime as _rc
            _defer = _rc.defer_extraction() if defer_extraction is None else bool(defer_extraction)
            res = await walker_for("unstructured").ingest_file(
                uuid.UUID(workspace_id), content, filename, collection_id=coll, mime_type=mime_type,
                plan=_doc_plan_from_features(features), reporter=rep, defer_extraction=_defer,
                replace=bool(replace))
            await _apply_anchors(workspace_id, res, anchors, asset_path or filename, coll)
            if res.get("extraction") == "deferred":
                # Queue the expensive half and return. Reported as its own task id so the caller can
                # follow it; the run is marked complete for INGESTION, which is what it is.
                job = v2_extract_document.delay(workspace_id, str(res.get("source_id")), features,
                                                collection_id, run_id)
                res["extraction_task_id"] = job.id
                _queue_embed_pass(workspace_id, res)
                if rep:
                    await rep.finish("completed", result=res)
                return res
            await _post_ingest(workspace_id, res, "document")
            # Relationship extraction: connect the entities this doc just produced (relate.* templates),
            # scoped to this source's chunks so edges appear automatically without a manual Run.
            try:
                _sid = res.get("source_id")
                if _sid:
                    rel = await run_relationship_templates_for_source(
                        uuid.UUID(workspace_id), uuid.UUID(_sid), coll)
                    res["relationships_built"] = rel.get("edges", 0)
            except Exception as re_:
                log.warning("extraction.relationships_failed", error=str(re_))
            _queue_embed_pass(workspace_id, res)
            if rep:
                await rep.finish("completed", result=res)
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


def _queue_extract_pass(workspace_id: str, res, features: str = None,
                        collection_id: str = None, run_id: str = None) -> None:
    """Queue the deferred extractor stages for an ingest that stopped after committing its graph.

    Uniform across all three ingestion kinds: a document, a walked JSON payload and a mapped table
    all reach the point where their nodes exist and only the LLM work is left, and none of them
    should hold an ingestion slot while that runs. No-op unless the result says it deferred.
    """
    try:
        if not isinstance(res, dict) or res.get("extraction") != "deferred":
            return
        sid = res.get("source_id")
        if not sid:
            return
        job = v2_extract_document.delay(str(workspace_id), str(sid), features, collection_id, run_id)
        res["extraction_task_id"] = job.id
    except Exception as e:                                    # noqa: BLE001 — never fail an ingest
        log.warning("extraction.enqueue_failed", error=str(e))        # whose graph already committed


@celery_app.task(bind=True, queue="extraction", name="opencortyx.extract_document",
                 acks_late=True, max_retries=2)
def v2_extract_document(self, workspace_id: str, source_id: str, features: str = None,
                        collection_id: str = None, run_id: str = None):
    """The DEFERRED half of document ingestion: LLM extraction over chunks that already exist.

    Runs on the `extraction` queue so it competes only with other extraction, never with new
    uploads. Idempotent by construction — extraction upserts by deterministic id, so a retry after a
    worker restart re-derives the same nodes rather than duplicating them, which is why `acks_late`
    is safe here.
    """
    async def _run():
        from opencortyx.ingest.unstructured.ingest import extract_over_source
        await init_v2_schema()
        oid, sid = uuid.UUID(workspace_id), uuid.UUID(source_id)
        coll = uuid.UUID(collection_id) if collection_id else None
        if await NodeRepo().get(oid, sid) is None:
            return {"status": "gone", "source_id": source_id,
                    "reason": "source no longer exists (deleted or purged before extraction ran)"}
        # The built-in extractors are rows, seeded lazily. On a fresh org nothing else on the
        # document path has seeded them, and extraction would find none and report 0 entities.
        async with V2Session() as db:
            await seed_builtin_templates(db, oid)
        await mark_deferred_extraction(run_id, "running")
        res = await extract_over_source(oid, sid, plan=_doc_plan_from_features(features),
                                        collection_id=coll)
        await _post_ingest(workspace_id, {**res, "source_id": source_id}, "document")
        try:
            rel = await run_relationship_templates_for_source(oid, sid, coll)
            res["relationships_built"] = rel.get("edges", 0)
        except Exception as e:                                # noqa: BLE001
            res["relationships_error"] = str(e)[:200]
        schedule_tree_index(workspace_id)
        await mark_deferred_extraction(run_id, "done", counts=res)
        return {"status": "ok", "source_id": source_id, **res}

    from opencortyx.ingest.runs.progress import mark_deferred_extraction
    try:
        return async_to_sync(_run)()
    except Exception as exc:
        if self.request.retries >= 2:                  # out of retries: the run says so, not "pending"
            async_to_sync(mark_deferred_extraction)(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            raise
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="extraction", name="opencortyx.run_document_pipeline",
                 acks_late=True)
def v2_run_document_pipeline(self, workspace_id: str, source_id: str, stages_json: str, chunks: int,
                             collection_id: str = None, save_chunks: bool = True,
                             node_type: str = "Chunk", run_id: str = None):
    """The DEFERRED half of a pipeline document ingest: its stages over the committed chunks.

    On the extraction queue, beside the plain path's deferred extraction, so LLM stages never hold
    the ingest worker. Reports each stage on the run that parsed the file."""
    async def _run():
        import json
        from opencortyx.ingest.unstructured.ingest import run_pipeline_over_source
        from opencortyx.ingest.runs.progress import ProgressReporter, mark_deferred_extraction
        await init_v2_schema()
        oid, sid = uuid.UUID(workspace_id), uuid.UUID(source_id)
        if await NodeRepo().get(oid, sid) is None:
            return {"status": "gone", "source_id": source_id}
        async with V2Session() as db:
            await seed_builtin_templates(db, oid)
        rep = ProgressReporter(run_id) if run_id else None
        if rep:
            await rep.resume()
        await mark_deferred_extraction(run_id, "running", step=None)
        dag = await run_pipeline_over_source(
            oid, sid, json.loads(stages_json or "[]"), chunks,
            collection_id=uuid.UUID(collection_id) if collection_id else None,
            reporter=rep, save_chunks=save_chunks, node_type=node_type)
        await _post_ingest(workspace_id, {**dag, "source_id": source_id}, "document")
        schedule_tree_index(workspace_id)
        await mark_deferred_extraction(run_id, "done", counts=dag, step=None)
        return {"status": "ok", "source_id": source_id, **dag}

    from opencortyx.ingest.runs.progress import mark_deferred_extraction
    try:
        return async_to_sync(_run)()
    except Exception as exc:
        if self.request.retries >= 2:
            async_to_sync(mark_deferred_extraction)(run_id, "failed", error=f"{type(exc).__name__}: {exc}",
                                                    step=None)
            raise
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_json")
def v2_ingest_json(self, workspace_id: str, content_b64: str, filename: str,
                   opts_json: str = None, run_id: str = None):
    """JSON ingestion. Two modes, selected by `opts.pipeline_id`:
      • plain (Mode B): walk the JSON into a hierarchical node/edge graph (ingest_json).
      • SEMI-STRUCTURED pipeline: walk the structure AND read the text — match each record to the
        pipeline's structure cards, emit Chunks from text-role keys, run the pipeline's extractor
        stages over them (ingest.semistructured.run_semistructured).
    `opts_json` carries root_type / max_depth / max_array / embed_nodes / pipeline_id / collection_id."""
    async def _run():
        import json
        from opencortyx.ingest.formats.semistructured import ParseError
        await init_v2_schema()
        rep = _reporter(run_id)
        try:
            raw = uploads.load(content_b64)
            opts = json.loads(opts_json) if opts_json else {}
            coll = uuid.UUID(opts["collection_id"]) if opts.get("collection_id") else None
            _rules, _apath = _anchor_opts(opts, filename)
            pipeline_id = opts.get("pipeline_id")
            mapping_ids = [str(x) for x in (opts.get("mapping_ids") or []) if x]

            # A named pipeline, or mappings to verify against, reads the records through structure
            # cards and extractor steps; otherwise the records are walked into a plain graph.
            stages = None
            if pipeline_id or mapping_ids:
                async with V2Session() as db:
                    await seed_builtin_templates(db, uuid.UUID(workspace_id))
                    p = await resolve_pipeline(db, uuid.UUID(workspace_id), pipeline_id) if pipeline_id else None
                if pipeline_id and not p:
                    raise RuntimeError(f"pipeline {pipeline_id} not found for this org — refusing to "
                                       f"ingest without the assigned pipeline's structures/stages")
                stages = (list(p.stages or []) if p else
                          [{"id": "structures", "type": "json_structures", "source": "document",
                            "config": {"mapping_ids": mapping_ids, "auto_accept": False}}])
            elif rep:
                await rep.start(1)
                await rep.step(1, "Walk JSON → graph", "running")

            try:
                res = await walker_for("semistructured").ingest_file(
                    uuid.UUID(workspace_id), raw, filename, pipeline=stages, pipeline_id=pipeline_id,
                    collection_id=coll, reporter=rep, options=opts)
            except ParseError as e:
                # A malformed file is a reported failure: the decoder names the line and column.
                if rep:
                    await rep.finish("failed", error=e)
                return {"status": "error", "reason": str(e), "filename": filename}

            summary = {k: v for k, v in res.items() if isinstance(v, (int, str, bool))}
            if res.get("duplicate"):
                if rep:
                    await rep.step(1, "Duplicate — identical file already ingested", "completed")
                    await rep.set_source(res.get("source_id"))
                    await rep.finish("duplicate", result=summary)
                return res
            await _apply_anchors(workspace_id, res, _rules, _apath, coll)
            _queue_embed_pass(workspace_id, res)
            _queue_extract_pass(workspace_id, res)
            await _post_ingest(workspace_id, res, "semistructured")
            if rep:
                if stages is None:
                    await rep.step(1, "Walk JSON → graph", "completed", count=res.get("nodes"))
                # attribute the run to the asset it produced, so deleting the asset removes the run too
                await rep.set_source(res.get("source_id"))
                await rep.finish("completed", result=summary)
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_json_stream")
def v2_ingest_json_stream(self, workspace_id: str, host_path: str, opts_json: str = None, run_id: str = None):
    """STREAMING JSON ingestion from a file on the mounted host filesystem — for GB-scale files / streaming
    APIs. The file (a top-level array of records) is iterated with ijson at CONSTANT memory (never loaded
    whole) and walked record-by-record. Nothing is uploaded through the API/broker; the worker reads the
    file directly. Gated by Settings → 'Allow local file access'. `opts_json` carries collection_id /
    root_type / max_* / resolve_refs / source_label / pipeline_id.

    Two modes, selected by `opts.pipeline_id`:
      • plain: walk the JSON into a hierarchical node/edge graph (ingest_json_stream).
      • SEMI-STRUCTURED pipeline: load the file, walk+match+extract through the pipeline stages
        (same as v2_ingest_json's semistructured path) including temporal normalization."""
    async def _run():
        import json
        import os as _os
        from opencortyx.core.config import runtime as rc
        from opencortyx.ingest.sources.datalake import translate_local_path
        await init_v2_schema()
        rep = _reporter(run_id)
        try:
            if not rc.allow_local_files():
                raise RuntimeError("Local file access is disabled. Enable 'Allow local file access' in "
                                   "Settings to stream files from the mounted host folder.")
            opts = json.loads(opts_json) if opts_json else {}
            path = translate_local_path(host_path)
            if not _os.path.exists(path):
                raise FileNotFoundError(f"{host_path} (resolved to {path}) not found — is its folder under "
                                        f"LOCAL_FS_HOST_ROOT and mounted into the worker?")
            coll = uuid.UUID(opts["collection_id"]) if opts.get("collection_id") else None
            label = opts.get("source_label") or _os.path.basename(path)
            _rules, _apath = _anchor_opts(opts, host_path or path)

            pipeline_id = opts.get("pipeline_id")
            if pipeline_id:                            # semi-structured pipeline path
                from opencortyx.ingest.semistructured import run_semistructured
                async with V2Session() as db:
                    p = await resolve_pipeline(db, uuid.UUID(workspace_id), pipeline_id)
                    await seed_builtin_templates(db, uuid.UUID(workspace_id))
                if not p:
                    raise RuntimeError(f"pipeline {pipeline_id} not found for this org — refusing to "
                                       f"ingest without the assigned pipeline's structures/stages")
                stages = list(p.stages or [])
                # semistructured needs the full parsed JSON for structure matching + extraction
                with open(path, "rb") as fo:
                    data = json.load(fo)
                res = await run_semistructured(
                    uuid.UUID(workspace_id), data, pipeline_stages=stages, source_label=label,
                    collection_id=coll, pipeline_id=pipeline_id, options=opts, reporter=rep)
                await _apply_anchors(workspace_id, res, _rules, _apath, coll)
                _queue_embed_pass(workspace_id, res)
                _queue_extract_pass(workspace_id, res)
                await _post_ingest(workspace_id, res, "semistructured")
                if rep:
                    # attribute the run to the asset it produced (so deleting that asset takes its
                    # progress record with it instead of leaving a phantom)
                    await rep.set_source(res.get("source_id"))
                    await rep.finish("completed", result={k: v for k, v in res.items()
                                                          if isinstance(v, (int, str, bool))})
                return res

            # plain streaming path — no pipeline, just walk
            from opencortyx.ingest.semistructured.ingest import ingest_json_stream
            if rep:
                await rep.start(1)
                await rep.step(1, "Stream JSON → graph", "running")
            res = await ingest_json_stream(
                uuid.UUID(workspace_id), path, source_label=label, collection_id=coll,
                root_type=opts.get("root_type"), max_depth=int(opts.get("max_depth", 6)),
                max_array=int(opts.get("max_array", 500)), max_records=int(opts.get("max_records", 10**9)),
                embed_nodes=bool(opts.get("embed_nodes", False)),
                resolve_refs=bool(opts.get("resolve_refs", False)), reporter=rep)
            await _apply_anchors(workspace_id, res, _rules, _apath, coll)
            _queue_embed_pass(workspace_id, res)
            _queue_extract_pass(workspace_id, res)
            await _post_ingest(workspace_id, res, "semistructured")
            if rep:
                await rep.finish("completed", result={k: v for k, v in res.items()
                                                      if isinstance(v, (int, str, bool))})
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=1)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_document_flow")
def v2_ingest_document_flow(self, workspace_id: str, content_b64: str, filename: str,
                            mime_type: str = "text/plain", pipeline_config: str = None,
                            collection_id: str = None, run_id: str = None):
    """Native document ingestion via the Modular Pipeline Builder.

    `pipeline_config` is a JSON DAG of stages, each with a `type`, an Input `source` (a previous
    stage's id or 'document'), and `config`. We pass the raw stages to the DAG executor so stages
    chain on top of each other (chunks → summary → roll-up, keywords over summaries, …).
    `run_id` drives the Progress tab."""
    async def _run():
        import json
        await init_v2_schema()
        async with V2Session() as db:
            await seed_builtin_templates(db, uuid.UUID(workspace_id))
        content = uploads.load(content_b64)
        coll = uuid.UUID(collection_id) if collection_id else None
        try:
            cfg = json.loads(pipeline_config) if pipeline_config else {}
        except Exception:
            cfg = {}
        stages = cfg.get("pipeline") if isinstance(cfg, dict) else None
        # A pipeline-driven document is anchored exactly like a plain one — the rules ride in the
        # same config blob the DAG does, so assigning a pipeline never silently drops them.
        _rules, _apath = _anchor_opts(cfg if isinstance(cfg, dict) else {}, filename)
        rep = _reporter(run_id)
        from opencortyx.core.config import runtime as _rc
        try:
            # Parse + chunk here; the pipeline's stages (LLM summaries, extractors) go to the
            # extraction queue. Run inline, one large document held the only ingest worker for
            # hours and every upload behind it sat "waiting for a worker".
            res = await walker_for("unstructured").ingest_file(
                uuid.UUID(workspace_id), content, filename, collection_id=coll, mime_type=mime_type,
                pipeline=stages or None, reporter=rep,
                defer_extraction=bool(stages) and _rc.defer_extraction(),
                replace=bool(cfg.get("replace")) if isinstance(cfg, dict) else False)
            await _apply_anchors(workspace_id, res, _rules, _apath, coll)
            await _post_ingest(workspace_id, res, "document")
            if res.get("extraction") == "deferred" and stages:
                job = v2_run_document_pipeline.delay(
                    workspace_id, str(res.get("source_id")), json.dumps(stages), int(res.get("chunks") or 0),
                    collection_id, bool(res.get("save_chunks", True)),
                    str(res.get("node_type") or "Chunk"), run_id)
                res["extraction_task_id"] = job.id
                _queue_embed_pass(workspace_id, res)
            if rep:
                await rep.finish("completed", result=res)
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_tabular")
def v2_ingest_tabular(self, workspace_id: str, content_b64: str, filename: str,
                      pipeline_id: str = None, overrides_json: str = None,
                      collection_id: str = None, run_id: str = None):
    """Native TABULAR-FILE ingestion (CSV / TSV / Parquet / Excel) via DuckDB → the structured graph.

    The worker reads the stored file (core/uploads.py) and writes it to a temp file DuckDB reads. The structured pipeline's Step-1 config (`base_schema` stage) drives the
    shape — catalog (Table/Attribute) or one-node-per-row entities. `overrides_json` carries any manual
    parse overrides (delimiter/encoding/sheet…) from the Ingestion Hub; None → DuckDB auto-detects."""
    async def _run():
        await init_v2_schema()
        content = uploads.load(content_b64)
        coll = uuid.UUID(collection_id) if collection_id else None
        overrides = json.loads(overrides_json) if overrides_json else None
        # resolve the pipeline's Step-1 (base_schema) config → reading mode / node type / column roles
        step1 = {}
        _row_cfg = {}
        _rules, _apath = _anchor_opts(overrides if isinstance(overrides, dict) else {}, filename)
        if isinstance(overrides, dict):
            for _k in ("max_rows", "mode", "anchors", "asset_path"):
                if _k in overrides:
                    _v = overrides.pop(_k)
                    if _k in ("max_rows", "mode"):
                        _row_cfg[_k] = _v
            if not overrides:
                overrides = None
        _verify_ids = []
        if isinstance(overrides, dict) and overrides.get("mapping_ids"):
            _verify_ids = [str(x) for x in overrides.pop("mapping_ids") or [] if x]
            if not overrides:
                overrides = None
        if _verify_ids and not pipeline_id:
            from opencortyx.ingest.structured.ingest import load_mapping_table_cards
            overrides = await _merge_mapping_file_settings(workspace_id, _verify_ids, overrides)
            cards = await load_mapping_table_cards(uuid.UUID(workspace_id), _verify_ids)
            if cards:
                step1 = {"tables": cards, "mode": "auto", "auto_mode": "row",
                         "row_node_type": cards[0]["node_type"] if len(cards) == 1 else "",
                         "columns": cards[0]["columns"] if len(cards) == 1 else []}
        if pipeline_id:
            async with V2Session() as db:
                p = await resolve_pipeline(db, uuid.UUID(workspace_id), pipeline_id)
            if p is None:
                raise RuntimeError(f"pipeline {pipeline_id} not found for this org — refusing to "
                                   f"ingest without the assigned pipeline's Step-1 reading rules")
            stages = list(p.stages or [])
            base = next((s for s in stages if isinstance(s, dict) and s.get("action") == "base_schema"), None)
            step1 = (base.get("config") if base else {}) or {}
            from opencortyx.ingest.structured.ingest import (
                resolve_bound_mapping_ids, load_mapping_table_cards)
            _bound = resolve_bound_mapping_ids(step1)
            overrides = await _merge_mapping_file_settings(workspace_id, _bound, overrides)
            if _bound:
                cards = await load_mapping_table_cards(uuid.UUID(workspace_id), _bound)
                if cards:
                    step1 = {**step1, "tables": cards, "mode": "auto", "auto_mode": "row",
                             "row_node_type": cards[0]["node_type"] if len(cards) == 1 else "",
                             "columns": cards[0]["columns"] if len(cards) == 1 else []}
        # An explicit per-upload row rule wins over whatever the pipeline's Step-1 card says: the
        # caller asked for exactly this on THIS run.
        if _row_cfg:
            step1 = {**step1, **_row_cfg}
        rep = _reporter(run_id)
        try:
            res = await walker_for("structured").ingest_file(
                uuid.UUID(workspace_id), content, filename, step1_cfg=step1, collection_id=coll,
                overrides=overrides, reporter=rep)
            await _apply_anchors(workspace_id, res, _rules, _apath, coll)
            _queue_embed_pass(workspace_id, res)
            _queue_extract_pass(workspace_id, res)
            await _post_ingest(workspace_id, res, "structured")
            if rep:
                # record WHICH asset this run produced — without it the run is unattributable and
                # survives as a phantom on the Progress tab when the asset is deleted
                await rep.set_source(res.get("source_id") or res.get("source_key"))
                await rep.finish("completed", result=res)
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_connector_tabular")
def v2_ingest_connector_tabular(self, workspace_id: str, connector_id: str, paths: list,
                                pipeline_id: str = None, collection_id: str = None, run_id: str = None,
                                directories: list = None, anchors: list = None):
    """Ingest chosen TABULAR files (CSV/TSV/Parquet/Excel) from a file/lake connector through the
    structured pipeline. Storage is resolved in-worker (creds never cross the broker); each file's
    bytes are read (fsspec for cloud) into a temp file that DuckDB reads, then ingest_tabular_file
    builds the graph per the pipeline's Step-1 config (catalog vs one-node-per-row)."""
    async def _run():
        import os
        import tempfile
        import anyio
        from opencortyx.ingest.structured.ingest import ingest_tabular_file
        await init_v2_schema()
        oid = uuid.UUID(workspace_id)
        coll = uuid.UUID(collection_id) if collection_id else None
        async with V2Session() as db:
            c = await db.get(Connector, uuid.UUID(connector_id))
            if not c:
                raise RuntimeError("connector not found")
            cfg = c.config or {}
            ckind = (cfg.get("kind") or "").lower()
            if ckind in datalake.LAKE_STORAGE:
                storage_cfg = cfg
            else:                                          # 'files'/format connector → resolve its storage
                storage_cfg = {"kind": "local"}
                if cfg.get("lake_connector_id"):
                    lc = await db.get(Connector, uuid.UUID(cfg["lake_connector_id"]))
                    storage_cfg = (lc.config or {}) if lc else storage_cfg
            step1 = {}
            if pipeline_id:
                p = await resolve_pipeline(db, oid, pipeline_id)
                stages = list(p.stages or []) if p else []
                base = next((s for s in stages if isinstance(s, dict) and s.get("action") == "base_schema"), None)
                step1 = (base.get("config") if base else {}) or {}
        if step1:
            from opencortyx.ingest.structured.ingest import (
                resolve_bound_mapping_ids, load_mapping_table_cards)
            _bound = resolve_bound_mapping_ids(step1)
            if _bound:
                cards = await load_mapping_table_cards(oid, _bound)
                if cards:
                    step1 = {**step1, "tables": cards, "mode": "auto", "auto_mode": "row",
                             "row_node_type": cards[0]["node_type"] if len(cards) == 1 else "",
                             "columns": cards[0]["columns"] if len(cards) == 1 else []}
        # expand server folder paths → the tabular files inside them (recursive), honouring the
        # local-file permission + host→mount translation just like the document lake ingest.
        all_paths = list(paths or [])
        if directories:
            from opencortyx.ingest.formats.structured import tabular as _T
            from opencortyx.core.config import runtime as rc
            exts = [e.lstrip(".") for e in _T.TABULAR_EXTS]
            dirs = list(directories)
            if (storage_cfg.get("kind") or "local").lower() == "local":
                try:
                    await rc.load()
                except Exception:
                    pass
                if not rc.allow_local_files():
                    raise RuntimeError("Local file access is disabled. Enable 'Allow local file access' "
                                       "in Settings → Local files.")
                dirs = [datalake.translate_local_path(d) for d in dirs]
            for d in dirs:
                try:
                    listed = await anyio.to_thread.run_sync(lambda dd=d: datalake.list_files(storage_cfg, dd, exts))
                    all_paths.extend(p for _n, p in listed)
                except Exception as e:
                    log.warning("tabular.list_failed", path=str(d), error=str(e))
        files = list(dict.fromkeys(all_paths))         # de-dupe, keep order
        rep = _reporter(run_id)
        if rep:
            await rep.start(max(1, len(files)))
        done = {"files": 0, "rows": 0, "errors": []}
        for i, path in enumerate(files, 1):
            name = str(path).split("/")[-1]
            if rep and await rep.is_canceled():
                break
            if rep:
                await rep.step(i, name, "running")
            tmp = None
            try:
                data = await anyio.to_thread.run_sync(lambda p=path: datalake.read_file(storage_cfg, p))
                suffix = os.path.splitext(name)[1] or ".csv"
                fd, tmp = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                with open(tmp, "wb") as fh:
                    fh.write(data)
                res = await ingest_tabular_file(oid, tmp, name, step1_cfg=step1, collection_id=coll)
                # `path` — not `name` — is what the rules match on: a folder rule is meaningless
                # against a bare filename, and the folder IS the mapping the user configured.
                await _apply_anchors(workspace_id, res, anchors, path, coll)
                _roll_up_anchors(res, done)
                _queue_embed_pass(workspace_id, res)
                _queue_extract_pass(workspace_id, res)
                await _post_ingest(workspace_id, res, "structured")
                done["files"] += 1
                done["rows"] += int(res.get("rows") or 0)
                if rep:
                    await rep.step(i, name, "completed", res.get("rows"))
            except Exception as e:
                done["errors"].append(f"{name}: {e}")
                if rep:
                    await rep.step(i, name, "failed")
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except Exception:
                        pass
        if rep:
            await rep.finish("completed", result=done)
        return done

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_sql")
def v2_ingest_sql(self, workspace_id: str, dsn: str = None, schema: str = None,
                  collection_id: str = None, enrich: bool = True, pipeline: list = None,
                  run_id: str = None, connector_id: str = None, prune: bool = True,
                  ingestion_id: str = None, anchors: list = None):
    """Native structured ingestion: connect to a SQL source, introspect + profile + LLM-enrich,
    write Table/Attribute nodes and edges into v2, then generate the derived business nodes.

    `pipeline` (the Structured Builder stages) → ordered DAG execution: introspect → each task on
    top of its Input Source. None → full default ingest + enrich (e.g. raw API call).
    `connector_id` → the DSN (with credentials) is resolved INSIDE the worker (from Key Vault / env /
    encrypted), so secrets never pass through the broker. `run_id` drives the Progress tab."""
    async def _run():
        from opencortyx.ingest.structured.ingest import (
            ingest_sql_source, enrich_catalogue_native, run_structured_dag)
        await init_v2_schema()
        oid = uuid.UUID(workspace_id)
        from datetime import datetime, timezone
        _started = datetime.now(timezone.utc)         # this ingest's writes: what the checks look at
        nonlocal dsn, schema
        if connector_id and not dsn:                      # resolve credentials in-worker
            from opencortyx.core.security.secrets import resolve_connector_dsn
            async with V2Session() as _db:
                _c = await _db.get(Connector, uuid.UUID(connector_id))
            if not _c:
                raise RuntimeError("connector not found")
            dsn = resolve_connector_dsn(_c.config or {})
            schema = schema or (_c.config or {}).get("schema")
        coll = uuid.UUID(collection_id) if collection_id else None
        rep = _reporter(run_id)

        try:
            # DAG mode — the Structured Builder sends ordered stages with per-stage Input Source
            if pipeline:
                res = await run_structured_dag(oid, dsn, schema, pipeline, coll, reporter=rep, prune=prune)
            else:
                # no pipeline → default full ingest + enrich (raw API call)
                if rep:
                    await rep.start(2)
                    await rep.step(1, "Introspect & ingest", "running")
                res = await ingest_sql_source(oid, dsn, schema, coll, enrich=True, prune=prune)
                if rep:
                    await rep.set_source(res.get("source_key"))
                    await rep.step(1, "Introspect & ingest", "completed", res.get("tables"))
                    await rep.step(2, "Enrich catalogue", "running")
                if enrich:
                    try:
                        res["enrich"] = await enrich_catalogue_native(
                            oid, coll, source_key=res.get("source_key"))
                    except Exception as exc:
                        res["enrich"] = {"status": "error", "error": str(exc)}
                if rep:
                    await rep.step(2, "Enrich catalogue", "completed")
            # Tables anchor on `schema/table` — the layout a database has in place of folders.
            await _apply_anchors_sql(workspace_id, res, anchors)
            _queue_embed_pass(workspace_id, res)
            # SQL is multi-table (no single source unit) → run Contradiction Detection org-scoped;
            # per-source Versioning is skipped (nothing to fingerprint as one unit). Gated + best-effort.
            try:
                res["conflicts_resolved"] = await sweep(oid, since=_started)
            except Exception as ce:
                log.warning("capabilities.contradiction_failed", kind="sql", error=str(ce))
            if rep:
                await rep.finish("completed", result=res)
            await _close_ingestion(ingestion_id, "completed", run_id)
            return res
        except Exception as e:
            if rep:
                await rep.finish("failed", error=e)
            await _close_ingestion(ingestion_id, "failed", run_id)
            raise

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=2)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.ingest_lake_files")
def v2_ingest_lake_files(self, workspace_id: str, connector_id: str, collection_id: str = None,
                         run_id: str = None, pipeline: list = None, plan: dict = None,
                         directories: list = None, formats: list = None, anchors: list = None):
    """Ingest UNSTRUCTURED files (PDF/docx/txt/…) from a data lake (S3/GCS/ADLS/local) as documents.
    The connector supplies the STORAGE context: either a 'files' format connector (references a
    storage connector + directory) or a lake STORAGE connector directly (local/s3/gcs/adls). When
    `directories` is given (document ingestion wizard), those folders are walked instead of the
    connector's single directory; `formats` (e.g. ["pdf","docx"]) filters extensions (empty → all).
    Each file is read (fsspec for cloud) and run through the normal document pipeline."""
    async def _run():
        import anyio
        import mimetypes
        from opencortyx.ingest.unstructured.ingest import ingest_document
        await init_v2_schema()
        oid = uuid.UUID(workspace_id)
        coll = uuid.UUID(collection_id) if collection_id else None
        async with V2Session() as db:                      # resolve the storage + directories in-worker
            tf = await db.get(Connector, uuid.UUID(connector_id))
            if not tf:
                raise RuntimeError("connector not found")
            cfg = tf.config or {}
            ckind = (cfg.get("kind") or "").lower()
            if ckind in datalake.LAKE_STORAGE:             # the selected connector IS the storage (local/s3/…)
                storage_cfg = cfg
            else:                                          # a 'files' format connector → resolve its storage
                storage_cfg = {"kind": "local"}
                if cfg.get("lake_connector_id"):
                    lc = await db.get(Connector, uuid.UUID(cfg["lake_connector_id"]))
                    storage_cfg = (lc.config or {}) if lc else storage_cfg
            dirs = [d for d in (directories or []) if d and str(d).strip()]
            if not dirs and cfg.get("directory"):          # fall back to the connector's single directory
                dirs = [cfg["directory"]]
        # Local filesystem reads are gated by an explicit Settings permission (ethical, opt-in) and
        # mapped from host path → container mount. Cloud lakes (s3/gcs/adls) use their own creds.
        if (storage_cfg.get("kind") or "local").lower() == "local":
            from opencortyx.core.config import runtime as rc
            try:
                await rc.load()
            except Exception:
                pass
            if not rc.allow_local_files():
                raise RuntimeError("Local file access is disabled. Enable 'Allow local file access' in "
                                   "Settings → Local files, or use browser bulk upload.")
            dirs = [datalake.translate_local_path(d) for d in dirs]
        exts = [e for e in (formats or []) if e] or None   # None → all supported document extensions
        files, seen, walk_errors = [], set(), []
        for d in (dirs or [""]):
            try:
                listed = await anyio.to_thread.run_sync(
                    lambda dd=d: datalake.list_files(storage_cfg, dd, exts))
            except Exception as e:
                walk_errors.append(f"{d or '(connector directory)'}: {e}")
                continue
            for name, path in listed:
                if path in seen:
                    continue
                seen.add(path)
                files.append((name, path))
        rep = _reporter(run_id)
        if rep:
            await rep.start(max(1, len(files)))
        done = {"files": 0, "errors": list(walk_errors)}
        for i, (name, path) in enumerate(files, 1):
            if rep and await rep.is_canceled():
                break
            if rep:
                await rep.step(i, name, "running")
            try:
                data = await anyio.to_thread.run_sync(lambda p=path: datalake.read_file(storage_cfg, p))
                mime = mimetypes.guess_type(name)[0] or "text/plain"
                _res = await ingest_document(oid, data, name, mime, coll, plan=plan, pipeline=pipeline)
                # `path` — not `name` — is what the rules match on: a folder rule is meaningless
                # against a bare filename, and the folder IS the mapping the user configured.
                await _apply_anchors(workspace_id, _res or {}, anchors, path, coll)
                _roll_up_anchors(_res or {}, done)
                done["files"] += 1
                if rep:
                    await rep.step(i, name, "completed")
            except Exception as e:
                done["errors"].append(f"{name}: {e}")
                if rep:
                    await rep.step(i, name, "failed")
        if rep:
            await rep.finish("completed", result=done)
        return done

    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=1)
