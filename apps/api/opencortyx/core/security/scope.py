"""Access scope: which collections a caller may read, and up to what sensitivity."""
import uuid

from opencortyx.domain.control import SENSITIVITY_ORDER, Policy


async def resolve_scope(workspace_id: uuid.UUID, groups: list | None = None):
    """Return (allowed_collection_ids | None, max_sensitivity) for a caller.

    Default (no policies) = access to all of the org's collections at 'normal'. As soon as a
    policy exists for the caller's groups, it constrains scope.
    """
    from opencortyx.core.db import V2Session
    from sqlalchemy import select
    if not groups:
        return None, "normal"
    async with V2Session() as db:
        rows = (await db.execute(
            select(Policy).where(Policy.workspace_id == workspace_id, Policy.group.in_(groups)))).scalars().all()
    if not rows:
        return None, "normal"
    colls, max_sens = set(), 0
    allow_all = False
    for p in rows:
        max_sens = max(max_sens, SENSITIVITY_ORDER.get(p.max_sensitivity, 1))
        if p.collection_id is None:
            allow_all = True
        else:
            colls.add(p.collection_id)
    sens_name = next((k for k, v in SENSITIVITY_ORDER.items() if v == max_sens), "normal")
    return (None if allow_all else list(colls)), sens_name
