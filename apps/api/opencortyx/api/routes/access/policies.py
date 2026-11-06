"""Routes: access policies - which group reads which collection, up to what sensitivity.

The rule they feed is in core/security/scope.py. Adding the FIRST policy turns access control on for
the workspace (deny by default: a caller reads the collections its groups are given, and records in
no collection); deleting the last turns it off. `/access/preview` says what a group or a person
would read before anyone relies on it.
"""
import uuid
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select

from opencortyx.core.db import V2Session
from opencortyx.core.security import scope as _scope
from opencortyx.core.security.auth import CurrentUser, require_admin
from opencortyx.domain.control import SENSITIVITY_ORDER, Collection, Policy, User

router = APIRouter()


class PolicyIn(BaseModel):
    group: str = Field(..., min_length=1, max_length=120)
    collection_id: Optional[str] = None          # None: every collection
    max_sensitivity: Literal["public", "normal", "confidential", "restricted"] = "normal"

    @field_validator("group")
    @classmethod
    def _g(cls, v):
        v = " ".join((v or "").split()).lower()
        if not v:
            raise ValueError("a group name is required")
        return v


def _view(p: Policy, names: dict) -> dict:
    return {"id": str(p.id), "group": p.group,
            "collection_id": str(p.collection_id) if p.collection_id else None,
            "collection": names.get(p.collection_id) if p.collection_id else "Every collection",
            "max_sensitivity": p.max_sensitivity, "permission": p.permission,
            "created_at": p.created_at.isoformat() if p.created_at else None}


@router.get("/access/policies")
async def list_policies(admin: CurrentUser = Depends(require_admin)):
    """The policies, the collections and groups to choose from, and whether access control is on."""
    async with V2Session() as db:
        rows = (await db.execute(select(Policy).where(Policy.workspace_id == admin.workspace_id)
                                 .order_by(Policy.group, Policy.created_at))).scalars().all()
        colls = (await db.execute(select(Collection).where(Collection.workspace_id == admin.workspace_id)
                                  .order_by(Collection.name))).scalars().all()
        users = (await db.execute(select(User.role, User.groups).where(
            User.workspace_id == admin.workspace_id))).all()
    names = {c.id: c.name for c in colls}
    groups = sorted({str(g) for role, gs in users for g in ([role] + list(gs or []))} | {p.group for p in rows})
    return {"enabled": bool(rows), "items": [_view(p, names) for p in rows],
            "collections": [{"id": str(c.id), "name": c.name} for c in colls],
            "groups": groups, "sensitivities": list(SENSITIVITY_ORDER)}


@router.post("/access/policies")
async def create_policy(body: PolicyIn, admin: CurrentUser = Depends(require_admin)):
    cid = None
    async with V2Session() as db:
        if body.collection_id:
            try:
                cid = uuid.UUID(body.collection_id)
            except ValueError:
                raise HTTPException(status_code=422, detail="collection_id is not an id")
            c = await db.get(Collection, cid)
            if c is None or c.workspace_id != admin.workspace_id:
                raise HTTPException(status_code=404, detail="collection not found")
        dup = (await db.execute(select(Policy).where(
            Policy.workspace_id == admin.workspace_id, Policy.group == body.group,
            (Policy.collection_id == cid) if cid else Policy.collection_id.is_(None)))).scalars().first()
        if dup is not None:
            dup.max_sensitivity = body.max_sensitivity
            p = dup
        else:
            p = Policy(workspace_id=admin.workspace_id, group=body.group, collection_id=cid,
                       max_sensitivity=body.max_sensitivity, permission="read")
            db.add(p)
        first = not (await db.execute(select(func.count()).select_from(Policy).where(
            Policy.workspace_id == admin.workspace_id))).scalar()
        await db.commit()
        await db.refresh(p)
        names = {c.id: c.name for c in (await db.execute(select(Collection).where(
            Collection.workspace_id == admin.workspace_id))).scalars().all()}
    _scope.forget(workspace_id=admin.workspace_id)
    return {**_view(p, names), "access_control_turned_on": bool(first and dup is None)}


@router.delete("/access/policies/{policy_id}")
async def delete_policy(policy_id: uuid.UUID, admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        p = await db.get(Policy, policy_id)
        if p is None or p.workspace_id != admin.workspace_id:
            raise HTTPException(status_code=404, detail="policy not found")
        await db.delete(p)
        await db.commit()
        left = (await db.execute(select(func.count()).select_from(Policy).where(
            Policy.workspace_id == admin.workspace_id))).scalar()
    _scope.forget(workspace_id=admin.workspace_id)
    return {"status": "deleted", "access_control_on": bool(left)}


@router.get("/access/preview")
async def preview_access(group: Optional[str] = None, user_id: Optional[str] = None,
                         admin: CurrentUser = Depends(require_admin)):
    """What a group (or a person, with all their groups) reads: for questions and for browsing."""
    groups: list = []
    role = None
    if user_id:
        async with V2Session() as db:
            u = await db.get(User, uuid.UUID(user_id))
        if u is None or u.workspace_id != admin.workspace_id:
            raise HTTPException(status_code=404, detail="user not found")
        role = u.role
        groups = list(dict.fromkeys([u.role] + list(u.groups or [])))
    elif group:
        groups = [group]
    else:
        raise HTTPException(status_code=422, detail="give a group or a user_id")
    allowed, sens = await _scope.resolve_scope(admin.workspace_id, groups, admin=role == "admin")
    async with V2Session() as db:
        names = {str(c.id): c.name for c in (await db.execute(select(Collection).where(
            Collection.workspace_id == admin.workspace_id))).scalars().all()}
    cols = None if allowed is None else [names.get(str(c)) for c in allowed if str(c) in names]
    return {"groups": groups, "collections": cols, "every_collection": allowed is None,
            "workspace_wide_records": True, "max_sensitivity": sens}
