"""Services: reading, authoring and searching nodes."""
import uuid

from opencortyx.api.services.graph.graph import trim_props
from opencortyx.core.substrate import NodeRepo

_BOOKKEEPING_EDGES = {"CREATED_ON", "MODIFIED_ON", "VALID_FROM", "VALID_TO", "INGESTED_ON"}
_SUBGRAPH_MAX_NODES = 2000


async def node_subgraph(workspace_id, node_id: uuid.UUID, hops: int = 1, focus: bool = False) -> dict:
    """A node and its N-hop subgraph, capped at `_SUBGRAPH_MAX_NODES`. `focus` skips time-bookkeeping
    edges, so the walk does not fan out through shared Date nodes."""
    repo = NodeRepo()
    core = await repo.get(workspace_id, node_id)
    if core is None:
        return {"status": "not_found"}

    nodes, edges = {core.id: core}, {}
    frontier = [core.id]
    for _ in range(max(1, min(hops, 20))):
        if not frontier or len(nodes) >= _SUBGRAPH_MAX_NODES:
            break
        hops_found = await repo.graph.neighbors(workspace_id, frontier, direction="both",
                                                limit=_SUBGRAPH_MAX_NODES - len(nodes) + len(frontier))
        frontier = []
        for hop in hops_found:
            if focus and (hop.edge.type or "").upper() in _BOOKKEEPING_EDGES:
                continue
            edges[hop.edge.id] = hop.edge
            if hop.node.id not in nodes:
                nodes[hop.node.id] = hop.node
                frontier.append(hop.node.id)
    edges = [e for e in edges.values() if e.source_id in nodes and e.target_id in nodes]

    deg: dict = {}
    for e in edges:
        deg[e.source_id] = deg.get(e.source_id, 0) + 1
        deg[e.target_id] = deg.get(e.target_id, 0) + 1

    return {
        "node": {
            "id": str(core.id),
            "label": (core.label or "").strip() or core.type,
            "type": core.type,
            "entity_type": core.entity_type,
            "level": core.level,
            "properties": core.properties or {},
        },
        "graph": {
            "nodes": [{
                "id": str(n.id),
                "label": (n.label or "").strip() or n.type,
                "type": n.type,
                "entity_type": n.entity_type,
                "level": n.level,
                "degree": deg.get(n.id, 0),
                "properties": trim_props(n.properties),
            } for n in nodes.values()],
            "edges": [{
                "source": str(e.source_id),
                "target": str(e.target_id),
                "label": e.type,
                "relation": (e.properties or {}).get("relation"),
            } for e in edges],
        },
    }
