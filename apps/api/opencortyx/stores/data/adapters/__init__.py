"""Live sources read through DuckDB without a copy: natively attached databases (attach.py) and the
Arrow-adapter contract for everything else (base.py)."""
from opencortyx.stores.data.adapters.base import Column, Filter, SourceAdapter, TableSchema

__all__ = ["Column", "Filter", "SourceAdapter", "TableSchema"]
