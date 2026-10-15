"""The asset catalogue shows what ingestion recorded about each asset, never placeholder values."""
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from opencortyx.api.services.ingestion.structured import asset_view
from opencortyx.ingest.formats.catalog import FORMATS


def _node(type_, **props):
    return SimpleNamespace(id=uuid.uuid4(), label=props.get("filename") or props.get("table_name"),
                           type=type_, properties=props, sensitivity="normal",
                           updated_at=datetime(2026, 9, 14, tzinfo=timezone.utc))


def test_text_document():
    view = asset_view(_node("Source", filename="notes.txt", mime="text/plain", word_count=226,
                            __simhash="1079539190589164066"))
    assert view["kind"] == "document"
    assert view["format"] == FORMATS["txt"].name
    assert view["words"] == 226
    assert view["source"] is None
    assert view["fingerprint"] == format(1079539190589164066, "016x")
    assert view["updated_at"] == "2026-09-14T00:00:00+00:00"


def test_json_records_are_semistructured():
    view = asset_view(_node("Source", filename="orders.json", __source="json"))
    assert (view["kind"], view["format"]) == ("semistructured", FORMATS["json"].name)
    assert asset_view(_node("Source", __source="json"))["kind"] == "semistructured"


def test_tables_from_a_file_and_from_a_database():
    csv = asset_view(_node("Table", table_name="customers", source_file="customers.csv",
                           source_system="file", row_count=3, column_count=4))
    assert (csv["kind"], csv["format"], csv["source"]) == ("structured", FORMATS["csv"].name, "file")
    assert (csv["rows"], csv["columns"]) == (3, 4)

    table = asset_view(_node("Table", table_name="orders", source_system="postgres"))
    assert (table["kind"], table["format"], table["source"]) == ("structured", "postgres", "postgres")


def test_retyped_asset_keeps_its_kind():
    view = asset_view(_node("Invoice", filename="scan.pdf", __base_type="Source"))
    assert (view["kind"], view["format"]) == ("document", FORMATS["pdf"].name)


def test_unknown_values_are_empty_not_invented():
    view = asset_view(_node("Source", filename="README", __simhash="not-a-number"))
    assert view["kind"] == "document"
    assert view["format"] is None
    assert view["fingerprint"] is None
    assert view["rows"] is None and view["words"] is None
