"""The /api/v2 router.

Resource routers are included in a fixed order: when two routes match the same request, the one
included first handles it.
"""
from fastapi import APIRouter

from opencortyx.api.routes.access import api_keys, audit, auth, collections, users
from opencortyx.api.routes.graph import (
    assets, comments, definitions, edges, glossary, graph, nodes, ontology, resolution, tree)
from opencortyx.api.routes.ingestion import (
    connectors, extraction, ingest, ingestions, pipelines, progress, quarantine, schema_mappings,
    structured)
from opencortyx.api.routes.knowledge import (
    authoring, capabilities, chats, context, evaluation, memory)
from opencortyx.api.routes.system import (
    backup, config, cost, health, integrations, maintenance, models, stats)

router = APIRouter(prefix="/v2", tags=["context-layer-v2"])
router.include_router(auth.router)
router.include_router(users.router)
router.include_router(api_keys.router)
router.include_router(ingest.router)
router.include_router(connectors.router)
router.include_router(ingestions.router)
router.include_router(structured.router)
router.include_router(extraction.router)
router.include_router(context.router)
router.include_router(chats.router)
router.include_router(collections.router)
router.include_router(graph.router)
router.include_router(tree.router)
router.include_router(stats.router)
router.include_router(ontology.router)
router.include_router(resolution.router)
router.include_router(nodes.router)
router.include_router(maintenance.router)
router.include_router(edges.router)
router.include_router(glossary.router)
router.include_router(definitions.router)
router.include_router(audit.router)
router.include_router(progress.router)
router.include_router(models.router)
router.include_router(pipelines.router)
router.include_router(schema_mappings.router)
router.include_router(config.router)
router.include_router(capabilities.router)
router.include_router(quarantine.router)
router.include_router(health.router)
router.include_router(backup.router)
router.include_router(cost.router)
router.include_router(memory.router)
router.include_router(integrations.router)
router.include_router(evaluation.router)
router.include_router(comments.router)
router.include_router(assets.router)
router.include_router(authoring.router)
