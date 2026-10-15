"""Routes: graph views and graph queries."""
from fastapi import APIRouter, Depends, HTTPException

from opencortyx.api.deps import audit
from opencortyx.api.schemas.graph.graph import GraphQueryBody
from opencortyx.api.services.graph.graph import light_props, trim_props
from opencortyx.core.security.auth import CurrentUser, require_auth
from opencortyx.core.substrate import NodeRepo

router = APIRouter()
_repo = NodeRepo()


def _label(n) -> str:
    return (n.label or "").strip() or n.type


@router.get("/graph")
async def graph(user: CurrentUser = Depends(require_auth), limit: int = 0, light: bool = False):
    org = user.workspace_id
    total = await _repo.count(org)
    if limit > 0:
        nodes = await _repo.find(org, order_by="-created_at", limit=limit)
        ids = {n.id for n in nodes}
        touching = await _repo.edges(org, node_ids=list(ids)) if ids else []
        # a limited view also shows the far ends of the edges it touches
        missing = ({e.source_id for e in touching} | {e.target_id for e in touching}) - ids
        nodes += await _repo.get_many(org, list(missing))
    else:
        nodes = await _repo.find(org)
        touching = await _repo.edges(org) if nodes else []
    ids = {n.id for n in nodes}
    edges = [e for e in touching if e.source_id in ids and e.target_id in ids]

    deg: dict = {}
    for e in edges:
        deg[e.source_id] = deg.get(e.source_id, 0) + 1
        deg[e.target_id] = deg.get(e.target_id, 0) + 1

    props_of = light_props if light else trim_props
    return {
        "nodes": [{"id": str(n.id), "label": _label(n), "type": n.type, "entity_type": n.entity_type,
                   "level": n.level, "degree": deg.get(n.id, 0), "properties": props_of(n.properties)}
                  for n in nodes],
        "edges": [{"id": str(e.id), "source": str(e.source_id), "target": str(e.target_id),
                   "label": e.type, "relation": (e.properties or {}).get("relation")} for e in edges],
        "stats": {"nodes": len(nodes), "edges": len(edges)},
        "total": total, "truncated": total > len(nodes),
    }


@router.post("/graph/query")
async def graph_query_ep(body: GraphQueryBody, user: CurrentUser = Depends(require_auth)):
    """Execute a multi-stage graph traversal query (GRAPH_QUERY) over the OpenCortyx knowledge graph.

    Prerequisites & Primitives:
      • START: Seed from node label, query, id(s), or type.
      • TRAVERSE: Multi-hop graph expansion along edge types (direction: out, in, both).
      • FILTER: Attribute and property predicates (==, !=, >, <, contains, in).
      • MATCH: Neighbor pattern constraints.
      • INTERSECT: Set intersection with another sub-traversal (e.g. shared interests).
      • UNION: Set union with another sub-traversal.
      • PATH: Track and return full path trajectories.
      • LIMIT: Bound active nodes / paths.
      • RETURN: Project evidence, nodes, edges, paths, count.
    """
    from opencortyx.stores.graph_query import execute_graph_query, parse_graph_query

    try:
        spec = parse_graph_query(
            query=body.query,
            steps=body.steps,
            limit=body.limit,
            returns=body.returns,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    res = await execute_graph_query(user.workspace_id, spec, collection_id=body.collection_id)

    await audit(user.workspace_id, user.user_id, "graph/query",
                meta={"count": res.get("count", 0), "steps": len(spec.steps),
                       "time_ms": res.get("execution_time_ms")})
    return res
