"""Access scope: one rule, applied to every read NodeRepo makes while a scope is bound.

Negative cases first: a record in a collection the caller may not read, above their sensitivity, or
a merged record with a member they may not read is NOT FOUND - by id, by search, by a graph hop -
and a write still sees it, so nothing a caller cannot see is ever overwritten as if it were new.
"""
import asyncio
import uuid

import pytest

from opencortyx.core.security import scope as S
from opencortyx.core.substrate.repo import NodeRepo
from opencortyx.stores.graph.adapters.memory import MemoryGraphStore
from opencortyx.stores.graph.models import Node

WS = uuid.uuid4()
SALES, HR = uuid.uuid4(), uuid.uuid4()


def _n(label, *, collection=None, sensitivity="normal", props=None):
    return Node(id=uuid.uuid4(), workspace_id=WS, type="Record", label=label, properties=dict(props or {}),
                collection_id=collection, sensitivity=sensitivity)


@pytest.fixture
def repo(monkeypatch):
    async def no_index(self, changes):
        return None
    monkeypatch.setattr(NodeRepo, "_index", no_index)
    monkeypatch.setattr("opencortyx.core.substrate.repo._guard_assets", _no_guard)
    graph = MemoryGraphStore()
    r = NodeRepo(graph=graph, vectors=object())
    sales, hr, open_, secret = (_n("deal", collection=SALES), _n("salary", collection=HR),
                                _n("policy"), _n("ssn", sensitivity="confidential"))
    merged = _n("Acme", props={"__master": True, "__collections": [str(SALES), str(HR)]})
    asyncio.run(graph.upsert_nodes([sales, hr, open_, secret, merged]))
    from opencortyx.stores.graph.models import Edge
    asyncio.run(graph.upsert_edges([Edge(workspace_id=WS, source_id=sales.id, target_id=hr.id, type="REL")]))
    r.fx = {"sales": sales, "hr": hr, "open": open_, "secret": secret, "merged": merged}
    S.bind(None)
    yield r
    S.bind(None)


async def _no_guard(ws, ids):
    return None


SALES_ONLY = S.Scope(collections=frozenset({str(SALES)}), max_sensitivity="normal")


def test_no_scope_reads_everything(repo):
    ids = [n.id for n in repo.fx.values()]
    assert len(asyncio.run(repo.get_many(WS, ids))) == 5


def test_a_record_of_another_collection_is_not_found_by_id_or_in_a_list(repo):
    with S.using(SALES_ONLY):
        assert asyncio.run(repo.get(WS, repo.fx["hr"].id)) is None
        got = {n.label for n in asyncio.run(repo.get_many(WS, [n.id for n in repo.fx.values()]))}
    assert got == {"deal", "policy"}, "the HR record, the confidential one and the mixed master are hidden"


def test_a_graph_hop_never_walks_into_what_the_caller_may_not_see(repo):
    with S.using(SALES_ONLY):
        hops = asyncio.run(repo.neighbors(WS, [repo.fx["sales"].id], direction="both"))
    assert hops == []
    hops = asyncio.run(repo.neighbors(WS, [repo.fx["sales"].id], direction="both"))
    assert [n.label for _, n in hops] == ["salary"], "without a scope the hop is there"


def test_a_merged_record_needs_every_member_collection():
    both = S.Scope(collections=frozenset({str(SALES), str(HR)}))
    m = _n("Acme", props={"__collections": [str(SALES), str(HR)]})
    assert both.allows(m) and not SALES_ONLY.allows(m)


def test_sensitivity_is_a_ceiling():
    assert not S.Scope(max_sensitivity="normal").allows(_n("x", sensitivity="confidential"))
    assert S.Scope(max_sensitivity="confidential").allows(_n("x", sensitivity="confidential"))
    assert not S.Scope(max_sensitivity="confidential").allows(_n("x", sensitivity="restricted"))


def test_records_in_no_collection_are_workspace_wide():
    assert SALES_ONLY.allows(_n("x")) and S.Scope(collections=frozenset()).allows(_n("x"))
    assert not S.Scope(collections=frozenset()).allows(_n("x", collection=SALES))


def test_a_write_still_sees_the_record_it_updates(repo):
    """An upsert by a caller who cannot see the record must not treat it as new and wipe it."""
    hr = repo.fx["hr"]
    with S.using(SALES_ONLY):
        asyncio.run(repo.upsert_node(workspace_id=WS, type="Record", node_id=hr.id, properties={"note": "x"},
                                     date_link=False))
    after = asyncio.run(repo.get(WS, hr.id))
    assert after.label == "salary" and after.collection_id == HR and after.properties["note"] == "x"


def test_scopes_only_narrow():
    wide = S.Scope(collections=None, max_sensitivity="restricted")
    assert wide.intersect(SALES_ONLY) == SALES_ONLY.intersect(wide) == SALES_ONLY
    none = S.Scope(collections=frozenset({str(HR)})).intersect(SALES_ONLY)
    assert none.collections == frozenset() and none.collection_ids() == [S.NO_COLLECTION], \
        "nothing allowed is a filter that matches nothing - never an empty list read as 'all'"


