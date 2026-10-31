"""Enrichment tasks: extractors, corpus enrichment, domain seeding, KPI scheduling."""
import uuid
from opencortyx.core.worker.celery_app import celery_app
from opencortyx.core.worker.utils import async_to_sync
from opencortyx.core.observability import get_logger
from opencortyx.bootstrap import init_v2_schema
from opencortyx.capabilities.modules.contradiction import sweep
from opencortyx.core.substrate import NodeRepo

log = get_logger(__name__)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.seed_domain")
def v2_seed_domain(self, workspace_id: str, domain: str, n_tables: int = 6, collection_id: str = None):
    """LLM-populate a whole catalogue topology (tables/cols/FKs/synonyms + derived nodes) from a
    domain name — no source DB needed."""
    async def _run():
        from opencortyx.ingest.structured.ingest import seed_domain
        await init_v2_schema()
        coll = uuid.UUID(collection_id) if collection_id else None
        return await seed_domain(uuid.UUID(workspace_id), domain, n_tables, coll)
    return async_to_sync(_run)()


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.structured_enrich_native")
def v2_structured_enrich_native(self, workspace_id: str, collection_id: str = None):
    """Re-generate the derived business nodes from the v2 catalogue. Idempotent."""
    async def _run():
        from opencortyx.ingest.structured.ingest import enrich_catalogue_native
        await init_v2_schema()
        coll = uuid.UUID(collection_id) if collection_id else None
        return await enrich_catalogue_native(uuid.UUID(workspace_id), coll)
    return async_to_sync(_run)()


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.corpus_enrich")
def v2_corpus_enrich(self, workspace_id: str):
    """Org-level: communities + conflict detection (trust)."""
    async def _run():
        from opencortyx.ingest.extraction.enrich import build_communities
        await init_v2_schema()
        communities = await build_communities(uuid.UUID(workspace_id))
        conflicts = await sweep(uuid.UUID(workspace_id))
        return {"status": "ok", "communities": communities, "conflicts": conflicts}
    return async_to_sync(_run)()


def _deadline() -> float:
    """When a run stops to continue in a new task: well before the soft time limit, so the node in
    hand finishes and commits (one model call on CPU can take a minute)."""
    import time

    from opencortyx.core.config import settings
    return time.monotonic() + max(60.0, settings.TASK_SOFT_TIME_LIMIT * 0.75)


