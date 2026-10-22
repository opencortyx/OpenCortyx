"""The graph store contract. `core.substrate.NodeRepo` is its only caller."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from opencortyx.stores.graph.models import (
    Edge, EdgeQuery, EdgeWrite, Neighbor, NeighborGroup, Node, NodeQuery, Path,
)
from opencortyx.stores.graph.query import node_status


class GraphStore(ABC):
    provider_name: str = ""

    @abstractmethod
    async def initialize(self) -> None:
        """Create the schema if it does not exist. Safe to call on every start."""

    @abstractmethod
    async def health_check(self) -> bool: ...

    @abstractmethod
    async def describe(self) -> dict:
        """What this store IS, for the health dashboard: provider, version, and a one-line detail.

        Separate from `health_check` because they answer different questions. `health_check` is a
        boolean the application acts on; this is text an operator reads, and it is allowed to be
        slower and to return partial information rather than raise.
        """


    @abstractmethod
    async def close(self) -> None: ...

    # nodes
    @abstractmethod
    async def upsert_nodes(self, nodes: Sequence[Node]) -> None:
        """Write whole records; a record with an existing id replaces it."""

    @abstractmethod
    async def get_nodes(self, workspace_id, ids: Sequence) -> list[Node]:
        """The nodes of `workspace_id` among `ids`, in no particular order."""

    @abstractmethod
    async def find_nodes(self, workspace_id, query: NodeQuery) -> list[Node]: ...

    async def find_node_ids(self, workspace_id, query: NodeQuery) -> list[str]:
        """The ids `find_nodes` would return, without loading the nodes. A store that can answer from
        its indexes alone overrides this: a job that only needs to know WHICH nodes (is it embedded?)
        should not pull every node's text through the store's memory to find out."""
        return [str(n.id) for n in await self.find_nodes(workspace_id, query)]

    async def node_outlines(self, workspace_id) -> list[Node]:
        """Every node of the workspace with only what an overview of the whole graph needs: id, type,
        label and provenance pointer, and in `properties` only `__base_type` and `__status`. No text,
        no other properties, no degree. A store that can project columns overrides this, so the
        overview never moves every node's text through the store's memory."""
        out = []
        for n in await self.find_nodes(workspace_id, NodeQuery()):
            props = n.properties or {}
            out.append(Node(id=n.id, workspace_id=n.workspace_id, type=n.type, label=n.label,
                            source_ref=n.source_ref,
                            properties={"__base_type": props.get("__base_type") or n.type,
                                        "__status": node_status(n)}))
        return out

    async def incident_edge_counts(self, workspace_id, node_ids: Sequence) -> dict[str, int]:
        """How many edges of each type touch these nodes, an edge counted once per end among them.
        Only the counts: no edge or node is read in full. A store may cap a node with very many edges
        (a day's Date node, a large document) at a few thousand of them."""
        seeds = {str(n) for n in node_ids}
        counts: dict[str, int] = {}
        for e in await self.find_edges(workspace_id, EdgeQuery(node_ids=list(seeds))):
            counts[e.type] = counts.get(e.type, 0) + (str(e.source_id) in seeds) + (str(e.target_id) in seeds)
        return counts

    async def edge_ends(self, workspace_id, exclude_types: Sequence[str] = ()) -> list[tuple[str, str]]:
        """(source id, target id) of every edge whose type is not excluded, without the edges'
        properties."""
        edges = await self.find_edges(workspace_id, EdgeQuery(exclude_types=list(exclude_types) or None))
        return [(str(e.source_id), str(e.target_id)) for e in edges]

    @abstractmethod
    async def count_nodes(self, workspace_id, query: NodeQuery | None = None) -> int: ...

    @abstractmethod
    async def count_nodes_by_type(self, workspace_id) -> dict[str, int]: ...

    @abstractmethod
    async def add_to_degree(self, workspace_id, deltas: dict) -> None:
        """Add `deltas[node_id]` to each node's degree."""

    @abstractmethod
    async def delete_nodes(self, workspace_id, ids: Sequence) -> int:
        """Delete nodes and their edges, keeping neighbours' degrees right. Returns nodes deleted."""

    # edges
    @abstractmethod
    async def upsert_edges(self, edges: Sequence[Edge], owned=None) -> EdgeWrite:
        """Write edges whose endpoints both exist in the edge's organization.

        A new edge adds one to each endpoint's degree; rewriting an existing one does not. `owned`:
        ids of vertices only this write touches right now (the elements a document upload just
        created), whose degree a store may update in bulk rather than one atomic increment each."""

    @abstractmethod
    async def find_edges(self, workspace_id, query: EdgeQuery) -> list[Edge]: ...

    @abstractmethod
    async def count_edges(self, workspace_id) -> int: ...

    @abstractmethod
    async def count_edges_by_type(self, workspace_id) -> dict[str, int]: ...

    @abstractmethod
    async def delete_edges(self, workspace_id, edges: Sequence[Edge]) -> int:
        """Delete these edges, keeping endpoint degrees right. Returns edges deleted."""

    # traversal
    @abstractmethod
    async def neighbors(self, workspace_id, node_ids: Sequence, *, edge_types: Sequence[str] | None = None,
                        direction: str = "out", limit: int = 200) -> list[Neighbor]:
        """One hop from `node_ids`; `direction` is out, in or both."""

    @abstractmethod
    async def neighbor_groups(self, workspace_id, node_id, *,
                              exclude_edge_types: Sequence[str] = ()) -> list[NeighborGroup]:
        """One node's relationships counted by (edge type, direction, far node type).

        Counted by the store, never by loading the neighbours: this is how a node with a million
        relationships is opened, so its cost must not grow with what it summarises beyond the scan."""

    @abstractmethod
    async def neighbor_page(self, workspace_id, node_id, *, edge_type: str, direction: str, node_type: str,
                            offset: int = 0, limit: int = 100) -> list[Neighbor]:
        """One group of `neighbor_groups`, a page at a time, in a stable order (far node id)."""

    @abstractmethod
    async def shortest_paths(self, workspace_id, source_id, target_id, *, max_depth: int = 3,
                             limit: int = 10) -> list[Path]: ...

    @abstractmethod
    async def clear_org(self, workspace_id) -> int:
        """Delete every node and edge of an organization. Returns nodes deleted."""
