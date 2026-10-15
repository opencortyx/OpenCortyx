"""Services: workspace users and their roles."""
from sqlalchemy import func, select


def user_view(u) -> dict:
    return {"id": str(u.id), "email": u.email, "role": u.role, "status": u.status,
            "created_at": u.created_at.isoformat() if u.created_at else None}


async def active_admin_count(db, workspace_id, exclude=None) -> int:
    from opencortyx.domain.control import User
    stmt = select(func.count()).select_from(User).where(
        User.workspace_id == workspace_id, User.role == "admin", User.status == "active")
    if exclude:
        stmt = stmt.where(User.id != exclude)
    return (await db.execute(stmt)).scalar() or 0
