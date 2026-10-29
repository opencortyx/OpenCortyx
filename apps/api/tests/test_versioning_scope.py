"""A new version is the same file uploaded again: version detection compares a document only with
documents of the same name. Compared with every document, chat sessions sharing their headers were
proposed as versions of each other by the hundred (and every upload read the whole workspace).

No services: a fake repository stands in for the graph.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from opencortyx.capabilities.modules import versioning as v
from opencortyx.ingest.unstructured.simhash import simhash64, to_str

WS = uuid.uuid4()
TEXT = "Quarterly review. Alice Smith joined Acme Corporation in Paris in 2021. Bob Lee met Globex."


def _doc(label, text, minutes):
    return SimpleNamespace(id=uuid.uuid4(), type="Source", label=label,
                           properties={"__simhash": to_str(simhash64(text))},
                           created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))


class _Repo:
    def __init__(self, docs):
        self.docs, self.asked, self.patched = docs, [], []

    async def get(self, workspace_id, nid):
        return next((d for d in self.docs if d.id == nid), None)

    async def find(self, workspace_id, exact_types=None, label=None, **kw):
        self.asked.append(label)
        return [d for d in self.docs if d.type in exact_types and (label is None or d.label == label)]

    async def patch_properties(self, workspace_id, ids, values, **kw):
        self.patched.append((ids, values))


@pytest.mark.anyio
async def test_only_a_document_of_the_same_name_can_be_its_earlier_version(monkeypatch):
    old = _doc("report.md", TEXT, 60)
    lookalike = _doc("session-17.md", TEXT, 30)          # same words, another file: not a version
    new = _doc("report.md", TEXT + " Carol Diaz signed.", 0)
    repo = _Repo([old, lookalike, new])
    monkeypatch.setattr(v, "_repo", repo)
    monkeypatch.setattr("opencortyx.core.config.runtime.simhash_version_review", lambda: True)
    res = await v.detect_version(WS, new.id, max_distance=64)   # the scope, not the distance, is tested
    assert repo.asked == ["report.md"], "only the same name is read"
    assert res["proposed_supersede"] == str(old.id)

    alone = _doc("notes.md", TEXT, 0)
    repo = _Repo([lookalike, alone])
    monkeypatch.setattr(v, "_repo", repo)
    assert (await v.detect_version(WS, alone.id, max_distance=64))["superseded_source"] is None
