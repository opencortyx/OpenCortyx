"""Assembles a reader's flat stream of elements into the layout AST.

Keeps every label a reader gives, maintains the open-heading stack for parent_id, and links every
element to the next in reading order (next_id). Readers only call `add_element` in reading order;
the tree, and so the graph, comes out the same whichever reader produced it.
"""
from __future__ import annotations

from typing import Any, Optional

from .schemas import BoundingBox, DocumentASTNode, ElementMetadata, to_pascal_case


class LayoutAssembler:
    """Assembles a flat stream of document elements into a dual-edge AST.
    
    1. parent_id: Enclosing structural heading/section in the hierarchy tree.
    2. next_id: Direct subsequent element in reading order flow.
    """

    def __init__(self, doc_id: str = "", document_id: str = ""):
        self.doc_id = doc_id or document_id or "doc"
        self.heading_stack: list[tuple[int, str]] = []  # [(heading_level, node_id)]
        self.nodes: list[DocumentASTNode] = []
        self._seq = 0
        #: The reader that read the document ("pdf", "docx", "markdown", ...), or "fallback", and why
        #: the fallback ran. Recorded on the Source so a graph whose shape differs from a colleague's
        #: can be traced to what read it.
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

    def to_markdown(self) -> str:
        """The document as Markdown: what the chunking path and previews read when they want text
        with its structure (headings as #, list items as -, tables as pipe tables)."""
        out: list[str] = []
        for n in self.nodes:
            if not n.text and not n.grid:
                continue
            if n.label == "title":
                out.append(f"# {n.text}")
            elif n.label == "section_header":
                out.append("#" * min(6, 1 + max(1, n.level or 1)) + f" {n.text}")
            elif n.label == "list_item":
                out.append(n.text if n.text[:2] in ("- ", "* ") else f"- {n.text}")
            elif n.label == "table" and n.grid:
                rows = [[str(c).replace("|", "\\|") for c in r] for r in n.grid if r]
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                out.append("\n".join(["| " + " | ".join(rows[0]) + " |",
                                       "|" + "---|" * width] + ["| " + " | ".join(r) + " |" for r in rows[1:]]))
            elif n.label == "code":
                out.append(f"```\n{n.text}\n```")
            elif n.label == "picture":
                out.append(f"![{n.text}]({n.asset_reference or ''})")
            else:
                out.append(n.text)
        return "\n\n".join(out)
