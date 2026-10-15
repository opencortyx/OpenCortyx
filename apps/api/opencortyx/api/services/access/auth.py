"""Services: sign-in, token refresh and first-run setup."""
import uuid

from sqlalchemy import func, select

from opencortyx.core.db import V2Session

DEMO_ORG = uuid.uuid5(uuid.NAMESPACE_DNS, "axiom-org:demo")   # the legacy dev workspace (continuity)


def access_token_ttl_seconds() -> int:
    """How long an access token is good for, read from the same settings that mint it."""
    from opencortyx.core.config import settings as _s
    return int(_s.JWT_EXPIRE_MINUTES) * 60


async def count_users() -> int:
    from opencortyx.domain.control import User
    async with V2Session() as db:
        return (await db.execute(select(func.count()).select_from(User))).scalar() or 0


async def materialize_for_mode(workspace_id, mode: str) -> None:
    """Best-effort: put the mode's default hierarchy on the graph (Person mode → Person, Team → Org)."""
    try:
        from opencortyx.ontology import build_rows, materialize_template
        if mode == "person":
            from opencortyx.ontology import (
                PERSON_EDGES,
                PERSON_HUB,
                PERSON_NODES,
            )
            await materialize_template(workspace_id, build_rows(PERSON_HUB, PERSON_NODES, PERSON_EDGES, source="person"), "person")
        elif mode == "team":
            from opencortyx.ontology import ORG_EDGES, ORG_HUB, ORG_NODES
            await materialize_template(workspace_id, build_rows(ORG_HUB, ORG_NODES, ORG_EDGES, source="organization"), "organization")
        from opencortyx.tasks.graph_tree import schedule_tree_index
        schedule_tree_index(str(workspace_id), now=True)   # so the starter template shows on the graph page
    except Exception:
        pass
