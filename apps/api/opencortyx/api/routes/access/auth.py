"""Routes: sign-in, token refresh and first-run setup."""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from opencortyx.api.schemas.access.auth import RefreshRequest, SetupRequest, TokenRequest
from opencortyx.api.services.access.auth import (
    DEMO_ORG,
    access_token_ttl_seconds,
    count_users,
    materialize_for_mode,
)
from opencortyx.core.security.auth import (
    CurrentUser,
    create_access_token,
    create_refresh_token,
    hash_password,
    require_auth,
    verify_password,
)
from opencortyx.core.db import V2Session
from opencortyx.domain.control import User

router = APIRouter()


@router.get("/auth/needs-setup")
async def auth_needs_setup():
    """True when no users exist yet → the web app shows the first-run Setup screen."""
    return {"needs_setup": (await count_users()) == 0}


@router.post("/auth/setup")
async def auth_setup(req: SetupRequest):
    """First-run bootstrap: create the workspace + its first ADMIN. Open ONLY while no users exist."""
    if not req.email.strip() or len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Email and a password (6+ chars) are required.")
    from opencortyx.knowledge.workspace import InvalidWorkspace, clean_mode, clean_name, save_profile
    try:
        name, mode = clean_name(req.name), clean_mode(req.mode)
    except InvalidWorkspace as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (await count_users()) > 0:
        raise HTTPException(status_code=409, detail="Setup already done — ask an admin to register you.")
    workspace_id = DEMO_ORG if req.adopt_demo else uuid.uuid4()
    async with V2Session() as db:
        u = User(workspace_id=workspace_id, email=req.email.strip().lower(),
                 password_hash=hash_password(req.password), role="admin", status="active")
        db.add(u)
        await db.commit()
        await db.refresh(u)
    # register the built-in on-prem defaults (gen/embed/rerank) so a fresh workspace's Model Registry
    # is populated and functional out of the box; the admin can swap them later per use case.
    try:
        from opencortyx.core.inference.registry import ensure_default_models
        await ensure_default_models(workspace_id)
    except Exception:
        pass
    # put BOTH built-in hierarchies (Person + Team) on the graph so a fresh install already shows every
    # node & relationship from both — editable/deletable/toggleable from the Nodes & Relationships page.
    await materialize_for_mode(workspace_id, "person")
    await materialize_for_mode(workspace_id, "team")
    # the workspace's name and mode, and the central node — you, or your team — at the top of its graph
    await save_profile(workspace_id, name=name, mode=mode)
    token = create_access_token(u.id, workspace_id, role="admin")
    return {"access_token": token, "token_type": "bearer", "workspace_id": str(workspace_id), "role": "admin"}


@router.post("/auth/token")
async def issue_token(req: TokenRequest):
    """Real login — validate email+password against the user store. Returns the shape next-auth wants."""
    email = (req.username or "").strip().lower()
    if (await count_users()) == 0:
        raise HTTPException(status_code=401, detail="No users yet — complete first-run setup.")
    async with V2Session() as db:
        u = (await db.execute(select(User).where(func.lower(User.email) == email))).scalars().first()
    if not u or u.status != "active" or not verify_password(req.password, u.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_access_token(u.id, u.workspace_id, role=u.role)
    refresh = create_refresh_token(u.id, u.workspace_id)
    return {"access_token": token, "refresh_token": refresh, "token_type": "bearer",
            "expires_in": access_token_ttl_seconds(),
            "workspace_id": str(u.workspace_id), "role": u.role}


@router.post("/auth/refresh")
async def refresh_access_token(req: RefreshRequest):
    """Exchange a refresh token for a new access token.

    The user is re-read rather than trusted from the token: a refresh token is valid for days, and in
    that time its holder may have been demoted or deactivated. Minting the new access token from the
    CURRENT row is what stops a stale token from carrying stale authority.
    """
    from opencortyx.core.security.auth import decode_refresh_token
    user_id, workspace_id = decode_refresh_token(req.refresh_token)
    async with V2Session() as db:
        u = await db.get(User, user_id)
    if not u or u.workspace_id != workspace_id or u.status != "active":
        raise HTTPException(status_code=401, detail="This session is no longer valid — sign in again.")
    return {"access_token": create_access_token(u.id, u.workspace_id, role=u.role),
            "token_type": "bearer",
            "expires_in": access_token_ttl_seconds(),
            "workspace_id": str(u.workspace_id), "role": u.role}


@router.get("/auth/me")
async def auth_me(user: CurrentUser = Depends(require_auth)):
    return {"user_id": str(user.user_id), "workspace_id": str(user.workspace_id), "role": user.role}
