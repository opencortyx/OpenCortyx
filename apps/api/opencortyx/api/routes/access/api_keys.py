"""Routes: API keys for agents and MCP clients."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from opencortyx.api.deps import ROLES
from opencortyx.api.schemas.access.api_keys import ApiKeyCreate
from opencortyx.core.security.auth import CurrentUser, require_admin
from opencortyx.core.db import V2Session
from opencortyx.domain.control import ApiKey

router = APIRouter()


@router.get("/apikeys")
async def list_apikeys(admin: CurrentUser = Depends(require_admin)):
    async with V2Session() as db:
        rows = (await db.execute(select(ApiKey).where(
            ApiKey.workspace_id == admin.workspace_id, ApiKey.revoked_at.is_(None))
            .order_by(ApiKey.created_at.desc()))).scalars().all()
    return {"items": [{"id": str(k.id), "app": k.app, "role": (k.scopes or {}).get("role", "editor"),
                       "created_at": k.created_at.isoformat() if k.created_at else None} for k in rows]}


@router.post("/apikeys")
async def create_apikey(spec: ApiKeyCreate, admin: CurrentUser = Depends(require_admin)):
    import hashlib as _hashlib
    import secrets

    role = spec.role if spec.role in ROLES else "editor"
    raw = "ax_" + secrets.token_urlsafe(32)
    perms = ["read"] if role == "viewer" else ["read", "write"]
    async with V2Session() as db:
        k = ApiKey(workspace_id=admin.workspace_id, app=(spec.app or "claude").strip(),
                   hash=_hashlib.sha256(raw.encode()).hexdigest(),
                   scopes={"role": role, "permissions": perms},
                   # an agent's sessions are attributed to the person who issued its key. A key
                   # issued by another (admin) key has no person behind it, so it records none.
                   created_by=admin.user_id if admin.actor_type == "user" else None)
        db.add(k)
        await db.commit()
        await db.refresh(k)
    return {"id": str(k.id), "app": k.app, "role": role, "key": raw}   # raw key shown ONCE


@router.delete("/apikeys/{key_id}")
async def revoke_apikey(key_id: str, admin: CurrentUser = Depends(require_admin)):
    from datetime import datetime, timezone

    async with V2Session() as db:
        k = await db.get(ApiKey, uuid.UUID(key_id))
        if not k or k.workspace_id != admin.workspace_id:
            raise HTTPException(status_code=404, detail="key not found")
        k.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    return {"status": "ok"}
