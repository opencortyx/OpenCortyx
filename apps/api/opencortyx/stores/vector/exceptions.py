"""Domain exceptions for vector storage operations.

Provider-specific exceptions (Qdrant, asyncpg, psycopg, Milvus, etc.) MUST NOT
leak past adapter boundaries. All adapters translate underlying driver errors
into these domain exception types.

Every type here is raised by an adapter today. Types that were declared for failures no adapter
actually produced have been removed: a taxonomy nobody raises is read as a description of the
system and is wrong about it. Add one when you have a driver error that does not fit the ones here,
not in anticipation of one.
"""
from __future__ import annotations


class VectorStoreError(Exception):
    """Base exception for all vector store errors."""

    def __init__(self, message: str, provider: str = "", original_error: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.original_error = original_error

    def __str__(self) -> str:
        prov = f"[{self.provider}] " if self.provider else ""
        orig = f" (caused by: {type(self.original_error).__name__}: {self.original_error})" if self.original_error else ""
        return f"{prov}{self.message}{orig}"


class VectorStoreValidationError(VectorStoreError):
    """Raised when input records, queries, or dimensions fail validation."""
    pass


class VectorStoreOperationError(VectorStoreError):
    """Raised when a write, search, or deletion operation fails internally."""
    pass
