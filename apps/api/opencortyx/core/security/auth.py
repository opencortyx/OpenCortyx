"""Self-contained auth for v2 (no v1 import).

JWT (jose) + API-key (against v2's own `control.ApiKey`). Same `CurrentUser` shape the v2
router already expects.
"""
import base64
import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opencortyx.core.config import settings
from opencortyx.core.db import get_db

bearer_scheme = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass
class CurrentUser:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    role: str
    actor_type: str
    permissions: list


async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer_scheme),
    api_key: Optional[str] = Security(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> CurrentUser:
    if credentials:
        return _verify_jwt(credentials.credentials)
    if api_key:
        return await _verify_api_key(api_key, db)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Authentication required. Provide Bearer token or X-API-Key header.")


TOKEN_ACCESS = "access"
TOKEN_REFRESH = "refresh"


def create_access_token(user_id: uuid.UUID, workspace_id: uuid.UUID, role: str = "admin",
                        minutes: Optional[int] = None) -> str:
    """Mint a JWT that `_verify_jwt` accepts. Used by the /auth/token and /auth/refresh endpoints."""
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes or settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "workspace_id": str(workspace_id), "role": role,
               "typ": TOKEN_ACCESS, "exp": exp}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(user_id: uuid.UUID, workspace_id: uuid.UUID, days: Optional[int] = None) -> str:
    """Mint the long-lived token `/auth/refresh` exchanges for a new access token.

    It carries no role. Roles change — a user can be demoted or deactivated — so the refresh
    endpoint re-reads the user and mints the new access token from what is true NOW, rather than
    letting a token issued days ago keep asserting the permissions its holder had back then.
    """
    exp = datetime.now(timezone.utc) + timedelta(days=days or settings.JWT_REFRESH_EXPIRE_DAYS)
    payload = {"sub": str(user_id), "workspace_id": str(workspace_id), "typ": TOKEN_REFRESH, "exp": exp}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_refresh_token(token: str) -> tuple[uuid.UUID, uuid.UUID]:
    """`(user_id, workspace_id)` from a refresh token, or 401. Rejects an access token outright."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    if payload.get("typ") != TOKEN_REFRESH:
        raise HTTPException(status_code=401, detail="Not a refresh token")
    sub, org = payload.get("sub"), payload.get("workspace_id")
    if not sub or not org:
        raise HTTPException(status_code=401, detail="Invalid refresh token payload")
    return uuid.UUID(sub), uuid.UUID(org)


def _verify_jwt(token: str) -> CurrentUser:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id = payload.get("sub")
        workspace_id = payload.get("workspace_id")
        role = payload.get("role", "viewer")
        if not user_id or not workspace_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        # A refresh token is not a credential for calling the API. Tokens minted before `typ`
        # existed carry none, and stay acceptable — only an explicit refresh token is refused.
        if payload.get("typ") == TOKEN_REFRESH:
            raise HTTPException(status_code=401, detail="Refresh token cannot be used as an access token")
        perms = ["read", "write"] if role in ("editor", "admin") else ["read"]
        return CurrentUser(user_id=uuid.UUID(user_id), workspace_id=uuid.UUID(workspace_id),
                           role=role, actor_type="user", permissions=perms)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


async def _verify_api_key(raw_key: str, db: AsyncSession) -> CurrentUser:
    from opencortyx.domain import ApiKey
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = (await db.execute(
        select(ApiKey).where(ApiKey.hash == key_hash, ApiKey.revoked_at.is_(None)))).scalar_one_or_none()
    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    scopes = key.scopes if isinstance(key.scopes, dict) else {}
    return CurrentUser(
        user_id=key.id, workspace_id=key.workspace_id, role=scopes.get("role") or "agent", actor_type="agent",
        permissions=scopes.get("permissions", ["read"]))


def require_role(*roles: str):
    def _check(user: CurrentUser = Depends(require_auth)) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail=f"One of these roles required: {roles}")
        return user
    return _check


# ── real password auth (stdlib pbkdf2 — no extra dependency) ─────────────────
_PBKDF2_ITER = 240_000


def hash_password(password: str) -> str:
    """Return a self-describing hash: pbkdf2$<iter>$<salt_b64>$<hash_b64>."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITER)
    return f"pbkdf2${_PBKDF2_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(dk, base64.b64decode(hash_b64))
    except Exception:
        return False


# `viewer` is read-only — block it from mutating endpoints.
WRITE_ROLES = ("admin", "editor")


def require_write(user: CurrentUser = Depends(require_auth)) -> CurrentUser:
    # A user writes only with a writing role; an API key by its granted permissions. Checking for
    # "viewer" alone let any other role string (a typo, a legacy value) write.
    allowed = "write" in (user.permissions or []) if user.actor_type == "agent" else user.role in WRITE_ROLES
    if not allowed:
        raise HTTPException(status_code=403, detail="Read-only access: this role cannot modify data.")
    return user


# admin-only dependency (user/key management, settings)
require_admin = require_role("admin")
