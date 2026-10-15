"""Domain exceptions for graph storage operations.

Provider-specific exceptions (NebulaGraph IOError, SessionException, asyncpg, psycopg,
Neo4j DriverError, etc.) MUST NOT leak past adapter boundaries. All adapters translate
underlying driver errors into these domain exception types.

Every type here is raised by an adapter today. Types that were declared for failures no adapter
actually produced have been removed: a taxonomy nobody raises is read as a description of the
system and is wrong about it. Add one when you have a driver error that does not fit the ones here,
not in anticipation of one.
"""
from __future__ import annotations


class GraphStoreError(Exception):
    """Base exception for all graph store errors."""

    def __init__(self, message: str, provider: str = "", original_error: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.original_error = original_error

    def __str__(self) -> str:
        prov = f"[{self.provider}] " if self.provider else ""
        orig = f" (caused by: {type(self.original_error).__name__}: {self.original_error})" if self.original_error else ""
        return f"{prov}{self.message}{orig}"


class GraphStoreConnectionError(GraphStoreError):
    """Raised when connection to the graph database fails or is lost."""
    pass


class GraphStoreValidationError(GraphStoreError):
    """Raised when input nodes, edges, or traversal specifications fail validation."""
    pass


class GraphStoreOperationError(GraphStoreError):
    """Raised when a graph write, read, or traversal query fails internally."""
    pass
