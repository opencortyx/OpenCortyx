"""A workbook read as a document (spreadsheets normally go to the structured path, which turns rows
into records; this is for one someone chose to read as prose): each sheet a section with its table."""
from __future__ import annotations

import io
from typing import Optional

from ..assembler import LayoutAssembler
from ..engine import grid_text

#: Rows of one sheet read into its table; a sheet larger than this belongs on the structured path.
MAX_ROWS = 2000


class PandasExcelReader:
    name = "excel"
    extensions = ("xlsx", "xlsm", "xls")

    def read(self, content: bytes, filename: str, asm: LayoutAssembler,
             max_pages: Optional[int] = None) -> None:
        import pandas as pd
        asm.metadata.update(_workbook_meta(content, filename))
        sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=str, nrows=MAX_ROWS)
        for page_no, (name, frame) in enumerate(sheets.items(), start=1):
            if max_pages and page_no > max_pages:
                break
            frame = frame.dropna(how="all").dropna(axis=1, how="all").fillna("")
            if frame.empty:
                continue
            asm.add_element(raw_label="section_header", text=str(name), page_no=page_no, level=1)
            grid = [[str(v).strip() for v in row] for row in frame.values.tolist()]
            asm.add_element(raw_label="table", text=grid_text(grid), page_no=page_no, grid=grid)


def _workbook_meta(content: bytes, filename: str) -> dict:
    """An .xlsx workbook's properties (title, creator, dates); nothing for older formats."""
    if not filename.lower().endswith((".xlsx", ".xlsm")):
        return {}
    try:
        import openpyxl
        from .native import put
        props = openpyxl.load_workbook(io.BytesIO(content), read_only=True).properties
        meta: dict = {}
        for attr, name in (("title", "title"), ("creator", "author"), ("subject", "subject"),
                           ("keywords", "keywords"), ("created", "created"), ("modified", "modified")):
            put(meta, name, getattr(props, attr, None))
        return meta
    except Exception:                                   # noqa: BLE001 — properties are optional
        return {}
