"""Routes: capability modules."""
from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.schemas.knowledge.capabilities import CapabilityUpdate
from opencortyx.domain.control import AuditLog
from opencortyx.core.security.auth import CurrentUser, require_admin, require_auth
from opencortyx.core.db import V2Session

router = APIRouter()


@router.get("/capabilities")
async def list_capabilities(user: CurrentUser = Depends(require_auth)):
    """Every registered capability (the platform tree) as UI/API-ready descriptors: identity, group,
    enable state, config schema + current values, hooks, and a deep link to where it is configured.
    Schema-driven so a future SDK capability appears automatically with zero UI code."""
    from opencortyx.capabilities import config as cap_config
    from opencortyx.core.config import runtime as rc
    await rc.load()
    return {"capabilities": await cap_config.describe_all(user.workspace_id)}


@router.put("/capabilities/{name}")
async def update_capability(name: str, spec: CapabilityUpdate,
                            user: CurrentUser = Depends(require_admin)):
    """Persist + apply one capability's config (enable toggle + schema fields). Only keys in the
    capability's own schema are honoured; values are stored in the shared runtime_setting store."""
    from opencortyx.capabilities import config as cap_config
    from opencortyx.core.config import runtime as rc
    before = rc.current()
    try:
        desc = await cap_config.apply(name, spec.values, user.workspace_id)
    except rc.InvalidSetting as exc:
        raise HTTPException(status_code=422, detail=f"Not saved — {exc}") from exc
    if desc is None:
        raise HTTPException(status_code=404, detail=f"unknown capability '{name}'")
    changed = rc.changes(before, rc.current(), (spec.values or {}).keys())
    if changed:
        async with V2Session() as db:
            db.add(AuditLog(workspace_id=user.workspace_id, actor=str(user.user_id),
                            action="capability/update", meta={"name": name, "changed": changed}))
            await db.commit()
    return {"status": "ok", "capability": desc}
