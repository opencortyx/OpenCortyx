"""Default walker for documents."""
from __future__ import annotations

import mimetypes
import uuid
from typing import Any, Optional

from .base import BaseWalker


class UnstructuredWalker(BaseWalker):
    """Documents (PDF, Word, PowerPoint, Markdown, HTML, text, email, ...).

    The document is parsed, split into sections and chunks under its `Source` node (`CONTAINS`), and
    the pipeline's extraction steps run over the chunks. A bound mapping decides the reading rules;
    without one the default chunking applies.
    """

    kind = "unstructured"

    async def ingest_file(self, workspace_id: uuid.UUID, content: bytes, filename: str, *,
                          pipeline: Optional[list] = None, pipeline_id: Optional[str] = None,
                          collection_id: Optional[uuid.UUID] = None, mime_type: Optional[str] = None,
                          **options: Any) -> dict:
        from opencortyx.ingest.unstructured.ingest import ingest_document
        mime = mime_type or mimetypes.guess_type(filename)[0] or "text/plain"
        return await ingest_document(workspace_id, content, filename, mime, collection_id,
                                     pipeline=pipeline, **options)

    @classmethod
    def introspect(cls, text_or_bytes: str | bytes, filename: str = "", mime: str = "",
                   strategy: str = "auto", max_pages: Optional[int] = None) -> dict:
        """Introspect a document using the walker's parsing & chunking engine.

        Supports two primary strategies:
          - 'sliding' (or 'sliding_window'): standard character/token window chunking.
          - 'layout': the format's layout reader (layout/readers/): element labels, 2D table grids,
                      picture assets, and dual edges (hierarchy + NEXT flow).
        """
        from opencortyx.ingest.unstructured.chunking import chunk_document
        from opencortyx.ingest.formats.unstructured.documents import parse_content
        from opencortyx.ingest.formats.structured.tabular import table_name_of

        if isinstance(text_or_bytes, bytes):
            parsed = parse_content(text_or_bytes, mime_type=mime or "text/plain", filename=filename)
            text = str(parsed.get("raw_text") or "")
            word_count = parsed.get("word_count") or len(text.split())
            language = parsed.get("language") or "en"
        else:
            text = str(text_or_bytes or "")
            word_count = len(text.split())
            language = "en"

        table = table_name_of(filename or "document")
        root_node = {
            "nodeType": "document",
            "nodeLabel": "Document",
            "elementSource": "document",
            "attributes": [
                {"name": "title", "role": "id", "value": filename or "document", "basis": f"str: '{filename or 'document'}'"},
                {"name": "file_type", "role": "metadata", "value": mime or "text", "basis": f"str: '{mime or 'text'}'"},
                {"name": "char_count", "role": "measure", "value": str(len(text)), "basis": f"int: '{len(text)}'"},
                {"name": "word_count", "role": "measure", "value": str(word_count), "basis": f"int: '{word_count}'"},
                {"name": "language", "role": "metadata", "value": language, "basis": f"str: '{language}'"},
            ],
        }

        # ── 1. Layout strategy: the reader for the format ─────────────────────────────
        if strategy == "layout":
            from opencortyx.ingest.formats.unstructured.layout import LayoutEngine
            assembler = LayoutEngine.convert(text_or_bytes, filename=filename, max_pages=max_pages)
            ast_nodes = assembler.nodes

            # Collect dynamic discovered node types (no restricted enums)
            types_map: dict[str, list] = {}
            for n in ast_nodes:
                types_map.setdefault(n.type, []).append(n)

            dynamic_nodes = []
            for ntype, items in types_map.items():
                first_text = items[0].text[:60] if items[0].text else f"{ntype} element"
                attrs = [
                    {"name": "text", "role": "text", "value": f"{first_text}...", "basis": f"str: '{first_text}...'"},
                    {"name": "page_number", "role": "measure", "value": str(items[0].metadata.page_number), "basis": f"int: '{items[0].metadata.page_number}'"},
                ]
                if ntype in ("SectionHeader", "Title", "Heading") and items[0].level is not None:
                    attrs.append({"name": "level", "role": "measure", "value": str(items[0].level), "basis": f"int: '{items[0].level}'"})
                if ntype == "Table" and items[0].grid:
                    attrs.append({"name": "grid_rows", "role": "measure", "value": str(len(items[0].grid)), "basis": f"int: '{len(items[0].grid)}'"})
                if items[0].asset_reference:
                    attrs.append({"name": "asset_reference", "role": "metadata", "value": items[0].asset_reference, "basis": f"str: '{items[0].asset_reference}'"})

                dynamic_nodes.append({
                    "nodeType": items[0].label,
                    "nodeLabel": ntype,
                    "elementSource": items[0].label,
                    "count": len(items),
                    "attributes": attrs,
                })

            # Derive hierarchy edges and sequential NEXT edges
            edge_rules = []
            # Document root contains top-level headings/elements
            top_headers = [n for n in ast_nodes if n.type in ("SectionHeader", "Title", "Heading") and (n.parent_id is None or n.parent_id == assembler.doc_id)]
            if top_headers:
                edge_rules.append({
                    "type": "CONTAINS",
                    "fromNode": "Document",
                    "toNode": top_headers[0].type,
                    "edgeLabel": "CONTAINS",
                    "from": {"node": "Document", "ref": "id", "kind": "self"},
                    "to": {"node": top_headers[0].type, "ref": "parent_id", "kind": "context"},
                })

            # Enclosing headings contain their content nodes
            for ntype in types_map:
                if ntype not in ("SectionHeader", "Title", "Heading"):
                    from_node = "SectionHeader" if "SectionHeader" in types_map else "Document"
                    edge_rules.append({
                        "type": "CONTAINS",
                        "fromNode": from_node,
                        "toNode": ntype,
                        "edgeLabel": "CONTAINS",
                        "from": {"node": from_node, "ref": "id", "kind": "context"},
                        "to": {"node": ntype, "ref": "parent_id", "kind": "self"},
                    })

            # Sequential reading order NEXT edge
            if len(ast_nodes) > 1:
                edge_rules.append({
                    "type": "NEXT",
                    "fromNode": "Element",
                    "toNode": "Element",
                    "edgeLabel": "NEXT",
                    "from": {"node": "Element", "ref": "id", "kind": "self"},
                    "to": {"node": "Element", "ref": "next_id", "kind": "context"},
                })

            return {
                "table": table,
                "filename": filename,
                "char_count": len(text),
                "word_count": word_count,
                "strategy": "layout",
                "engine": assembler.engine,
                "fallback_reason": assembler.fallback_reason,
                "root": root_node,
                "types": [d["nodeLabel"] for d in dynamic_nodes],
                "dynamic_nodes": dynamic_nodes,
                "edge_rules": edge_rules,
                "ast_nodes": assembler.to_dict_list(),
                "node_count": len(ast_nodes),
            }

        # ── 2. Sliding Window Strategy ───────────────────────────────────────
        eff_strategy = "sliding_window"
        chunks = chunk_document(text, strategy="sliding_window", chunk_size=2000, overlap=200)
        if not chunks:
            chunks = [{"text": text[:2000], "attrs": {}}]

        headings = [c["attrs"]["heading"] for c in chunks if c.get("attrs", {}).get("heading")]

        sections = []
        if headings:
            sections.append({
                "nodeType": "section",
                "nodeLabel": "Section",
                "elementSource": "section",
                "edgeFrom": "Document",
                "edgeLabel": "HAS_SECTION",
                "count": len(set(headings)),
                "sample_heading": headings[0],
                "attributes": [
                    {"name": "heading", "role": "metadata", "value": headings[0], "basis": f"str: '{headings[0]}'"},
                    {"name": "level", "role": "measure", "value": "1", "basis": "int: '1'"},
                ],
            })

        first_chunk_text = chunks[0]["text"].replace("\n", " ").strip()[:60]
        chunk_node = {
            "nodeType": "chunk",
            "nodeLabel": "Chunk",
            "elementSource": "chunk",
            "edgeFrom": "Section" if headings else "Document",
            "edgeLabel": "CONTAINS_CHUNK",
            "count": len(chunks),
            "strategy": eff_strategy,
            "attributes": [
                {"name": "text", "role": "text", "value": f"{first_chunk_text}...", "basis": f"str: '{first_chunk_text}...'"},
                {"name": "ordinal", "role": "id", "value": "0", "basis": "int: '0'"},
                {"name": "char_count", "role": "measure", "value": str(len(chunks[0]["text"])), "basis": f"int: '{len(chunks[0]['text'])}'"},
            ],
        }

        return {
            "table": table,
            "filename": filename,
            "char_count": len(text),
            "word_count": word_count,
            "strategy": eff_strategy,
            "root": root_node,
            "sections": sections,
            "chunks": chunk_node,
            "headings": list(dict.fromkeys(headings)),
            "chunk_count": len(chunks),
        }
