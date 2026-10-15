"""Auto-detect sources: each file is routed by its format to the pipeline chosen for that kind."""
from types import SimpleNamespace

from opencortyx.api.services.ingestion.ingestions import clean_mappings
from opencortyx.ingest.sources import sync


def test_file_kind_follows_the_format():
    assert sync.file_kind("report.pdf") == sync.KIND_UNSTRUCTURED
    assert sync.file_kind("orders.json") == sync.KIND_SEMISTRUCTURED
    assert sync.file_kind("customers.csv") == sync.KIND_STRUCTURED


def test_auto_detect_folders_scan_every_supported_format():
    assert {"pdf", "json", "csv"} <= set(sync.auto_exts())


def test_mapping_kind_keeps_auto():
    assert sync.mapping_kind("auto") == sync.KIND_AUTO
    assert sync.mapping_kind("document") == sync.KIND_UNSTRUCTURED


def test_per_kind_pipelines_are_saved_and_resolved():
    rows = clean_mappings([{
        "connector_id": "c1", "kind": "auto", "directories": ["/data/inbox"],
        "pipelines": {"structured": "p-table", "semistructured": "", "other": "x"},
    }])
    assert rows == [{"connector_id": "c1", "pipeline_id": None, "directories": ["/data/inbox"],
                     "kind": "auto", "pipelines": {"structured": "p-table"}}]

    ingestion = SimpleNamespace(kind="auto", config={"mappings": rows}, connector_id=None, pipeline_id=None)
    (mapping,) = sync.resolve_mappings(ingestion)
    assert mapping["pipelines"] == {"structured": "p-table"}
    assert sync.is_file_sync(ingestion)
