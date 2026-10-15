"""IBM Docling Document Engine.

Wraps IBM Docling DocumentConverter to extract document layout, tables (TableFormer),
figures, and reading order into the DynamicDoclingAssembler.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from typing import Any, Optional

from opencortyx.core.observability import get_logger
from .assembler import DynamicDoclingAssembler

log = get_logger(__name__)


class DoclingEngine:
    """Interface to IBM Docling layout conversion."""

    _converter_instance = None
    #: Why Docling is unavailable in this process, once found to be. Cached so a worker does not
    #: pay a failing model load on every document, and exposed so a run can say which engine read
    #: it instead of silently producing a differently-shaped graph.
    unavailable_reason: Optional[str] = None

    @classmethod
    def get_converter(cls):
        """Lazy-initialize the Docling DocumentConverter singleton, or None if it cannot load.

        ANY exception disables it, not only ImportError. The failures seen in practice are not
        import errors: docling >= 2.1xx raises AttributeError at import under pydantic < 2.10, and
        OpenCV raises ImportError('libGL.so.1') only on the first PDF. Catching one type let the
        other escape `convert()` and fail the whole ingest instead of reaching the fallback.
        """
        if cls._converter_instance is None and cls.unavailable_reason is None:
            try:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                from opencortyx.core.config import settings

                pdf = PdfPipelineOptions()
                pdf.do_ocr = bool(settings.LAYOUT_OCR)
                pdf.do_table_structure = bool(settings.LAYOUT_TABLE_STRUCTURE)
                cls._converter_instance = DocumentConverter(
                    format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pdf)})
            except Exception as e:                   # noqa: BLE001 — see docstring
                cls.unavailable_reason = f"{type(e).__name__}: {e}"[:300]
                log.warning("docling.unavailable", error=cls.unavailable_reason,
                            action="using the built-in structural parser")
        return cls._converter_instance

    @classmethod
    def convert(
        cls,
        content: bytes | str,
        filename: str = "document.pdf",
        doc_id: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> DynamicDoclingAssembler:
        """Convert a document (PDF, DOCX, XLSX, Markdown, HTML) into a dynamic AST.

        `max_pages` reads only the first pages — for a preview, which must not cost what reading a
        300-page PDF costs. Ingestion leaves it unset and reads everything."""
        if not doc_id:
            h = hashlib.sha256(content if isinstance(content, bytes) else content.encode("utf-8")).hexdigest()[:12]
            doc_id = f"doc_{h}"

        assembler = DynamicDoclingAssembler(doc_id=doc_id)
        filename = _effective_filename(content, filename)
        converter = cls.get_converter()

        if converter is not None:
            try:
                result = cls._convert_with_docling(converter, content, filename, assembler, max_pages)
                result.engine = "docling"
                return result
            except Exception as exc:                 # noqa: BLE001 — the fallback still reads it
                log.warning("docling.conversion_error_falling_back", filename=filename,
                            error=f"{type(exc).__name__}: {exc}"[:300])
                assembler = DynamicDoclingAssembler(doc_id=doc_id)   # discard partial elements
                assembler.fallback_reason = f"{type(exc).__name__}: {exc}"[:300]
        else:
            assembler.fallback_reason = cls.unavailable_reason or "docling is not installed"

        result = cls._convert_fallback(content, filename, assembler)
        result.engine = "fallback"
        return result

    @classmethod
    def _convert_with_docling(
        cls,
        converter: Any,
        content: bytes | str,
        filename: str,
        assembler: DynamicDoclingAssembler,
        max_pages: Optional[int] = None,
    ) -> DynamicDoclingAssembler:
        """Execute conversion using IBM Docling DocumentConverter."""
        suffix = os.path.splitext(filename or "")[1] or ".pdf"
        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content if isinstance(content, bytes) else content.encode("utf-8"))

            res = (converter.convert(temp_path, max_num_pages=max_pages) if max_pages
                   else converter.convert(temp_path))
            doc = res.document

            # Docling provides iterate_items() in reading order
            for item, _depth in doc.iterate_items():
                label = getattr(item.label, "value", str(item.label)).lower()
                text = getattr(item, "text", "") or ""

                # Provenance: page number and bounding box
                page_no = 1
                bbox_dict = {"x_min": 0.0, "y_min": 0.0, "x_max": 0.0, "y_max": 0.0}
                if hasattr(item, "prov") and item.prov:
                    p0 = item.prov[0]
                    page_no = getattr(p0, "page_no", 1)
                    b = getattr(p0, "bbox", None)
                    if b:
                        bbox_dict = {
                            "x_min": float(getattr(b, "l", getattr(b, "x_min", 0.0))),
                            "y_min": float(getattr(b, "t", getattr(b, "y_min", 0.0))),
                            "x_max": float(getattr(b, "r", getattr(b, "x_max", 0.0))),
                            "y_max": float(getattr(b, "b", getattr(b, "y_max", 0.0))),
                        }

                # Heading level. Docling numbers section headers from 1 and gives the title no
                # level; putting the title at 1 too made every top-level section its SIBLING, so the
                # title contained only the text before the first section. The title is 0.
                level = None
                if label == "title":
                    level = 0
                elif label == "section_header":
                    level = int(getattr(item, "level", None) or 1)

                # 2D Grid matrix for tables
                grid = None
                if label == "table":
                    grid = cls._extract_table_grid(item)
                    if not text.strip() and grid:
                        # Docling leaves TableItem.text empty. Without this a table is a node with
                        # no text: never embedded, never matched, invisible to retrieval.
                        text = _grid_text(grid)

                # Image asset reference
                asset_ref = None
                if label in ("picture", "image"):
                    asset_ref = cls._extract_image_asset(assembler.doc_id, item, doc)

                assembler.add_element(
                    raw_label=label,
                    text=text,
                    page_no=page_no,
                    bbox=bbox_dict,
                    level=level,
                    grid=grid,
                    asset_reference=asset_ref,
                )

            return assembler
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    @classmethod
    def _extract_table_grid(cls, table_item: Any) -> list[list[str]]:
        """Extract a 2D text matrix from Docling TableItem."""
        grid: list[list[str]] = []
        try:
            if hasattr(table_item, "data") and hasattr(table_item.data, "grid"):
                for row in table_item.data.grid:
                    grid.append([getattr(cell, "text", str(cell)).strip() for cell in row])
                return grid
            if hasattr(table_item, "export_to_dataframe"):
                df = table_item.export_to_dataframe()
                header = [str(c) for c in df.columns]
                rows = [[str(v) for v in row] for row in df.values.tolist()]
                return [header] + rows
        except Exception as exc:
            log.warning("docling.table_grid_error", error=str(exc))
        return grid

    @classmethod
    def _extract_image_asset(cls, doc_id: str, picture_item: Any, doc: Any) -> Optional[str]:
        """Extract image asset reference from Docling PictureItem."""
        try:
            assets_dir = os.path.join("data", "files", "extracted_assets")
            os.makedirs(assets_dir, exist_ok=True)
            img_name = f"{doc_id}_{picture_item.self_ref.replace('/', '_')}.png" if hasattr(picture_item, "self_ref") else f"{doc_id}_img.png"
            full_path = os.path.join(assets_dir, img_name)
            
            if hasattr(picture_item, "get_image"):
                pil_img = picture_item.get_image(doc)
                if pil_img:
                    pil_img.save(full_path, "PNG")
                    return f"extracted_assets/{img_name}"
        except Exception:
            pass
        return None

    @classmethod
    def _convert_fallback(
        cls,
        content: bytes | str,
        filename: str,
        assembler: DynamicDoclingAssembler,
    ) -> DynamicDoclingAssembler:
        """Fallback structural layout parser when Docling weights are unavailable."""
        text = _readable_text(content, filename)
        lines = text.splitlines()
        page_no = 1
        line_idx = 0

        in_table = False
        table_lines: list[str] = []

        for line in lines:
            line_idx += 1
            trimmed = line.strip()

            if not trimmed:
                if in_table:
                    # Flush table
                    cls._flush_table(table_lines, page_no, assembler)
                    table_lines = []
                    in_table = False
                continue

            # Markdown or text Table detection
            if "|" in trimmed and ("---" in trimmed or trimmed.startswith("|") or trimmed.endswith("|")):
                in_table = True
                table_lines.append(trimmed)
                continue
            elif in_table:
                cls._flush_table(table_lines, page_no, assembler)
                table_lines = []
                in_table = False

            # Heading detection
            if trimmed.startswith("#"):
                m = len(trimmed) - len(trimmed.lstrip("#"))
                header_text = trimmed.lstrip("#").strip()
                # Docling's own Markdown reading: `#` is the title, `##` a level-1 section header,
                # `###` level 2. Mirrored so both engines give one document the same tree.
                assembler.add_element(
                    raw_label="title" if m == 1 else "section_header",
                    text=header_text,
                    page_no=page_no,
                    level=0 if m == 1 else m - 1,
                    bbox={"x_min": 50.0, "y_min": float(line_idx * 15), "x_max": 500.0, "y_max": float(line_idx * 15 + 20)},
                )
            elif trimmed.startswith("![") and "](" in trimmed:
                # Image detection
                alt_text = trimmed[2:trimmed.find("]")]
                img_path = trimmed[trimmed.find("](") + 2:trimmed.rfind(")")]
                assembler.add_element(
                    raw_label="picture",
                    text=alt_text,
                    page_no=page_no,
                    bbox={"x_min": 50.0, "y_min": float(line_idx * 15), "x_max": 500.0, "y_max": float(line_idx * 15 + 100)},
                    asset_reference=img_path,
                )
            elif trimmed.startswith(("- ", "* ", "+ ")) or (len(trimmed) > 2 and trimmed[0].isdigit() and trimmed[1] in (".", ")")):
                # List item detection
                assembler.add_element(
                    raw_label="list_item",
                    text=trimmed,
                    page_no=page_no,
                    bbox={"x_min": 60.0, "y_min": float(line_idx * 15), "x_max": 500.0, "y_max": float(line_idx * 15 + 15)},
                )
            else:
                # Prose, labelled "text" — Docling's own label — so a document read by the
                # fallback yields the same node types as one read by Docling.
                assembler.add_element(
                    raw_label="text",
                    text=trimmed,
                    page_no=page_no,
                    bbox={"x_min": 50.0, "y_min": float(line_idx * 15), "x_max": 500.0, "y_max": float(line_idx * 15 + 30)},
                )

        if in_table and table_lines:
            cls._flush_table(table_lines, page_no, assembler)

        return assembler

    @classmethod
    def _flush_table(cls, lines: list[str], page_no: int, assembler: DynamicDoclingAssembler):
        """Parse markdown table rows into a 2D grid matrix."""
        grid: list[list[str]] = []
        for l in lines:
            if re.match(r"^\|?\s*[-:]+[-| :]*\|?$", l.strip()):
                continue  # Skip separator line
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if cells:
                grid.append(cells)
        if grid:
            assembler.add_element(
                raw_label="table",
                text="\n".join(lines),
                page_no=page_no,
                grid=grid,
                bbox={"x_min": 50.0, "y_min": 100.0, "x_max": 500.0, "y_max": 300.0},
            )


#: Containers, not text. Decoding their bytes as UTF-8 produced one node per line of PDF syntax —
#: 318 `Paragraph` nodes reading "xref", "endobj", "%%EOF" for a one-page contract.
_BINARY_EXTS = (".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".odt", ".rtf", ".epub")


def _looks_binary(content: bytes) -> bool:
    return content.startswith(b"%PDF") or content.startswith(b"PK\x03\x04") or b"\x00" in content[:4096]


def _effective_filename(content: bytes | str, filename: str) -> str:
    """A name whose extension tells Docling what it holds.

    With no extension (a sample string from the Mapper's live preview) the old default was `.pdf`,
    so Docling was handed markdown that claimed to be a PDF and always failed.
    """
    if os.path.splitext(filename or "")[1]:
        return filename
    base = filename or "document"
    if isinstance(content, str) or not _looks_binary(content):
        return base + ".md"
    return base + ".pdf"


def _readable_text(content: bytes | str, filename: str) -> str:
    """Text for the structural fallback — through the ingest parser for binary formats."""
    if isinstance(content, str):
        return content
    if os.path.splitext(filename or "")[1].lower() in _BINARY_EXTS or _looks_binary(content):
        from opencortyx.ingest.formats.unstructured.documents import parse_content
        return str(parse_content(content, filename=filename).get("raw_text") or "")
    return content.decode("utf-8", errors="ignore")


def _grid_text(grid: list[list[str]]) -> str:
    return "\n".join(" | ".join(str(c) for c in row) for row in grid if row)
