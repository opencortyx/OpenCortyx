"""Routes: runtime performance and retrieval settings."""
from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.schemas.system.config import ConfigUpdate
from opencortyx.api.services.access.auth import materialize_for_mode
from opencortyx.domain.control import AuditLog
from opencortyx.core.security.auth import CurrentUser, require_admin, require_auth
from opencortyx.core.db import V2Session

router = APIRouter()

#: Settings fields that belong to the caller's workspace, mapped to the profile field each one is.
_WORKSPACE_FIELDS = {"workspace_name": "name", "mode": "mode"}


async def _workspace_values(workspace_id) -> dict:
    from opencortyx.knowledge.workspace import get_profile
    p = await get_profile(workspace_id)
    return {"workspace_name": p.name, "mode": p.mode}


@router.get("/config")
async def get_config(user: CurrentUser = Depends(require_auth)):
    """Current runtime-tunable knobs (+ render schema) and read-only infra knobs for the Settings UI."""
    from opencortyx.core.config import runtime as rc
    await rc.load()
    readonly = rc.readonly_snapshot()
    # Which models run is chosen on the Models page, so report what the registry resolves — the env
    # values are only the seed for a workspace with no rows yet.
    from opencortyx.core.inference.registry import resolve_model
    gen = await resolve_model(user.workspace_id, "generation")
    emb = await resolve_model(user.workspace_id, "embedding")
    readonly.update({"llm_model": gen.get("model_id"), "embed_model": emb.get("model_id"),
                     "generation_endpoint": gen.get("base_url") or f"({gen.get('provider')} API)"})
    return {"tunable": {**rc.current(), **await _workspace_values(user.workspace_id)},
            "schema": rc.SCHEMA, "readonly": readonly}


@router.put("/config")
async def update_config(spec: ConfigUpdate, user: CurrentUser = Depends(require_admin)):
    """Persist + apply editable runtime knobs (parallelism / cache / abstain / permissions).
    Infra knobs (pool size, replica URL, embedding model) are ignored here — they need a restart."""
    from opencortyx.core.config import runtime as rc
    from opencortyx.knowledge.workspace import InvalidWorkspace, clean_mode, clean_name, save_profile
    values = dict(spec.values or {})
    ws = {_WORKSPACE_FIELDS[k]: values.pop(k) for k in list(values) if k in _WORKSPACE_FIELDS}
    ws = {k: v for k, v in ws.items() if v is not None}
    before = rc.current()
    try:
        # checked before anything is written, so one bad field saves nothing at all
        clean_name(ws.get("name")), clean_mode(ws.get("mode", "person"))
        updated = await rc.update(values)
        _, ws_changed = await save_profile(user.workspace_id, **ws) if ws else (None, {})
    except (rc.InvalidSetting, InvalidWorkspace) as exc:
        raise HTTPException(status_code=422, detail=f"Not saved — {exc}") from exc
    changed = rc.changes(before, updated, values.keys())
    changed.update({("workspace_name" if k == "name" else k): v for k, v in ws_changed.items()})
    if "mode" in ws_changed:                   # switching mode → put that hierarchy on the graph
        await materialize_for_mode(user.workspace_id, ws_changed["mode"]["to"])
    elif "name" in ws_changed:                 # the central node was relabelled: show it now
        from opencortyx.tasks.graph_tree import schedule_tree_index
        schedule_tree_index(str(user.workspace_id), now=True)
    updated = {**updated, **await _workspace_values(user.workspace_id)}
    if changed:
        async with V2Session() as db:
            db.add(AuditLog(workspace_id=user.workspace_id, actor=str(user.user_id), action="config/update",
                            meta={"changed": changed}))
            await db.commit()
    return {"status": "ok", "tunable": updated}
