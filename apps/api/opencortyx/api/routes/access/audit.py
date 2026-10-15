"""Routes: the audit log."""
from fastapi import APIRouter, Depends
from sqlalchemy import select

from opencortyx.domain.control import AuditLog, User
from opencortyx.core.security.auth import CurrentUser, require_auth
from opencortyx.core.db import V2Session

router = APIRouter()


@router.get("/audit")
async def get_audit_logs(limit: int = 100, user: CurrentUser = Depends(require_auth)):
    """Fetch the recent observability and trust event logs."""
    async with V2Session() as db:
        logs = (await db.execute(
            select(AuditLog).where(AuditLog.workspace_id == user.workspace_id)
            .order_by(AuditLog.ts.desc()).limit(limit)
        )).scalars().all()
        # Actors are stored as user ids; the log is read by people, so name them.
        ids = {log.actor for log in logs if log.actor}
        emails = {str(u.id): u.email for u in (await db.execute(
            select(User).where(User.workspace_id == user.workspace_id))).scalars().all() if str(u.id) in ids}

    return {"items": [{
        "id": str(log.id),
        "action": log.action,
        "actor": log.actor,
        "actor_name": emails.get(str(log.actor or "")),
        "node_id": str(log.node_id) if log.node_id else None,
        "latency_ms": log.latency_ms,
        "trust_score": log.trust_score,
        "details": log.meta,
        "created_at": log.ts.isoformat()
    } for log in logs]}
