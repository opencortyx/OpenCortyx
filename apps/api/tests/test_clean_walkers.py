"""The default walkers: one per kind of source, each reading a file through the engine for that kind."""
import json
import os
import uuid

import pytest

from opencortyx.ingest import runner
from opencortyx.ingest.walkers import (
    SemiStructuredWalker,
    StructuredWalker,
    UnstructuredWalker,
    walker_for,
)

ORG = uuid.uuid4()


def test_walker_for_each_kind():
    assert isinstance(walker_for("structured"), StructuredWalker)
    assert isinstance(walker_for("semistructured"), SemiStructuredWalker)
    assert isinstance(walker_for("unstructured"), UnstructuredWalker)
    with pytest.raises(ValueError):
        walker_for("auto")


async def test_unstructured_walker_uses_the_document_engine(monkeypatch):
    calls = {}

    async def fake_ingest_document(workspace_id, content, filename, mime_type, collection_id, **kwargs):
        calls.update(content=content, filename=filename, mime_type=mime_type, **kwargs)
        return {"status": "ok", "source_id": "doc-1"}

    monkeypatch.setattr("opencortyx.ingest.unstructured.ingest.ingest_document",
                        fake_ingest_document)
    stages = [{"id": "stage_0", "type": "chunking", "config": {}}]
    res = await UnstructuredWalker().ingest_file(ORG, b"%PDF-1.7", "report.pdf", pipeline=stages,
                                                 reporter="reporter")
    assert res["source_id"] == "doc-1"
    assert calls["mime_type"] == "application/pdf"
    assert calls["pipeline"] == stages
    assert calls["reporter"] == "reporter"


async def test_structured_walker_hands_the_engine_a_readable_file(monkeypatch):
    seen = {}

    async def fake_ingest_tabular_file(workspace_id, path, filename, **kwargs):
        with open(path, "rb") as fh:
            seen.update(content=fh.read(), suffix=os.path.splitext(path)[1], **kwargs)
        return {"status": "ok", "source_id": "table-1"}

    monkeypatch.setattr("opencortyx.ingest.structured.ingest.ingest_tabular_file",
                        fake_ingest_tabular_file)
    res = await StructuredWalker().ingest_file(ORG, b"id,name\n1,Ada\n", "people.csv",
                                               step1_cfg={"mode": "row"})
    assert res["source_id"] == "table-1"
    assert seen["content"] == b"id,name\n1,Ada\n"
    assert seen["suffix"] == ".csv"
    assert seen["step1_cfg"] == {"mode": "row"}


async def test_semistructured_walker_walks_records_without_a_pipeline(monkeypatch):
    seen = {}

    async def fake_ingest_json(workspace_id, data, **kwargs):
        seen.update(data=data, **kwargs)
        return {"status": "ok", "source_id": "records-1"}

    monkeypatch.setattr("opencortyx.ingest.semistructured.ingest.ingest_json", fake_ingest_json)
    payload = [{"id": 1, "name": "Ada"}]
    res = await SemiStructuredWalker().ingest_file(ORG, json.dumps(payload).encode(), "people.json",
                                                   options={"max_depth": 3})
    assert res["source_id"] == "records-1"
    # the RECORDS are walked, each under the Source — not the parser's {"records": [...]} envelope,
    # which walked as one record holding every element as a child
    assert seen["data"] == payload
    assert seen["max_depth"] == 3
    assert seen["checksum"]


async def test_runner_detects_the_kind_unless_one_is_given(monkeypatch):
    used = []

    async def fake_ingest_file(self, workspace_id, content, filename, **kwargs):
        used.append((self.kind, filename))
        return {"status": "ok"}

    for cls in (StructuredWalker, SemiStructuredWalker, UnstructuredWalker):
        monkeypatch.setattr(cls, "ingest_file", fake_ingest_file)
    for name in ("orders.csv", "orders.json", "orders.md"):
        await runner.ingest_file(ORG, b"", name)
    await runner.ingest_file(ORG, b"", "notes.txt", kind="structured")
    assert used == [("structured", "orders.csv"), ("semistructured", "orders.json"),
                    ("unstructured", "orders.md"), ("structured", "notes.txt")]


async def test_semistructured_tree_walk_builds_typed_nodes_and_sibling_edges():
    walker = SemiStructuredWalker(workspace_id=uuid.uuid4(), persist=False)
    data = {
        "customer": {
            "id": "CUST-10001",
            "name": "Alice Johnson",
            "phone_numbers": ["+91-9876543210", "+91-9123456780"],
            "profile": {"gender": "female", "preferences": {"theme": "dark"}},
            "addresses": [{"id": "ADDR-001", "city": "Nagpur"}, {"id": "ADDR-002", "city": "Mumbai"}],
        }
    }

    res = await walker.walk(data, source_label="user_dataset.json")

    assert res["status"] == "ok"
    assert res["nodes"] > 5 and res["edges"] > 5
    assert {"Customer", "Addresses", "Profile"} <= set(res["created_by_type"])
    assert any(e["type"] == "NEXT" for e in walker.emitted_edges)
