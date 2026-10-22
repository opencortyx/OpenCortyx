"""The layout AST every document reader produces, whatever the format.

Elements carry layout labels (title, section_header, text, list_item, table, picture, caption,
code, page_header, page_footer, ...) and two graph edges:
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


#: Every element label a reader may emit. This is the label set the layout reader has written since
#: the first release (it was Docling's), kept as it was so the node types of existing graphs, the
#: ontology and the Mapper stay the same with the readers that replaced it.
LAYOUT_LABELS = (
    "caption", "chart", "checkbox_selected", "checkbox_unselected", "code", "document_index",
    "empty_value", "field_heading", "field_hint", "field_item", "field_key", "field_region",
    "field_value", "footnote", "form", "formula", "grading_scale", "handwritten_text",
    "key_value_region", "list_item", "marker", "page_footer", "page_header", "paragraph", "picture",
    "reference", "section_header", "table", "text", "title",
)


def layout_element_types() -> frozenset:
    """Every node type the layout reader can write: the element labels, named the way the assembler
    names them."""
    return frozenset(to_pascal_case(label) for label in LAYOUT_LABELS)