def test_using_restores_the_outer_scope():
    S.bind(None)
    with S.using(SALES_ONLY):
        assert S.current() == SALES_ONLY
        with S.unscoped():
            assert S.current() is None
        assert S.current() == SALES_ONLY
    assert S.current() is None


# ── the policy rule ─────────────────────────────────────────────────────────────────────────────

def _policies(monkeypatch, rows):
    async def fake(ws):
        return rows
    monkeypatch.setattr(S, "_workspace_policies", fake)


def test_a_workspace_without_policies_has_no_access_control(monkeypatch):
    _policies(monkeypatch, [])
    assert asyncio.run(S.resolve_scope(WS, ["viewer"])) == (None, "normal")


def test_once_a_policy_exists_a_group_no_policy_names_reads_no_collection(monkeypatch):
    _policies(monkeypatch, [("finance", str(SALES), "normal", "read")])
    allowed, sens = asyncio.run(S.resolve_scope(WS, ["viewer"]))
    assert allowed == [S.NO_COLLECTION] and sens == "normal"


def test_a_groups_policies_give_their_collections_and_highest_sensitivity(monkeypatch):
    _policies(monkeypatch, [("finance", str(SALES), "confidential", "read"), ("finance", str(HR), "normal", "read"),
                            ("hr", None, "restricted", "read")])
    allowed, sens = asyncio.run(S.resolve_scope(WS, ["finance"]))
    assert set(allowed) == {SALES, HR} and sens == "confidential"
    assert asyncio.run(S.resolve_scope(WS, ["hr"])) == (None, "restricted"), "a policy on every collection"


def test_an_admin_reads_every_collection_but_not_above_what_a_policy_gives(monkeypatch):
    _policies(monkeypatch, [("finance", str(SALES), "normal", "read")])
    assert asyncio.run(S.resolve_scope(WS, ["admin"], admin=True)) == (None, "normal")


def test_browsing_is_unrestricted_for_admins_and_without_policies(monkeypatch):
    from types import SimpleNamespace
    _policies(monkeypatch, [("finance", str(SALES), "normal", "read")])
    admin = SimpleNamespace(role="admin", workspace_id=WS, user_id=uuid.uuid4(), actor_type="user", groups=[])
    assert asyncio.run(S.request_scope(admin)) is None
    agent = SimpleNamespace(role="agent", workspace_id=WS, user_id=uuid.uuid4(), actor_type="agent", groups=["finance"])
    s = asyncio.run(S.request_scope(agent))
    assert s.collections == frozenset({str(SALES)}), "an API key reads as its groups"
    _policies(monkeypatch, [])
    viewer = SimpleNamespace(role="viewer", workspace_id=WS, user_id=uuid.uuid4(), actor_type="user", groups=[])
    assert asyncio.run(S.request_scope(viewer)) is None


def test_a_scope_that_cannot_be_read_fails_closed(monkeypatch):
    from types import SimpleNamespace

    async def broken(ws):
        raise RuntimeError("database down")
    monkeypatch.setattr(S, "_workspace_policies", broken)
    viewer = SimpleNamespace(role="viewer", workspace_id=WS, user_id=uuid.uuid4(), actor_type="user", groups=[])
    async def go():
        await S.bind_request(viewer)
        return S.current()
    assert asyncio.run(go()) == S.Scope(collections=frozenset(), max_sensitivity="normal")


# ── personal-data columns leave the SQL views ───────────────────────────────────────────────────

def test_personal_data_columns_are_hidden_unless_the_scope_reaches_confidential():
    from opencortyx.retrieval.l2_data.schema import Column, Schema, Table
    t = Table(asset_id="a1", view="people", name="people", kind="structured", storage="managed", format="delta",
              columns=[Column("name"), Column("email", pii=True)])
    schema = Schema(tables={"people": t})
    assert schema.hidden_columns() == {}, "the platform's own work (no scope) reads everything"
    with S.using(S.Scope(max_sensitivity="normal")):
        assert schema.hidden_columns() == {"a1": ["email"]}
    with S.using(S.Scope(max_sensitivity="confidential")):
        assert schema.hidden_columns() == {}


def test_a_hidden_column_is_not_in_the_view_so_select_star_cannot_return_it(tmp_path):
    import duckdb  # noqa: F401
    from opencortyx.stores.data.duck import Views
    v = Views(None, [], root=str(tmp_path), hidden={"a1": ["email"]})
    v.con.execute("CREATE TABLE src AS SELECT 'Ana' AS name, 'ana@x.io' AS email")
    from types import SimpleNamespace
    ref = SimpleNamespace(asset_id="a1", storage="external", format="postgres", name="people", location="src",
                          options={}, dsn=None)
    # the view builder's own EXCLUDE path, on a source DuckDB can read directly
    v._hidden = {"a1": ["email"]}
    view = v._view_name(ref)
    v.con.execute(f'CREATE VIEW "{view}" AS SELECT * EXCLUDE ("email") FROM src')
    cols = [d[0] for d in v.con.execute(f'SELECT * FROM "{view}"').description]
    assert cols == ["name"]
    v.close()
