"""Table formats × data variants through the DuckDB reader (FMTT)."""
from __future__ import annotations

import math

import pytest

from tests.regression.framework import data as D
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression


# ── FMTT: tables ─────────────────────────────────────────────────────────────────────────────────
def _same(a, b) -> bool:
    if a in (None, "") and b in (None, ""):
        return True
    if isinstance(a, bool) or isinstance(b, bool):
        return str(a).lower() == str(b).lower()
    try:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return str(a)[:10] == str(b)[:10] if "-" in str(b) else str(a) == str(b)


@pytest.mark.parametrize("scenario", scenarios("FMTT"), ids=str)
def test_table_format(scenario, rec, tmp_path):
    from opencortyx.ingest.formats.structured import tabular
    fmt, variant = scenario.params["fmt"], scenario.params["variant"]
    cols, rows, expect = D.TABLE_VARIANTS[variant]
    content = D.table_bytes(fmt, cols, rows)
    if content is None:
        rec.block(f"no .{fmt} writer available in the test image")
    path = tmp_path / f"regression.{fmt}"
    path.write_bytes(content)
    rec.generated(format=fmt, rows=len(rows), columns=len(cols), size_bytes=len(content))
    rec.step(f"tabular.preview_file({path.name}) then iter_row_batches")
    try:
        pv = tabular.preview_file(str(path), path.name)
        streamed = [r for batch in tabular.iter_row_batches(str(path), path.name) for r in batch]
    except Exception as e:                                    # noqa: BLE001 — the reader's refusal
        rec.result({"outcome": "rejected", "error": f"{type(e).__name__}: {e}"[:300]})
        rec.verify("outcome matches expectation", expect == "reject", f"{type(e).__name__}: {e}")
        return
    names = [c["name"] for c in pv["columns"]]
    rec.result({"outcome": "read", "row_count": pv["row_count"], "streamed": len(streamed), "columns": names[:12]})
    rec.verify("outcome matches expectation", expect != "reject", "file was read")
    rec.verify("row count", pv["row_count"] == len(rows), f"{pv['row_count']} vs {len(rows)}")
    rec.verify("streamed rows", len(streamed) == len(rows), f"{len(streamed)} vs {len(rows)}")
    rec.verify("column names", names == list(cols), f"{names[:6]} vs {list(cols)[:6]}")
    for i, want in enumerate(rows[:3]):
        got = streamed[i] if i < len(streamed) else {}
        bad = [c for c in cols if not _same(got.get(c), want.get(c))]
        rec.verify(f"row {i} values", not bad, {c: (got.get(c), want.get(c)) for c in bad[:4]})
