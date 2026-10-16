"""Dynamic AST Structural Assembler for IBM Docling.

Preserves all detected Docling labels without coercion, maintains an active heading hierarchy
stack for parent_id resolution, and links every sequential element via next_id.
"""
from __future__ import annotations

from typing import Any, Optional

from .schemas import BoundingBox, DocumentASTNode, ElementMetadata, to_pascal_case


class DynamicDoclingAssembler:
    """Assembles a flat stream of Docling document elements into a dual-edge AST.
    
    1. parent_id: Enclosing structural heading/section in the hierarchy tree.
    2. next_id: Direct subsequent element in reading order flow.
    """

    def __init__(self, doc_id: str = "", document_id: str = ""):
        self.doc_id = doc_id or document_id or "doc"
        self.heading_stack: list[tuple[int, str]] = []  # [(heading_level, node_id)]
        self.nodes: list[DocumentASTNode] = []
        self._seq = 0
        #: "docling" or "fallback" once converted, and why the fallback ran. Recorded on the Source
        #: so a graph whose shape differs from a colleague's can be traced to the engine that read it.
        self.engine: str = ""
        self.fallback_reason: str = ""

    def _next_id(self, page_no: int) -> str:
        self._seq += 1
        return f"{self.doc_id}:p{page_no}:{self._seq:05d}"

    def add_element(
        self,
        raw_label: str = "",
        text: str = "",
        page_no: int = 1,
        bbox: dict | BoundingBox | None = None,
        level: Optional[int] = None,
        grid: Optional[list[list[str]]] = None,
        asset_reference: Optional[str] = None,
        label: str = "",
    ) -> DocumentASTNode:
        """Add an element into the AST, resolving parent_id and next_id dynamically."""
        node_id = self._next_id(page_no)
        effective_label = raw_label or label or "paragraph"
        norm_label = str(effective_label).lower().strip()
        node_type = to_pascal_case(norm_label)

        # Parse bounding box
        if isinstance(bbox, BoundingBox):
            box_model = bbox
        elif isinstance(bbox, dict):
            box_model = BoundingBox(
                x_min=float(bbox.get("x_min", bbox.get("l", 0.0))),
                y_min=float(bbox.get("y_min", bbox.get("t", 0.0))),
                x_max=float(bbox.get("x_max", bbox.get("r", 0.0))),
                y_max=float(bbox.get("y_max", bbox.get("b", 0.0))),
            )
        else:
            box_model = BoundingBox()

        # Heading hierarchy resolution: title, section_header, or explicitly leveled element
        # Only headings open a section. Treating ANY element with a level as a heading let a leveled
        # list item or caption push itself onto the stack and adopt everything after it.
        is_heading = norm_label in ("title", "section_header", "heading")
        if is_heading:
            # Title is 0 so level-1 sections nest UNDER it rather than beside it.
            actual_level = level if level is not None else (0 if norm_label == "title" else 1)
        else:
            actual_level = None

        root_id = self.doc_id if self.doc_id else None
        if is_heading and actual_level is not None:
            # Pop all headings at same or deeper level
            while self.heading_stack and self.heading_stack[-1][0] >= actual_level:
                self.heading_stack.pop()

            parent_id = self.heading_stack[-1][1] if self.heading_stack else root_id
            self.heading_stack.append((actual_level, node_id))
        elif norm_label in ("page_header", "page_footer"):
            # A running header or footer belongs to the document, not to whichever section happens
            # to be open on that page.
            parent_id = root_id
        else:
            # Non-heading elements inherit the closest active heading
            parent_id = self.heading_stack[-1][1] if self.heading_stack else root_id
            actual_level = None

        node = DocumentASTNode(
            id=node_id,
            label=norm_label,
            type=node_type,
            text=str(text or "").strip(),
            level=actual_level,
            parent_id=parent_id,
            next_id=None,
            metadata=ElementMetadata(page_number=max(1, int(page_no or 1)), bounding_box=box_model),
            grid=grid,
            asset_reference=asset_reference,
        )

        # Stitch NEXT reading order edge from previous node
        if self.nodes:
            self.nodes[-1].next_id = node_id

        self.nodes.append(node)
        return node

    def to_dict_list(self) -> list[dict[str, Any]]:
        """Return the flat list array matching the mandated JSON AST schema."""
        return [node.to_dict() for node in self.nodes]

    def summary_stats(self) -> dict[str, Any]:
        """Compute structural breakdown of the assembled AST."""
        counts_by_type: dict[str, int] = {}
        for n in self.nodes:
            counts_by_type[n.type] = counts_by_type.get(n.type, 0) + 1
        return {
            "total_nodes": len(self.nodes),
            "counts_by_type": counts_by_type,
            "has_tables": counts_by_type.get("Table", 0) > 0,
            "has_images": counts_by_type.get("Picture", 0) > 0 or counts_by_type.get("Image", 0) > 0,
        }

    def build(self) -> list[DocumentASTNode]:
        """Return the assembled list of DocumentASTNode objects."""
        return self.nodes

    def get_unique_node_types(self) -> set[str]:
        """Return set of distinct PascalCase node types detected."""
        return {node.type for node in self.nodes}
