"""Mentions resolved by name: an entity an extractor read out of text, linked to the published record
it names.

Identity resolution (`engine.py`) joins records of ONE type that share an identity key. A mention is
different: "Alice Smith" read from a support ticket is a Person to the extractor and a Customer to the
CRM, and carries no customer id. What the two share is the name - so a mention whose normalised label
names exactly one published record (`knowledge/semantic/publish.py` labels each record by its name)
is drawn SAME_AS that record. Two records with the same name are not guessed between: the mention
stays unlinked, as it would for a person reading it.
"""
from __future__ import annotations

from opencortyx.core.observability import get_logger

log = get_logger(__name__)
#: The edge from a mention to the record it names - the resolution engine's own "same thing".
SAME_AS = "SAME_AS"


def _mention(node) -> bool:
    props = node.properties or {}
    return props.get("__identity") == "entity" and bool(str(node.label or "").strip())


async def _published_types(workspace_id) -> list[str]:
    from opencortyx.core.config import runtime as rc
    from opencortyx.knowledge.semantic import store
    everything = rc.knowledge_publish()
    return [t.name for t in (await store.load(workspace_id)).accepted().types
            if t.kind == "entity" and t.keys and (t.materialize or everything)]


async def link_mentions(workspace_id, nodes, *, repo=None) -> int:
    """Draw SAME_AS from each extracted entity in `nodes` to the one published record its name names.
    Returns the edges drawn. Best-effort: a failure leaves the mentions unlinked, never the caller failed."""
    cands = [n for n in nodes or [] if _mention(n)]
    if not cands:
        return 0
    try:
        from opencortyx.core.substrate import NodeRepo
        from opencortyx.stores.graph.query import normalize_name
        types = await _published_types(workspace_id)
        if not types:
            return 0
        repo = repo or NodeRepo()
        edges, seen = [], set()
        for n in cands:
            key = normalize_name(n.label)
            if not key or (key, n.id) in seen:
                continue
            seen.add((key, n.id))
            named = [r for r in await repo.find(workspace_id, exact_types=types, name_key=key, limit=2)
                     if r.id != n.id]
            if len(named) == 1:
                edges.append({"source_id": n.id, "target_id": named[0].id, "type": SAME_AS,
                              "properties": {"by": "name", "__by": "resolution.mentions"}})
        if edges:
            await repo.add_edges(workspace_id=workspace_id, edges=edges, _quarantine=False)
        log.info("resolution.mentions", workspace_id=str(workspace_id), mentions=len(cands), linked=len(edges))
        return len(edges)
    except Exception as e:                                    # noqa: BLE001
        log.warning("resolution.mentions_failed", workspace_id=str(workspace_id), error=str(e)[:200])
        return 0


#: Mentions re-checked after a publish, at most (the most recent first).
RELINK_LIMIT = 20_000


async def relink(workspace_id) -> int:
    """Re-check the names already read from text against the records just published: a document
    read before its customers were published names them too. Returns the links drawn."""
    from opencortyx.core.substrate import NodeRepo
    repo = NodeRepo()
    nodes = await repo.find(workspace_id, props={"__identity": "entity"}, order_by="-created_at", limit=RELINK_LIMIT)
    return await link_mentions(workspace_id, nodes, repo=repo)