def continue_run(task, result: dict, **kwargs) -> dict:
    """A run that stopped at its deadline ("partial") is queued again with what it has read, so the
    rest is read in a new task and nothing twice."""
    if isinstance(result, dict) and result.get("status") == "partial":
        task.apply_async(kwargs={**kwargs, "done": result["done"]})
        log.info("extractor.continued", name=result.get("name"), read=len(result["done"]))
    return result


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.run_extractor")
def v2_run_extractor(self, workspace_id: str, template_id: str, collection_id: str = None, done: list = ()):
    """Run ONE executable enrichment template over the existing graph (attach LLM-generated
    attributes / nodes / edges onto the nodes it selects); a long run continues in a new task."""
    async def _run():
        from opencortyx.ingest.extraction.templates import run_template_by_id
        await init_v2_schema()
        coll = uuid.UUID(collection_id) if collection_id else None
        return await run_template_by_id(uuid.UUID(workspace_id), uuid.UUID(template_id), coll,
                                        deadline=_deadline(), done=done)
    try:
        result = async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=1)
    return continue_run(self, result, workspace_id=workspace_id, template_id=template_id,
                        collection_id=collection_id)


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.run_all_extractors")
def v2_run_all_extractors(self, workspace_id: str, collection_id: str = None):
    """Run every executable enrichment template over the existing graph (excludes chunk-time ones)."""
    async def _run():
        from opencortyx.ingest.extraction.templates import run_all
        await init_v2_schema()
        coll = uuid.UUID(collection_id) if collection_id else None
        return await run_all(uuid.UUID(workspace_id), coll)
    return async_to_sync(_run)()


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.schedule_kpi_computation")
def schedule_kpi_computation(self, workspace_id: str):
    """Count the graph and store the figures as a Report node."""
    async def _run():
        from datetime import datetime, timezone
        repo, oid = NodeRepo(), uuid.UUID(workspace_id)
        kpi_data = {
            "total_nodes": await repo.count(oid),
            "total_edges": await repo.count_edges(oid),
            "total_structured_tables": await repo.count(oid, types=["Table"]),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        await repo.upsert_node(workspace_id=oid, type="Report", label="Scheduled KPI Report",
                               properties={"metrics": kpi_data, "is_kpi": True})
        return {"status": "ok", "kpis": kpi_data}

    return async_to_sync(_run)()


# ── Add data, Custom: extractors chosen for one upload ────────────────────────────────────────────

#: The extractors an upload chose, until its run finishes: [{"name", "inputs"}].
_BOOKED = "v2:custom-extractors:{}"


def book_extractors(run_id, items: list) -> None:
    """Remember the extractors an upload chose (Add data, Custom), with the columns, keys or sections
    each reads; they run when the upload's run finishes (`queue_booked_extractors`). Best-effort."""
    if not run_id or not items:
        return
    try:
        import json

        import redis as _redis

        from opencortyx.core.config import settings
        _redis.Redis.from_url(settings.CELERY_BROKER_URL).set(_BOOKED.format(run_id), json.dumps(items),
                                                              ex=7 * 24 * 3600)
    except Exception as e:                                    # noqa: BLE001
        log.warning("custom_extractors.book_failed", error=str(e)[:200])


def queue_booked_extractors(workspace_id, run_id, source_id, kind: str | None) -> int:
    """Queue the extractors booked for this run over the asset it read: a table's or a JSON file's
    columns or keys, a document's sections (none chosen: all of it). Returns how many were queued."""
    if not run_id or not source_id:
        return 0
    try:
        import json

        import redis as _redis

        from opencortyx.core.config import settings
        raw = _redis.Redis.from_url(settings.CELERY_BROKER_URL).getdel(_BOOKED.format(run_id))
    except Exception as e:                                    # noqa: BLE001
        log.warning("custom_extractors.take_failed", error=str(e)[:200])
        return 0
    items = json.loads(raw) if raw else []
    for it in items:
        inputs = [str(x) for x in (it.get("inputs") or []) if str(x).strip()]
        data = {"asset": str(source_id)}
        if inputs:
            data["sections" if kind == "document" else "columns"] = inputs
        v2_run_extractor_over.delay(str(workspace_id), str(it.get("name") or ""), data)
    return len(items)


@celery_app.task(bind=True, queue="extraction", name="opencortyx.run_extractor_over", acks_late=True,
                 max_retries=2)
def v2_run_extractor_over(self, workspace_id: str, name: str, data: dict, done: list = ()):
    """Run a saved extractor over one asset's data - the columns, keys or sections `data` names
    (`ingest/extraction/data_inputs.py`) - whatever the extractor reads elsewhere; a long run
    continues in a new task."""
    async def _run():
        from sqlalchemy import select

        from opencortyx.core.db import V2Session
        from opencortyx.domain.control import ExtractionTemplate
        from opencortyx.ingest.extraction.templates import run_template
        oid = uuid.UUID(workspace_id)
        async with V2Session() as db:
            t = (await db.execute(select(ExtractionTemplate).where(
                ExtractionTemplate.workspace_id == oid, ExtractionTemplate.name == name))).scalar_one_or_none()
            if t is None:
                return {"status": "not_found", "name": name}
            db.expunge(t)
        own = (t.source or {}).get("data") or {}
        # no inputs chosen: it reads what it was built to read (a type's records), else the asset
        reads = own if set(data) == {"asset"} and own.get("type") else data
        t.source = {**(t.source or {}), "select": "*", "data": reads}   # this run only; never saved
        return await run_template(oid, t, deadline=_deadline(), done=done)

    try:
        result = async_to_sync(_run)()
    except Exception as exc:
        from opencortyx.tasks._shared import retry_policy
        _max, _delay = retry_policy(self, exc, 2)
        raise self.retry(exc=exc, countdown=_delay, max_retries=_max)
    return continue_run(self, result, workspace_id=workspace_id, name=name, data=data)
