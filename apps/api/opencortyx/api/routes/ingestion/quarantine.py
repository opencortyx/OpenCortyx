"""Routes: extractions whose model output could not be parsed."""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opencortyx.api.deps import audit
from opencortyx.api.schemas.ingestion.quarantine import QuarantineRetrySpec
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo

router = APIRouter()
_repo = NodeRepo()
_ANOMALY = "DataQualityAnomaly"


async def _anomaly(workspace_id, node_id: str):
    node = await _repo.get(workspace_id, uuid.UUID(node_id))
    return node if node is not None and node.type == _ANOMALY else None


@router.get("/quarantine")
async def list_quarantine(user: CurrentUser = Depends(require_auth)):
    nodes = await _repo.find(user.workspace_id, exact_types=[_ANOMALY], order_by="-created_at", limit=100)
    return {"items": [{
        "id": str(n.id),
        "chunk_node_id": str(n.source_ref) if n.source_ref else None,
        "error_type": n.properties.get("error_type"),
        "error_message": n.properties.get("error_message"),
        "failed_output": n.properties.get("failed_output"),
        "chunk_text": n.properties.get("chunk_text"),
        "extractor_type": n.properties.get("extractor_type"),
        "template_name": n.properties.get("template_name"),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n in nodes]}


@router.get("/quarantine/writes")
async def list_write_quarantine(user: CurrentUser = Depends(require_auth)):
    """Writes that did not land — edges whose endpoint never appeared, records no mapping matched,
    writes the ontology refused. Recorded by the substrate so nothing is lost silently; listed here
    so "recorded" also means "visible", which the anomaly list above never covered."""
    from opencortyx.core.substrate.quarantine import list_rows, summary
    return {"summary": await summary(user.workspace_id), "items": await list_rows(user.workspace_id)}


@router.delete("/quarantine/{node_id}")
async def dismiss_quarantine(node_id: str, user: CurrentUser = Depends(require_write)):
    if await _anomaly(user.workspace_id, node_id) is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    await _repo.delete_nodes(user.workspace_id, [uuid.UUID(node_id)])
    await audit(user.workspace_id, user.user_id, "quarantine/dismiss", node_id=uuid.UUID(node_id))
    return {"status": "ok"}


@router.post("/quarantine/{node_id}/retry")
async def retry_quarantine(node_id: str, spec: QuarantineRetrySpec, user: CurrentUser = Depends(require_write)):
    from opencortyx.domain.control import ExtractionTemplate
    from opencortyx.ingest.extraction.templates import apply_output

    try:
        data_dict = json.loads(spec.json_data)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"Invalid JSON format: {str(e)}")

    node = await _anomaly(user.workspace_id, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    if node.properties.get("extractor_type") != "template":
        return {"status": "error", "message": "Unknown extractor type"}
    async with V2Session() as db:
        tmpl = (await db.execute(select(ExtractionTemplate).where(
            ExtractionTemplate.workspace_id == user.workspace_id,
            ExtractionTemplate.name == node.properties.get("template_name")))).scalar_one_or_none()
    if not tmpl:
        return {"status": "error", "message": "Template not found"}
    try:
        await apply_output(user.workspace_id, tmpl, node.source_ref, data_dict, node.collection_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    await _repo.delete_nodes(user.workspace_id, [node.id])
    await audit(user.workspace_id, user.user_id, "quarantine/retry",
                node_id=node.source_ref, meta={"extractor_type": "template"})
    return {"status": "ok", "message": "Extraction succeeded and anomaly resolved"}


@router.post("/quarantine/{node_id}/rerun")
async def rerun_quarantine(node_id: str, user: CurrentUser = Depends(require_write)):
    """Re-run the original extractor on the chunk. The stale anomaly is dropped first; if the model
    fails again, the extractor quarantines the fresh output once more."""
    from opencortyx.ingest.extraction.templates import existing_template_names, run_named_templates_on_chunks

    node = await _anomaly(user.workspace_id, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    ext_type = node.properties.get("extractor_type")
    chunk_node_id, collection_id = node.source_ref, node.collection_id
    chunk_text = node.properties.get("chunk_text", "")
    if ext_type == "template" and node.properties.get("template_name"):
        names = [node.properties["template_name"]]
    else:
        return {"status": "error", "message": "Unknown extractor type"}
    if not (chunk_text or "").strip():
        return {"status": "error", "message": "No source text to re-run."}
    if not await existing_template_names(user.workspace_id, names):
        return {"status": "error", "message": "That extractor no longer exists — dismiss this entry."}
    await _repo.delete_nodes(user.workspace_id, [node.id])
    try:
        # the same engine every ingest uses — its prompt, its input binding and its emit
        await run_named_templates_on_chunks(user.workspace_id, names, [(chunk_node_id, chunk_text)],
                                            collection_id)
    except Exception as e:
        return {"status": "error", "message": str(e)}
    requarantined = bool(chunk_node_id is not None and await _repo.find(
        user.workspace_id, exact_types=[_ANOMALY], source_refs=[chunk_node_id], limit=1))
    await audit(user.workspace_id, user.user_id, "quarantine/rerun",
                node_id=chunk_node_id, meta={"extractor_type": ext_type})
    if requarantined:
        return {"status": "requarantined",
                "message": "The model failed again — re-quarantined. Try editing the JSON manually."}
    return {"status": "ok", "message": "Re-ran with the model and rescued the data."}
