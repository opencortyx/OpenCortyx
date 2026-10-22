"""In-process graph store for tests. Same semantics as the NebulaGraph adapter."""
from __future__ import annotations
from opencortyx.stores.graph.exceptions import GraphStoreValidationError

import copy
from collections import deque
from typing import Sequence

from opencortyx.stores.graph import query as q
from opencortyx.stores.graph.models import (
    Edge, EdgeQuery, EdgeWrite, Neighbor, NeighborGroup, Node, NodeQuery, Path,
)
from opencortyx.stores.graph.port import GraphStore


class MemoryGraphStore(GraphStore):
    provider_name = "memory"

    def __init__(self):
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._degree: dict[str, int] = {}

    async def initialize(self) -> None:
        return None

    async def health_check(self) -> bool:
        return True

    async def describe(self) -> dict:
        return {"provider": self.provider_name, "version": "", "detail": "in-process, not durable"}

    async def close(self) -> None:
        return None

    def _node(self, node: Node) -> Node:
        out = copy.deepcopy(node)
        out.degree = max(0, self._degree.get(str(node.id), 0))
        return out

    def _org_nodes(self, workspace_id):
        return [n for n in self._nodes.values() if str(n.workspace_id) == str(workspace_id)]

    async def upsert_nodes(self, nodes: Sequence[Node]) -> None:
        for n in nodes:
            self._nodes[str(n.id)] = copy.deepcopy(n)

    async def get_nodes(self, workspace_id, ids: Sequence) -> list[Node]:
        out = []
        for i in dict.fromkeys(str(x) for x in ids if x is not None):
            n = self._nodes.get(i)
            if n is not None and str(n.workspace_id) == str(workspace_id):
                out.append(self._node(n))
        return out

    async def find_nodes(self, workspace_id, query: NodeQuery) -> list[Node]:
        return q.select_nodes([self._node(n) for n in self._org_nodes(workspace_id)], query)

    async def count_nodes(self, workspace_id, query: NodeQuery | None = None) -> int:
        query = query or NodeQuery()
        return len(q.select_nodes([self._node(n) for n in self._org_nodes(workspace_id)],
                                  NodeQuery(**{**query.__dict__, "limit": None, "order_by": None})))

    async def count_nodes_by_type(self, workspace_id) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in self._org_nodes(workspace_id):
            out[n.type] = out.get(n.type, 0) + 1
        return out

    async def add_to_degree(self, workspace_id, deltas: dict) -> None:
        for nid, d in deltas.items():
            self._degree[str(nid)] = self._degree.get(str(nid), 0) + int(d)

    async def delete_nodes(self, workspace_id, ids: Sequence) -> int:
        gone = {str(n.id) for n in await self.get_nodes(workspace_id, ids)}
        incident = [e for e in self._edges.values()
                    if str(e.source_id) in gone or str(e.target_id) in gone]
        await self.delete_edges(workspace_id, incident)
        for i in gone:
            self._nodes.pop(i, None)
            self._degree.pop(i, None)
        return len(gone)

    async def upsert_edges(self, edges: Sequence[Edge], owned=None) -> EdgeWrite:
        result = EdgeWrite()
        for e in {str(e.id): e for e in edges}.values():
            src, dst = self._nodes.get(str(e.source_id)), self._nodes.get(str(e.target_id))
            if not src or not dst or str(src.workspace_id) != str(e.workspace_id) or str(dst.workspace_id) != str(e.workspace_id):
                result.refused.append(e)
                continue
            if str(e.id) in self._edges:
                e.created_at = self._edges[str(e.id)].created_at or e.created_at
                result.updated.append(e)
            else:
                result.created.append(e)
                await self.add_to_degree(e.workspace_id, {str(e.source_id): 1, str(e.target_id): 1})
            self._edges[str(e.id)] = copy.deepcopy(e)
        return result

    async def find_edges(self, workspace_id, query: EdgeQuery) -> list[Edge]:
        out = [copy.deepcopy(e) for e in self._edges.values()
               if str(e.workspace_id) == str(workspace_id) and q.edge_matches(e, query)]
        return out[:query.limit] if query.limit is not None else out

    async def count_edges(self, workspace_id) -> int:
        return sum(1 for e in self._edges.values() if str(e.workspace_id) == str(workspace_id))

    async def count_edges_by_type(self, workspace_id) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self._edges.values():
            if str(e.workspace_id) == str(workspace_id):
                out[e.type] = out.get(e.type, 0) + 1
        return out

    async def delete_edges(self, workspace_id, edges: Sequence[Edge]) -> int:
        deleted = 0
        for e in edges:
            held = self._edges.get(str(e.id))
            if held is None or str(held.workspace_id) != str(workspace_id):
                continue
            del self._edges[str(e.id)]
            await self.add_to_degree(workspace_id, {str(held.source_id): -1, str(held.target_id): -1})
            deleted += 1
        return deleted

    async def neighbors(self, workspace_id, node_ids: Sequence, *, edge_types: Sequence[str] | None = None,
                        direction: str = "out", limit: int = 200) -> list[Neighbor]:
        if direction not in ("out", "in", "both"):
            raise GraphStoreValidationError(
                f"direction must be out, in or both, not {direction!r}", provider="memory")
        seeds = {str(i) for i in node_ids}
        out = []
        for e in self._edges.values():
            if str(e.workspace_id) != str(workspace_id) or (edge_types and e.type not in edge_types):
                continue
            hops = []
            if direction in ("out", "both") and str(e.source_id) in seeds:
                hops.append((e.target_id, "out"))
            if direction in ("in", "both") and str(e.target_id) in seeds:
                hops.append((e.source_id, "in"))
            for far, way in hops:
                node = self._nodes.get(str(far))
                if node is not None and str(node.workspace_id) == str(workspace_id):
                    out.append(Neighbor(edge=copy.deepcopy(e), node=self._node(node), direction=way))
        return out[:limit]

    def _hops(self, workspace_id, node_id) -> list[Neighbor]:
        """Every relationship of one node, both directions; a self-loop counts once, as out."""
        me = str(node_id)
        out = []
        for e in self._edges.values():
            if str(e.workspace_id) != str(workspace_id):
                continue
            if str(e.source_id) == me:
                far, way = str(e.target_id), "out"
            elif str(e.target_id) == me:
                far, way = str(e.source_id), "in"
            else:
                continue
            node = self._nodes.get(far)
            if node is not None and str(node.workspace_id) == str(workspace_id):
                out.append(Neighbor(edge=copy.deepcopy(e), node=self._node(node), direction=way))
        return out

    async def neighbor_groups(self, workspace_id, node_id, *,
                              exclude_edge_types: Sequence[str] = ()) -> list[NeighborGroup]:
        counts: dict[tuple[str, str, str], int] = {}
        for h in self._hops(workspace_id, node_id):
            if h.edge.type in exclude_edge_types:
                continue
            key = (h.edge.type, h.direction, h.node.type)
            counts[key] = counts.get(key, 0) + 1
        return [NeighborGroup(edge_type=t, direction=d, node_type=n, count=c) for (t, d, n), c in counts.items()]

    async def neighbor_page(self, workspace_id, node_id, *, edge_type: str, direction: str, node_type: str,
                            offset: int = 0, limit: int = 100) -> list[Neighbor]:
        hops = [h for h in self._hops(workspace_id, node_id)
                if h.edge.type == edge_type and h.direction == direction and h.node.type == node_type]
        hops.sort(key=lambda h: (str(h.node.id), str(h.edge.id)))
        return hops[offset:offset + limit]

    async def shortest_paths(self, workspace_id, source_id, target_id, *, max_depth: int = 3,
                             limit: int = 10) -> list[Path]:
        start, goal = str(source_id), str(target_id)
        adjacency: dict[str, list[tuple[str, Edge]]] = {}
        for e in self._edges.values():
            if str(e.workspace_id) == str(workspace_id):
                adjacency.setdefault(str(e.source_id), []).append((str(e.target_id), e))
                adjacency.setdefault(str(e.target_id), []).append((str(e.source_id), e))
        frontier: deque[tuple[str, list[str], list[Edge]]] = deque([(start, [start], [])])
        best, paths = None, []
        while frontier:
            here, nodes, edges = frontier.popleft()
            if best is not None and len(edges) > best:
                break
            if here == goal and edges:
                best = len(edges)
                paths.append(Path(node_ids=[q.as_uuid(n) for n in nodes], edges=list(edges)))
                continue
            if len(edges) >= max_depth:
                continue
            for far, e in adjacency.get(here, []):
                if far not in nodes:
                    frontier.append((far, nodes + [far], edges + [e]))
        return paths[:limit]

    async def clear_org(self, workspace_id) -> int:
        ids = [n.id for n in self._org_nodes(workspace_id)]
        return await self.delete_nodes(workspace_id, ids)
