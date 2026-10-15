"""Routes: workspace users and their roles."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from opencortyx.api.deps import ROLES
from opencortyx.api.schemas.access.users import UserCreate, UserPatch
from opencortyx.api.services.access.users import active_admin_count, user_view
from opencortyx.core.security.auth import CurrentUser, hash_password, require_admin
from opencortyx.core.db import V2Session
from opencortyx.domain.control import User

router = APIRouter()


@router.get("/users")
async def list_users(admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        rows = (await db.execute(select(User).where(User.workspace_id == admin.workspace_id)
                                 .order_by(User.created_at))).scalars().all()
    return {"items": [user_view(u) for u in rows]}


@router.post("/users")
async def create_user(spec: UserCreate, admin: CurrentUser = Depends(require_admin)):
    email = spec.email.strip().lower()
    if not email or len(spec.password) < 6:
        raise HTTPException(status_code=400, detail="Email and a password (6+ chars) are required.")
    role = spec.role if spec.role in ROLES else "viewer"
    async with V2Session() as db:
        dup = (await db.execute(select(User).where(
            User.workspace_id == admin.workspace_id, func.lower(User.email) == email))).scalars().first()
        if dup:
            raise HTTPException(status_code=409, detail="A user with that email already exists.")
        u = User(workspace_id=admin.workspace_id, email=email, password_hash=hash_password(spec.password),
                 role=role, status="active")
        db.add(u)
        await db.commit()
        await db.refresh(u)
    return user_view(u)


@router.patch("/users/{user_id}")
async def update_user(user_id: str, spec: UserPatch, admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        u = await db.get(User, uuid.UUID(user_id))
        if not u or u.workspace_id != admin.workspace_id:
            raise HTTPException(status_code=404, detail="user not found")
        demoting = (spec.role in ROLES and spec.role != "admin" and u.role == "admin")
        disabling = (spec.status == "disabled" and u.status == "active" and u.role == "admin")
        if (demoting or disabling) and await active_admin_count(db, admin.workspace_id, exclude=u.id) == 0:
            raise HTTPException(status_code=400, detail="Can't remove the last admin.")
        if spec.role in ROLES:
            u.role = spec.role
        if spec.status in ("active", "disabled"):
            u.status = spec.status
        if spec.password:
            if len(spec.password) < 6:
                raise HTTPException(status_code=400, detail="Password must be 6+ chars.")
            u.password_hash = hash_password(spec.password)
        await db.commit()
        return user_view(u)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, admin: CurrentUser = Depends(require_admin)):
    if str(admin.user_id) == user_id:
        raise HTTPException(status_code=400, detail="You can't delete your own account.")
    async with V2Session() as db:
        u = await db.get(User, uuid.UUID(user_id))
        if not u or u.workspace_id != admin.workspace_id:
            raise HTTPException(status_code=404, detail="user not found")
        await db.delete(u)
        await db.commit()
    return {"status": "ok"}
