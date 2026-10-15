"""Routes: catalogued assets: profiles, overviews and deletion."""
import uuid

from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.assets import OverviewEdit
from opencortyx.core.security.auth import CurrentUser, require_write
from opencortyx.domain.ontology import ASSET_TYPES
from opencortyx.core.substrate import NodeRepo
from opencortyx.core.observability import get_logger

log = get_logger(__name__)

router = APIRouter()
_repo = NodeRepo()


@router.post("/assets/backfill-profiles")
async def backfill_profiles_ep(user: CurrentUser = Depends(require_write), limit: int = 500):
    """Run the 9 section extractors over existing assets (idempotent), for assets ingested before the
    extractors existed."""
    from opencortyx.ingest.extraction.enrich import build_asset_sections
    assets, sections = 0, 0
    for sid in [n.id for n in await _repo.find(user.workspace_id, types=list(ASSET_TYPES), limit=limit)]:
        try:
            sections += await build_asset_sections(sid, user.workspace_id)
            assets += 1
        except Exception:
            pass
    return {"assets": assets, "sections": sections}


@router.post("/assets/{source_id}/generate-overview")
async def generate_overview_ep(source_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Generate (or regenerate) the AI overview for ONE asset on demand — its profile sections. This is the manual 'Generate' action from the asset page, so
    it runs regardless of the global `ai_overviews` toggle (which only governs auto-generation at ingest).
    Idempotent (upserts the section nodes + HAS_SECTION edges)."""
    if await _repo.get(user.workspace_id, source_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    from opencortyx.ingest.extraction.enrich import build_asset_sections
    sections = await build_asset_sections(source_id, user.workspace_id)
    return {"status": "ok", "sections": sections}


@router.put("/assets/{source_id}/overview")
async def edit_overview_ep(source_id: uuid.UUID, patch: OverviewEdit,
                           user: CurrentUser = Depends(require_write)):
    """Persist USER-EDITED AI overview sections (executive summary / business purpose / NL description)
    onto their section nodes — same deterministic id + HAS_SECTION edge the generator uses, so an edit
    updates in place and stays connected to the asset. Marks the section `__source: user` (human-owned)."""
    from opencortyx.core.inference.embedding import embed_batch
    keymap = {"exec_summary": "ExecutiveSummary", "business_purpose": "BusinessPurpose",
              "nl_description": "NLDescription"}
    if await _repo.get(user.workspace_id, source_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    edited = []
    for key, ntype in keymap.items():
        text = getattr(patch, key)
        if text is None:
            continue
        pid = uuid.uuid5(uuid.NAMESPACE_URL, f"section:{key}:{user.workspace_id}:{source_id}")
        edited.append(await _repo.upsert_node(
            workspace_id=user.workspace_id, type=ntype, label=key.replace("_", " ").title(),
            properties={"text": text, "section": key, "__source": "user", "__edited": True},
            source_ref=source_id, level=1, confidence=1.0, node_id=pid))
        await _repo.add_edge(workspace_id=user.workspace_id, source_id=source_id, target_id=pid, type="HAS_SECTION")
    await _repo.index_embeddings(user.workspace_id, edited, embed_batch)
    return {"status": "ok", "updated": len(edited)}


@router.get("/assets/{asset_id}/delete-preview")
async def preview_asset_delete_ep(asset_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """What deleting this asset WOULD remove (nodes by type + edges) — powers the confirm dialog."""
    from opencortyx.ingest.runs.asset_purge import preview_asset_delete
    res = await preview_asset_delete(user.workspace_id, asset_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    return res


@router.delete("/assets/{asset_id}")
async def delete_asset_ep(asset_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """PERMANENTLY delete an asset: the asset node and every node that exists only because of it
    (chunks, properties/rows, entities, claims, summaries, profile sections, comments), all their
    edges, its Progress runs, and its entry in any sync manifest. Not recoverable. Idempotent."""
    from opencortyx.ingest.runs.asset_purge import purge_asset_full
    res = await purge_asset_full(user.workspace_id, asset_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Asset not found")
    await audit(user.workspace_id, user.user_id, "asset/delete", node_id=asset_id,
                meta={"label": res.get("label"), "type": res.get("type"),
                       "nodes": res["nodes"], "edges": res["edges"], "runs": res["runs"]})
    return res


@router.delete("/sources/{source_id}")
async def delete_source_ep(source_id: uuid.UUID, user: CurrentUser = Depends(require_write)):
    """Alias of DELETE /assets/{id} — same full purge, kept for existing callers."""
    return await delete_asset_ep(source_id, user)
