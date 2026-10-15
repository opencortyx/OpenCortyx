"""Build-side enrichment (runs after extraction).

This module writes NO node types and NO edge types of its own. Every function here is a thin
orchestrator that says WHICH declared extractor runs over WHICH node; the nodes, the edges, the
prompts and the models all live in the extractor cards (ontology/extractors.py + the Studio), so they are
editable without touching code:
  • build_asset_sections  -> the 9 builtin:doc.* profile sections (declared)
  • build_communities     -> builtin:doc.community (Community + IN_COMMUNITY, declared)
"""
import uuid
from opencortyx.core.observability import get_logger
from opencortyx.ingest.extraction.templates import run_template_by_name

log = get_logger(__name__)




# ── Phase 2: asset profile ───────────────────────────────────────────────
async def build_asset_sections(source_id: uuid.UUID, workspace_id: uuid.UUID) -> int:
    """Run the 9 INDEPENDENT section extractors for one asset → one node per section + HAS_SECTION
    edges. 5 computed (structure/quality/usage/change/trust — REAL signals only) + 4 narrative
    (exec_summary/business_purpose/nl_description/ai_insights).

    Each one is an ORDINARY BUILT-IN EXTRACTOR (builtin:doc.*) run by the generic engine: the
    computed ones are Python extractors reading the graph through the SDK, the narrative ones are
    plain prompts over the asset node. This function used to BE those nine extractors — ~100 lines
    of signal assembly and LLM calls that no one could edit, preview or re-run. It now only says
    WHICH extractors run over WHICH asset; identity, embeddings, edges and per-node isolation are
    the engine's. Returns the number of section nodes written."""
    from opencortyx.ontology.extractors import PROFILE_SECTION_NAMES
    from opencortyx.core.substrate import NodeRepo
    if await NodeRepo().get(workspace_id, source_id) is None:
        return 0
    made = 0
    for name in PROFILE_SECTION_NAMES:
        try:
            r = await run_template_by_name(workspace_id, name, node_ids=[source_id])
            made += int((r or {}).get("nodes") or 0)
        except Exception as e:                            # noqa: BLE001 — one section never stops the rest
            log.warning("enrich.asset_sections_failed", section=str(name), source_id=str(source_id),
                      error=str(e))
    return made


# ── Phase 4: communities (GraphRAG global search) ────────────────────────
async def build_communities(workspace_id: uuid.UUID, min_size: int = 3, max_communities: int = 50) -> int:
    """Cluster entities into communities and write a Community node per cluster with an LLM theme
    summary + IN_COMMUNITY edges. These power 'big picture' GLOBAL search (CommunityRetriever).

    The clustering itself (union-find over RELATES_TO) is now the editable `mechanism.code` of the
    builtin:doc.community extractor, which reads the topology through the SDK's graph access — so
    the algorithm is inspectable and tunable in the Studio rather than compiled in here. Idempotent:
    a stable cluster keeps its node (identity = its smallest member id).

    `min_size` / `max_communities` are the extractor's own constants now; they are accepted here for
    call-site compatibility and ignored, so the tuned values live in ONE place — the card."""
    try:
        r = await run_template_by_name(workspace_id, "builtin:doc.community")
    except Exception as e:                                # noqa: BLE001
        log.warning("enrich.communities_failed", error=str(e))
        return 0
    return int((r or {}).get("nodes") or 0)


