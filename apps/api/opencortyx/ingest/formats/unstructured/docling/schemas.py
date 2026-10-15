"""Pydantic and data schemas for IBM Docling Layout AST.

Preserves exact labels detected by IBM Docling (title, section_header, paragraph, table,
picture, formula, code, caption, list_item, etc.) and provides dual graph edges:
1. parent_id: hierarchical containment under the enclosing heading/section.
2. next_id: linear reading order sequence connecting elements in reading flow.
"""
from __future__ import annotations

import re
from typing import Any, Optional
from pydantic import BaseModel, Field


def to_pascal_case(label: str) -> str:
    """Convert snake_case or spaced label into PascalCase Graph Node Type.

    Examples:
        'section_header' -> 'SectionHeader'
        'page_header'    -> 'PageHeader'
        'table'          -> 'Table'
        'picture'        -> 'Picture'
        'list_item'      -> 'ListItem'
    """
    clean = re.sub(r"[^a-zA-Z0-9_]+", " ", str(label or "element")).strip()
    parts = clean.replace("_", " ").split()
    if not parts:
        return "Element"
    return "".join(p.capitalize() for p in parts)


class BoundingBox(BaseModel):
    x_min: float = 0.0
    y_min: float = 0.0
    x_max: float = 0.0
    y_max: float = 0.0


class ElementMetadata(BaseModel):
    page_number: int = 1
    bounding_box: BoundingBox = Field(default_factory=BoundingBox)


class DocumentASTNode(BaseModel):
    id: str
    label: str
    type: str
    text: str = ""
    level: Optional[int] = None
    parent_id: Optional[str] = None
    next_id: Optional[str] = None
    metadata: ElementMetadata = Field(default_factory=ElementMetadata)
    grid: Optional[list[list[str]]] = None
    asset_reference: Optional[str] = None

    @property
    def node_type(self) -> str:
        return self.type

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "text": self.text,
            "level": self.level,
            "parent_id": self.parent_id,
            "next_id": self.next_id,
            "metadata": {
                "page_number": self.metadata.page_number,
                "bounding_box": self.metadata.bounding_box.model_dump(),
            },
            "grid": self.grid,
            "asset_reference": self.asset_reference,
        }


def layout_element_types() -> frozenset:
    """Every node type the layout reader can write: Docling's element labels, named the way the
    assembler names them. Read from Docling itself so the list follows the library it describes."""
    try:
        from docling_core.types.doc import DocItemLabel
        return frozenset(to_pascal_case(label.value) for label in DocItemLabel)
    except Exception:                                   # noqa: BLE001 — docling not installed here
        return frozenset({"Title", "SectionHeader", "Text", "ListItem", "Table", "Picture",
                          "Caption", "Footnote", "PageHeader", "PageFooter", "Paragraph"})
