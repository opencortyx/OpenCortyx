"""Default walkers: one per kind of source, each reading a file into the graph."""
from __future__ import annotations

from .base import BaseWalker
from .semistructured import SemiStructuredWalker
from .structured import StructuredWalker
from .unstructured import UnstructuredWalker

WALKERS: dict[str, type[BaseWalker]] = {
    StructuredWalker.kind: StructuredWalker,
    SemiStructuredWalker.kind: SemiStructuredWalker,
    UnstructuredWalker.kind: UnstructuredWalker,
}


def walker_for(kind: str) -> BaseWalker:
    """The default walker for `structured`, `semistructured` or `unstructured` sources."""
    try:
        return WALKERS[kind]()
    except KeyError:
        raise ValueError(f"no walker for kind {kind!r}") from None


__all__ = ["BaseWalker", "SemiStructuredWalker", "StructuredWalker", "UnstructuredWalker", "WALKERS",
           "walker_for"]
