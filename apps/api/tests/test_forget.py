"""Forgetting reaches every copy: the data map, tombstones, and backups that must not bring it back.

`test_every_table_is_covered_by_forgetting_or_says_why_not` is the guard: a table added to the
platform without a deletion step - or a reason it needs none - fails it.
"""
import asyncio
import importlib
import pkgutil
import uuid
from types import SimpleNamespace

import pytest

from opencortyx.knowledge import forget

WS = uuid.uuid4()

#: Tables forgetting deletes or redacts rows of, by the step that does it.
COVERED = {
    "data_asset": "graph (asset purge)", "evidence": "evidence", "source_key": "graph (asset purge)",
    "ingest_run": "runs", "ingestion": "runs (manifest entries)", "resolution_key": "resolution",
    "resolution_decision": "resolution", "resolution_label": "resolution", "llm_cache": "cache",
    "chat_message": "chats", "audit_log": "audit", "graph_asset": "indexes", "graph_asset_tag": "indexes",
    "graph_cluster": "indexes", "graph_cluster_member": "indexes", "tombstone": "tombstone",
    "deletion_job": "the job itself (keeps hashes and ids only)",
}
#: Tables that hold no content about a record or a subject.
EXEMPT = {
    "api_key": "hashed keys and roles", "app_user": "accounts (deleting a user is its own action)",
    "chat_thread": "redacted by the chats step when its title names the subject",
    "collection": "names of folders", "connector": "connection settings", "cost_budget": "numbers",
    "extraction_template": "extractor definitions", "graph_cluster_link": "cluster ids and counts",
    "graph_cluster_run": "counts", "graph_root": "tree roots by type", "graph_tag_request": "tag requests",
    "graph_tree_run": "counts", "llm_usage": "token counts, no text", "model": "model settings",
    "pipeline": "pipeline definitions", "policy": "access rules", "runtime_setting": "settings",
    "schema_mapping": "mapping definitions", "type_registry": "the ontology", "workspace": "name and mode",
}


def test_every_table_is_covered_by_forgetting_or_says_why_not():
    import opencortyx.domain as dom
    from opencortyx.domain.base import V2Base
    for m in pkgutil.iter_modules(dom.__path__):
        importlib.import_module("opencortyx.domain." + m.name)
    tables = {t.name for t in V2Base.metadata.sorted_tables}
    missing = tables - set(COVERED) - set(EXEMPT)
    assert not missing, f"no deletion step and no reason for: {sorted(missing)}"


def test_every_store_has_a_step():
    assert set(forget.STORES) >= {"graph", "vectors", "data", "evidence", "masters", "resolution", "cache",
                                  "uploads", "chats", "audit", "runs", "indexes", "tombstone"}


def test_tombstone_keys_are_normalised_and_one_way():
    assert forget.key_hash("John  SMITH ") == forget.key_hash("john smith")
    assert forget.key_hash("john smith") != forget.key_hash("jon smith")
    assert "john" not in forget.key_hash("john smith")


@pytest.fixture
def tombs(monkeypatch):
    """The tombstone table, faked at the database read - below any caching, so a cache would show."""
    held = _Table()

    async def fake(ws, kind=None, hashes=None):
        held.reads.append(kind)
        return {(k, h) for k, h in held if (kind is None or k == kind) and (hashes is None or h in hashes)}
    from opencortyx.core.dataplane import tombstones
    monkeypatch.setattr(tombstones, "_rows", fake)
    return held


class _Table(set):
    """(kind, hash) rows, and the kind each read asked for."""
    def __init__(self):
        super().__init__()
        self.reads: list = []


def test_a_forgotten_asset_is_refused_when_written_again(tombs):
    aid = uuid.uuid4()
    asyncio.run(forget.guard_assets(WS, [aid]))                    # nothing forgotten yet
    tombs.add(("asset", forget.key_hash(aid)))
    with pytest.raises(forget.Forgotten):
        asyncio.run(forget.guard_assets(WS, [uuid.uuid4(), aid]))


def test_a_forget_in_one_process_is_seen_by_every_writer_at_once(tombs):
    """Forget runs in the API, ingestion in the workers. A per-process cache let a worker take a
    forgotten file back 0.3 s after it was forgotten: every guard reads the table itself."""
    aid = uuid.uuid4()
    for _ in range(3):
        asyncio.run(forget.guard_assets(WS, [aid]))
    tombs.add(("asset", forget.key_hash(aid)))                     # written by another process
    with pytest.raises(forget.Forgotten):
        asyncio.run(forget.guard_assets(WS, [aid]))
    assert tombs.reads == ["asset"] * 4


def _node(label, *, props=None, source_ref=None, id_=None, type_="Person"):
    return SimpleNamespace(id=id_ or uuid.uuid4(), label=label, type=type_, properties=dict(props or {}),
                           source_ref=source_ref)


def test_a_backup_restores_without_what_was_forgotten_since(tombs):
    doc = _node("contract.pdf", type_="Source")
    chunk = _node("page 1", source_ref=doc.id, type_="Chunk")
    para = _node("para", source_ref=chunk.id, type_="Chunk")
    person = _node("Jane Roe", props={"__identity": "entity"})
    other = _node("Acme", props={"__identity": "entity"}, type_="Organization")
    tombs |= {("asset", forget.key_hash(doc.id)), ("subject", forget.key_hash("jane roe"))}
    keep, dropped = asyncio.run(forget.restorable(WS, [doc, chunk, para, person, other]))
    assert [n.label for n in keep] == ["Acme"] and dropped == 4


def test_a_restore_with_nothing_forgotten_keeps_everything(tombs):
    nodes = [_node("a"), _node("b")]
    assert asyncio.run(forget.restorable(WS, nodes)) == (nodes, 0)


def test_a_new_file_naming_a_forgotten_subject_does_not_bring_it_back(tombs, monkeypatch):
    deleted = []

    class _Repo:
        async def delete_nodes(self, ws, ids):
            deleted.extend(ids)
            return len(ids)
    monkeypatch.setattr("opencortyx.core.substrate.NodeRepo", _Repo)
    tombs.add(("subject", forget.key_hash("Jane Roe")))
    jane = _node("JANE ROE", props={"__identity": "entity"})
    record = _node("Jane Roe", props={})                   # a record, not a name: kept
    acme = _node("Acme", props={"__identity": "entity"})
    gone = asyncio.run(forget.drop_tombstoned_names(WS, [jane, record, acme]))
    assert gone == [jane.id] and deleted == [jane.id]


def test_names_of_a_subject_come_from_its_records_not_from_a_merged_copy():
    a = _node("Jane Roe", props={"email": "jane@x.io", "name": "Jane Roe"})
    b = _node("J. Roe", props={"username": "jroe"})
    assert forget._names([a, b]) == ["Jane Roe", "jane@x.io", "J. Roe", "jroe"]
    assert forget._names([_node("JR")]) == [], "too short to search for safely"


def test_redaction_replaces_every_spelling_case():
    assert forget._replace_ci("Jane Roe met JANE ROE.", "jane roe") == "[removed] met [removed]."


def test_a_job_view_never_shows_the_names_it_keeps_for_a_retry():
    row = SimpleNamespace(id=uuid.uuid4(), kind="subject", target={"node_id": "x", "_names": ["Jane Roe"]},
                          status="incomplete", steps={}, proof={}, attempts=1, error=None, created_by=None,
                          created_at=None, finished_at=None)
    assert "_names" not in forget.job_view(row)["target"]
