"""The resolution engine: normalising, per-type settings, record decisions and the asset flow."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from opencortyx.resolution import records
from opencortyx.resolution.engine import resolve_asset
from opencortyx.resolution.normalize import identity_key, norm
from opencortyx.resolution.spec import ResolutionSpec, specs_from_emit

ORG, SRC = uuid.uuid4(), uuid.uuid4()
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


class N:
    """What the engine reads of a graph node."""
    def __init__(self, props, type_="Customer", age=0, id_=None):
        self.id = id_ or uuid.uuid4()
        self.type, self.properties = type_, props
        self.created_at, self.confidence, self.level = T0 + timedelta(seconds=age), 1.0, 0


SPEC = ResolutionSpec(identify_by=("customer_no",), ignore=("notes",))


# ── normalising ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("a,b", [(" ACME  Inc ", "acme inc"), ("9.50", 9.5), ("1,200", 1200), (7, "7.0"),
                                 ("2024-01-15T00:00:00", "2024-01-15"), (["b", "a"], ["a", "b"]),
                                 (True, "true"), ("Ｃａｆé", "café")])
def test_equal_meaning_is_equal(a, b):
    assert norm(a) == norm(b)


@pytest.mark.parametrize("a,b", [("007", "7x"), ("eu-west-1", "ap-south-1"), ("2024-01-15", "2024-01-16"),
                                 (1, 2), ("1.5", "15")])
def test_different_values_stay_different(a, b):
    assert norm(a) != norm(b)


def test_leading_zeros_are_kept_as_text():
    assert norm("007") == "7" and norm("007a") == "007a"      # a pure number is a number


def test_an_incomplete_identity_matches_nothing():
    assert identity_key(["C-1", None]) is None and identity_key([]) is None
    assert identity_key(["C-1", "x"]) == identity_key(["c-1", " X "])


# ── settings ───────────────────────────────────────────────────────────────────────────────────
def test_specs_come_from_the_extractors_emit():
    emit = {"nodes": [{"type": "Customer", "id": ["customer_no"], "resolve": {"ignore": ["notes"]}},
                      {"type": "Order", "id": "order_no, line"},
                      {"type": "Note"}]}                                  # no identity → not resolved
    specs = specs_from_emit(emit)
    assert set(specs) == {"Customer", "Order"}
    assert specs["Customer"].identify_by == ("customer_no",) and specs["Customer"].ignore == ("notes",)
    assert specs["Order"].identify_by == ("order_no", "line")


def test_compared_attributes_skip_identity_ignored_and_internal():
    assert SPEC.compared(["customer_no", "region", "notes", "__source"]) == ["region"]
    assert ResolutionSpec(identify_by=("k",), compare=("a",)).compared(["a", "b"]) == ["a"]


def test_a_spec_without_identity_is_inactive():
    assert not ResolutionSpec().active and not ResolutionSpec(identify_by=("k",), enabled=False).active


def test_spec_round_trips():
    s = ResolutionSpec(identify_by=("a", "b"), compare=("c",), ignore=("d",), enabled=False)
    assert ResolutionSpec.parse(s.to_dict()) == s


# ── record decisions ──────────────────────────────────────────────────────────────────────────
def _decide(nodes, existing=None, known=None):
    return records.decide(ORG, SRC, "Customer", SPEC, nodes, existing or {}, known or {})


def test_a_new_identity_becomes_canonical():
    plan = _decide([N({"customer_no": "C-1"})])
    assert plan.same == [] and plan.decisions == [] and len(plan.keys) == 1


def test_same_identity_in_one_asset_links_to_the_first():
    a, b = N({"customer_no": "C-1"}, age=0), N({"customer_no": "c-1 "}, age=1)
    plan = _decide([b, a])
    assert plan.same == [(b.id, a.id)]


def test_existing_node_is_canonical_and_attributes_are_classified():
    old = N({"customer_no": "C-1", "region": "eu-west-1", "tier": "gold"}, age=-100)
    new = N({"customer_no": "C-1", "region": "ap-south-1", "tier": "GOLD", "owner": "Ana", "notes": "x"})
    plan = _decide([new], existing={records.key_of(new, SPEC): old.id}, known={old.id: old})
    assert plan.same == [(new.id, old.id)]
    rel = {d["attribute"]: d["relation"] for d in plan.decisions}
    assert rel == {None: "SAME", "region": "CONTRADICTS", "tier": "SUPPORTS", "owner": "EXTENDS"}
    assert plan.extend == {old.id: {"owner": "Ana"}}                     # added, never overwritten
    conflict = next(d for d in plan.decisions if d["relation"] == "CONTRADICTS")
    assert conflict["needs_review"] and conflict["evidence"] == {"value": "ap-south-1", "other": "eu-west-1"}


def test_an_extension_is_added_once_then_supported():
    old = N({"customer_no": "C-1"}, age=-100)
    x, y = N({"customer_no": "C-1", "owner": "Ana"}, age=1), N({"customer_no": "C-1", "owner": "ana"}, age=2)
    plan = _decide([x, y], existing={records.key_of(x, SPEC): old.id}, known={old.id: old})
    assert [d["relation"] for d in plan.decisions if d["attribute"] == "owner"] == ["EXTENDS", "SUPPORTS"]


def test_nodes_without_identity_are_left_alone():
    plan = _decide([N({"name": "no key"}), N({"customer_no": ""})])
    assert plan.unidentified == 2 and plan.keys == [] and plan.decisions == []


def test_decisions_do_not_depend_on_input_order_and_are_keyed_by_content():
    a, b, c = (N({"customer_no": "C-9", "v": i}, age=i) for i in range(3))
    first, second = _decide([a, b, c]), _decide([c, a, b])
    assert first.same == second.same
    assert [d["id"] for d in first.decisions] == [d["id"] for d in second.decisions]


# ── the asset flow, against a fake store ──────────────────────────────────────────────────────
class FakeStore:
    def __init__(self, specs, nodes, index=None, known=None):
        self._specs, self._nodes, self.index, self._known = specs, nodes, index or {}, known or {}
        self.pool = {}
        self.lookups, self.applied, self.statements_for, self.forgotten = [], [], [], []

    async def specs(self, org):
        return self._specs

    async def asset_nodes(self, org, src):
        return self._nodes

    async def lookup(self, org, node_type, keys, exclude):
        self.lookups.append((node_type, set(keys)))
        return {k: v for k, v in self.index.items() if k in keys and v not in exclude}

    async def nodes(self, org, ids):
        return {i: self._known[i] for i in ids if i in self._known}

    async def forget(self, org, node_type, ids):
        self.forgotten.append((node_type, set(ids)))

    async def candidates(self, org, node_type, blocks, exclude):
        return {b: [i for i in ids if i not in exclude] for b, ids in self.pool.items() if b in blocks}

    async def apply(self, org, node_type, plan, known):
        self.applied.append((node_type, plan))

    async def statements(self, org, src):
        self.statements_for.append(src)
        return {"checked": 0, "applied": 0}

    async def mark_resolved(self, org, src, summary):
        self.resolved = (src, summary)


def test_resolve_asset_is_one_lookup_and_one_write_per_type():
    old = N({"customer_no": "C-1", "region": "eu"}, age=-100)
    nodes = [N({"customer_no": "C-1", "region": "ap"}), N({"customer_no": "C-2"}),
             N({"sku": "S-1"}, type_="Product"), N({"text": "chunk"}, type_="Chunk")]
    store = FakeStore({"Customer": SPEC, "Product": ResolutionSpec(identify_by=("sku",))}, nodes,
                      index={records.key_of(nodes[0], SPEC): old.id}, known={old.id: old})
    out = asyncio.run(resolve_asset(ORG, SRC, store=store))
    assert sorted(t for t, _ in store.lookups) == ["Customer", "Product"]
    assert len(store.applied) == 2
    assert out["types"]["Customer"]["same"] == 1 and out["types"]["Customer"]["contradicts"] == 1
    assert out["types"]["Product"] == {"same": 0, "supports": 0, "extends": 0, "contradicts": 0, "review": 0,
                                       "unidentified": 0}
    assert store.statements_for == [SRC]                               # statements always run
    assert store.resolved == (SRC, out["types"])                       # the asset says it was resolved


def test_without_settings_only_statements_run():
    store = FakeStore({}, [N({"customer_no": "C-1"})])
    out = asyncio.run(resolve_asset(ORG, SRC, store=store))
    assert store.lookups == [] and store.applied == [] and out["types"] == {}


# ── scorer, gate and relations ─────────────────────────────────────────────────────────────────
SIMILAR = ResolutionSpec(identify_by=("company",), scorer="similar", auto=0.95, review=0.80)


def _similar(nodes, existing_nodes, spec=SIMILAR):
    pool = {}
    for n in existing_nodes:
        pool.setdefault(records.block_of(n, spec), []).append(n.id)
    return records.decide(ORG, SRC, "Customer", spec, nodes, {}, {n.id: n for n in existing_nodes}, pool)


def test_similar_scorer_links_close_spellings_above_the_gate():
    old, new = N({"company": "Acme Corporation"}, age=-10), N({"company": "Acme Corporation."})
    plan = _similar([new], [old])
    same = plan.decisions[0]
    assert plan.same == [(new.id, old.id)] and same["method"] == "similar" and same["confidence"] >= 0.95


def test_a_possible_match_between_the_thresholds_waits_for_a_person():
    old, new = N({"company": "Acme Corporation"}, age=-10), N({"company": "Acme Corporatn"})
    plan = _similar([new], [old])
    same = plan.decisions[0]
    assert plan.same == [] and same["relation"] == "SAME" and same["needs_review"]
    assert 0.80 <= same["confidence"] < 0.95 and len(plan.decisions) == 1   # no attributes until decided


def test_below_the_review_threshold_it_is_not_a_match():
    old, new = N({"company": "Acme Corporation"}, age=-10), N({"company": "Acmeworks Foundry Ltd"})
    plan = _similar([new], [old])
    assert plan.same == [] and plan.decisions == []


def test_similar_only_compares_within_a_block():
    old, new = N({"company": "Zenith Acme"}, age=-10), N({"company": "Acme Zenith"})
    assert records.block_of(old, SIMILAR) != records.block_of(new, SIMILAR)
    assert _similar([new], [old]).decisions == []


def test_relations_limit_what_is_applied():
    spec = ResolutionSpec(identify_by=("customer_no",), relations=("SAME", "SUPPORTS"))
    old = N({"customer_no": "C-1", "tier": "gold", "region": "eu"}, age=-100)
    new = N({"customer_no": "C-1", "tier": "gold", "region": "ap", "owner": "Ana"})
    plan = records.decide(ORG, SRC, "Customer", spec, [new], {records.key_of(new, spec): old.id}, {old.id: old})
    assert sorted(d["relation"] for d in plan.decisions) == ["SAME", "SUPPORTS"] and plan.extend == {}


def test_without_same_nothing_is_linked():
    spec = ResolutionSpec(identify_by=("customer_no",), relations=("SUPPORTS",))
    old, new = N({"customer_no": "C-1"}, age=-100), N({"customer_no": "C-1"})
    plan = records.decide(ORG, SRC, "Customer", spec, [new], {records.key_of(new, spec): old.id}, {old.id: old})
    assert plan.same == [] and plan.decisions == []


def test_a_spec_round_trips_with_its_gate():
    s = ResolutionSpec(identify_by=("a",), relations=("SAME",), scorer="similar", auto=0.9, review=0.7)
    assert ResolutionSpec.parse(s.to_dict()) == s
    assert ResolutionSpec.parse({"identify_by": ["a"], "auto": 0.6, "review": 0.9}).review == 0.6   # review ≤ auto
    assert ResolutionSpec.parse({"identify_by": ["a"], "scorer": "bogus"}).scorer == "exact"


def test_resolve_asset_uses_the_block_pool_for_similar():
    old, new = N({"company": "Acme Corporation"}, age=-10), N({"company": "Acme Corporation."})
    store = FakeStore({"Customer": SIMILAR}, [new], known={old.id: old})
    store.pool = {records.block_of(old, SIMILAR): [old.id]}
    out = asyncio.run(resolve_asset(ORG, SRC, store=store))
    assert out["types"]["Customer"]["same"] == 1


# ── wiring ────────────────────────────────────────────────────────────────────────────────────
def test_resolution_has_its_own_queue():
    from opencortyx.core.worker.celery_app import celery_app
    assert celery_app.conf.task_routes["opencortyx.resolve_asset"] == {"queue": "resolution"}


def test_contradiction_has_no_ingest_hook_of_its_own():
    from opencortyx.capabilities.registry import registry
    assert "contradiction" not in [c.name for c in registry.post_ingest_hooks()]
    assert "versioning" in [c.name for c in registry.post_ingest_hooks()]


def test_fields_a_writer_generated_are_never_compared():
    spec = ResolutionSpec(identify_by=("customer_no",))
    old = N({"customer_no": "C-1", "tier": "gold", "text": "C-1 tier=gold", "__generated": ["text"]}, age=-100)
    new = N({"customer_no": "C-1", "tier": "platinum", "text": "C-1 tier=platinum", "__generated": ["text"]})
    plan = records.decide(ORG, SRC, "Customer", spec, [new], {records.key_of(new, spec): old.id}, {old.id: old})
    assert [(d["relation"], d["attribute"]) for d in plan.decisions] == [("SAME", None), ("CONTRADICTS", "tier")]


def test_a_key_whose_node_is_gone_is_forgotten_not_matched():
    gone = uuid.uuid4()                          # indexed, then its asset was deleted
    node = N({"customer_no": "C-1"})
    store = FakeStore({"Customer": SPEC}, [node], index={records.key_of(node, SPEC): gone})
    asyncio.run(resolve_asset(ORG, SRC, store=store))
    (_, plan), = store.applied
    assert plan.same == [] and plan.keys == [(records.key_of(node, SPEC), node.id)]
    assert store.forgotten == [("Customer", {gone})]


def test_the_dates_every_write_stamps_are_never_compared():
    from opencortyx.core.substrate.repo import AUDIT_DATE_KEYS
    from opencortyx.resolution.spec import STAMPED
    assert set(STAMPED) == set(AUDIT_DATE_KEYS)
    assert SPEC.compared(["region", "created_date", "modified_date"]) == ["region"]
