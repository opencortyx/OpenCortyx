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


@celery_app.task(bind=True, queue="graphrag", name="opencortyx.run_extractor")
def v2_run_extractor(self, workspace_id: str, template_id: str, collection_id: str = None):
    """Run ONE executable enrichment template over the existing graph (attach LLM-generated
    attributes / nodes / edges onto the nodes it selects)."""
    async def _run():
        from opencortyx.ingest.extraction.templates import run_template_by_id
        await init_v2_schema()
        coll = uuid.UUID(collection_id) if collection_id else None
        return await run_template_by_id(uuid.UUID(workspace_id), uuid.UUID(template_id), coll)
    try:
        return async_to_sync(_run)()
    except Exception as exc:
        raise self.retry(exc=exc, countdown=30, max_retries=1)


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
