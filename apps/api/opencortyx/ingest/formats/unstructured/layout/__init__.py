"""Layout reading for documents: one reader per format, one AST for all of them.

`LayoutEngine.convert(content, filename)` picks the reader for the file (layout/readers/), which adds
the document's elements in reading order to a `LayoutAssembler`. Every reader emits the same labels,
so a document becomes the same kind of graph whatever its format.
"""
from .schemas import BoundingBox, DocumentASTNode, ElementMetadata, to_pascal_case
from .assembler import LayoutAssembler
from .engine import LayoutEngine

__all__ = [
    "DocumentASTNode",
    "BoundingBox",
    "ElementMetadata",
    "to_pascal_case",
    "LayoutAssembler",
    "LayoutEngine",
]
