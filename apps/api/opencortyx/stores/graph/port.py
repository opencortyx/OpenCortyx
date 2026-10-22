"""The graph store contract. `core.substrate.NodeRepo` is its only caller."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from opencortyx.stores.graph.models import (
    Edge, EdgeQuery, EdgeWrite, Neighbor, NeighborGroup, Node, NodeQuery, Path,
)


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
