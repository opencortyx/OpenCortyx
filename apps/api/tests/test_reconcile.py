"""Reconcile: one rule per type, one similarity, clusters as a master with members - and the negative
cases (numbers, short words, other types, masters, types without a rule) that must not match."""
import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from opencortyx.resolution import engine, records
from opencortyx.resolution.normalize import similarity
from opencortyx.resolution.spec import ResolutionSpec

WS = uuid.uuid4()
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _n(label, type_="Person", props=None, named=True):
    p = dict(props or {})
    if named:
        p["__identity"] = "entity"
    return SimpleNamespace(id=uuid.uuid4(), type=type_, label=label, properties=p, created_at=T0,
                           confidence=0.9, level=0)


# ── one similarity, word by word ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("a,b", [("kristen hannah", "kristin hannah"), ("recurrent neural network", "recurrent neural networks"),
                                 ("hilton garden inn", "hilton gardens inn")])
def test_a_near_spelling_is_near(a, b):
    assert 0.85 <= similarity(a, b) < 1.0


@pytest.mark.parametrize("a,b", [("25 minutes", "20 minutes"), ("30 60 minutes", "60 minutes"), ("this week", "this weekend"),
                                 ("acl", "eacl"), ("3 day", "3 days"), ("paris", "peru")])
def test_different_numbers_words_and_short_words_are_far(a, b):
    assert similarity(a, b) < 0.85


def test_each_field_of_an_identity_is_compared_on_its_own():
    assert similarity("acme\x1fuk", "acme\x1fus") == 0.0, "a short field differs: different"
    assert similarity("acme\x1fuk", "acme") == 0.0, "a different number of fields: different"


# ── the rule a type follows ──────────────────────────────────────────────────────────────────────

def test_a_named_thing_without_a_rule_of_its_own_is_matched_by_its_name():
    spec = engine.spec_for({}, _n("Kristen Hannah"))
    assert spec.identify_by == ("label",) and spec.scorer == "similar" and spec.auto <= 0.95 and spec.review < spec.auto


def test_a_type_with_identity_fields_follows_them_and_a_type_with_neither_is_never_matched():
    rule = ResolutionSpec(identify_by=("customer_id",))
    assert engine.spec_for({"Customer": rule}, _n("Alice", "Customer", named=False)) is rule
    assert engine.spec_for({}, _n("a log line", "LogLine", named=False)) is None, "each one unique by default"


class _Store:
    """The engine's storage, in memory: an identity index and the plans it was asked to apply."""
    def __init__(self, known=()):
        self.known = {n.id: n for n in known}
        self.index: dict = {}
        self.applied = []

    async def specs(self, ws):
        return {}

    async def lookup(self, ws, t, keys, exclude):                 # the index is per type, as stored
        return {k: v for (tt, k), v in self.index.items() if tt == t and k in keys and v not in exclude}

    async def candidates(self, ws, t, blocks, exclude):
        out = {}
        for n in self.known.values():
            b = records.block_of(n, engine.named_spec())
            if n.type == t and b in blocks and n.id not in exclude:
                out.setdefault(b, []).append(n.id)
        return out

    async def nodes(self, ws, ids):
        return {i: self.known[i] for i in ids if i in self.known}

    async def forget(self, ws, t, ids):
        pass

    async def apply(self, ws, t, plan, known):
        self.applied.append((t, plan))


def _reconcile(new, old=()):
    store = _Store(old)
    for n in old:
        store.index[(n.type, records.key_of(n, engine.named_spec()))] = n.id
    out = asyncio.run(engine.reconcile(WS, new, store=store, specs={}))
    return out, store


def test_the_same_name_from_two_sources_is_one_thing():
    old, new = _n("The Nightingale", "Work_of_art"), _n("the nightingale", "Work_of_art")
    out, store = _reconcile([new], [old])
    (_, plan), = store.applied
    assert plan.same == [(new.id, old.id)] and out["Work_of_art"]["same"] == 1


def test_a_near_name_is_joined_and_a_far_one_left_alone():
    old = _n("Kristin Hannah")
    _, store = _reconcile([_n("Kristen Hannah"), _n("Kristina Hansen")], [old])
    (_, plan), = store.applied
    assert len(plan.same) == 1 and plan.same[0][1] == old.id, "Kristen joins; Kristina Hansen is someone else"


def test_a_near_name_joined_is_not_also_a_disagreement_about_the_name():
    """"hostel" joined "Hostels" because the names are near: the name is not a conflict for Review.
    Another attribute that differs still is."""
    old = _n("Hostels", "Place", {"name": "Hostels", "country": "Japan"})
    new = _n("hostel", "Place", {"name": "hostel", "country": "Peru"})
    _, store = _reconcile([new], [old])
    (_, plan), = store.applied
    flagged = [(d["attribute"], d["needs_review"]) for d in plan.decisions if d["relation"] == records.CONTRADICTS]
    assert plan.same == [(new.id, old.id)] and flagged == [("country", True)]


def test_values_and_other_types_never_join():
    _, store = _reconcile([_n("20 minutes", "Time")], [_n("25 minutes", "Time")])
    assert all(not plan.same for _, plan in store.applied)
    _, store = _reconcile([_n("Paris", "Gpe")], [_n("Paris", "Person")])
    assert all(not plan.same for _, plan in store.applied), "a place and a person called Paris are two things"


def test_masters_and_types_without_a_rule_are_not_reconciled():
    master = _n("Acme", "Org", {"__master": True})
    out, store = _reconcile([master, _n("a log line", "LogLine", named=False)])
    assert out == {} and store.applied == []


# ── clusters: a master with members, never destructive ───────────────────────────────────────────

class _Repo:
    def __init__(self, nodes):
        self.nodes = {n.id: n for n in nodes}
        self.upserts, self.edges = [], []

    async def get(self, ws, i):
        return self.nodes.get(i)

    async def upsert_many(self, ws, specs, date_link=True):
        self.upserts += specs

    async def add_edges(self, *, workspace_id, edges, **kw):
        self.edges += edges


def test_join_makes_one_master_with_every_member_and_leaves_the_members_as_they_were(monkeypatch):
    monkeypatch.setattr("opencortyx.tasks.graph.embed_later", lambda ws, ids: None)
    first, second, third = _n("Acme", "Org", {"country": "UK"}), _n("ACME Inc", "Org"), _n("Acme.", "Org")
    repo = _Repo([first, second, third])
    masters = asyncio.run(engine.join(repo, WS, [(second.id, first.id), (third.id, first.id)]))
    master = masters[first.id]
    assert master == engine.master_id(WS, first.id), "one master per cluster, the same id every time"
    (spec,) = repo.upserts
    assert spec["type"] == "Org" and spec["label"] == "Acme" and spec["properties"]["country"] == "UK"
    assert spec["properties"]["__master"] is True
    assert {(e["source_id"], e["target_id"], e["type"]) for e in repo.edges} == {
        (first.id, master, "MEMBER_OF"), (second.id, master, "MEMBER_OF"), (third.id, master, "MEMBER_OF")}
    assert first.properties == {"country": "UK", "__identity": "entity"}, "a member is never rewritten"


def test_a_person_s_decision_joins_the_same_way(monkeypatch):
    monkeypatch.setattr("opencortyx.tasks.graph.embed_later", lambda ws, ids: None)
    a, b = _n("Kristen Hannah"), _n("Kristin Hannah")
    repo = _Repo([a, b])
    masters = asyncio.run(engine.join(repo, WS, [(a.id, b.id)], by="person:1"))
    assert all(e["properties"]["__by"] == "person:1" for e in repo.edges) and masters[b.id]
