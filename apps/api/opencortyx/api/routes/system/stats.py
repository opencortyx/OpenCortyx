"""Routes: corpus and graph statistics."""
from fastapi import APIRouter, Depends

from opencortyx.domain.control import AuditLog
from opencortyx.core.security.auth import CurrentUser, require_auth, require_write
from opencortyx.core.db import V2Session
from opencortyx.core.substrate import NodeRepo

router = APIRouter()


@router.get("/stats")
async def stats(user: CurrentUser = Depends(require_auth)):
    """Counts of nodes and edges by type, and how many nodes are embedded."""
    repo = NodeRepo()
    nbt = await repo.count_by_type(user.workspace_id)
    ebt = await repo.count_edges_by_type(user.workspace_id)
    return {
        "nodes_by_type": nbt,
        "edges_by_type": ebt,
        "embedded_nodes": await repo.vectors.count_nodes(user.workspace_id),
        "total_nodes": sum(nbt.values()),
        "total_edges": sum(ebt.values()),
    }


@router.post("/stats/compute")
async def trigger_kpi_computation(user: CurrentUser = Depends(require_write)):
    """Trigger the scheduled background task for KPI aggregation."""
    from opencortyx.tasks.enrich import schedule_kpi_computation
    task = schedule_kpi_computation.delay(str(user.workspace_id))

    async with V2Session() as db:
        db.add(AuditLog(workspace_id=user.workspace_id, actor=str(user.user_id), action="compute_kpis",
                        meta={"task_id": task.id}))
        await db.commit()

    return {"task_id": task.id, "status": "dispatched"}
