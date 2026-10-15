"""The vector store contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from opencortyx.stores.vector.models import SparseVector, VectorFilter, VectorHit, VectorPoint


class VectorStore(ABC):
    provider_name: str = ""

    @abstractmethod
    async def initialize(self) -> None:
        """Create the collection and its indexes if they do not exist."""

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

    @abstractmethod
    async def upsert(self, points: Sequence[VectorPoint]) -> None:
        """Write points; a point with the same node and attribute replaces the old one."""

    @abstractmethod
    async def search_dense(self, vector: list[float], flt: VectorFilter, k: int) -> list[VectorHit]: ...

    @abstractmethod
    async def search_sparse(self, vector: SparseVector, flt: VectorFilter, k: int) -> list[VectorHit]: ...

    @abstractmethod
    async def update_fields(self, workspace_id, node_id, fields: dict) -> None:
        """Replace the search fields stored on every point of a node."""

    @abstractmethod
    async def scroll(self, flt: VectorFilter, *, limit: int, fields: Sequence[str]) -> list[dict]:
        """Payloads of up to `limit` matching points, in no particular order."""

    @abstractmethod
    async def embedded_nodes(self, workspace_id, node_ids: Sequence) -> set[str]:
        """The ids among `node_ids` whose attribute embeddings are current."""

    @abstractmethod
    async def count(self, flt: VectorFilter) -> int: ...

    @abstractmethod
    async def count_nodes(self, workspace_id) -> int:
        """Nodes of an organization whose attribute embeddings are current."""

    @abstractmethod
    async def attribute_values(self, workspace_id) -> dict[str, tuple[int, int]]:
        """Per attribute: how many points, and how many distinct texts."""

    @abstractmethod
    async def delete(self, flt: VectorFilter) -> None:
        """Delete every matching point."""

    @abstractmethod
    async def clear_org(self, workspace_id) -> None: ...
