"""Resolution quality: the safety gates, clusters computed from their members, and the score against
labelled pairs - with the hard negatives a wrong link comes from.

Nothing here is specific to people or companies: every rule is a setting of a node TYPE, so the same
tests hold for whatever an extractor, a mapping or a reader writes.
"""
import asyncio
import uuid

import pytest

from opencortyx.resolution import engine, master, quality, records
from opencortyx.resolution.spec import ResolutionSpec, effective
from tests.fakes_graph import FakeRepo, node

WS = uuid.uuid4()


@pytest.fixture(autouse=True)
def _no_embedding(monkeypatch):
    monkeypatch.setattr("opencortyx.tasks.graph.embed_later", lambda ws, ids: None)


def _decide(spec, new, old, *, apart=None):
    spec = effective(spec, corroborate=True, conflict_review=True) if spec.corroborate == "default" else spec
    existing, pool = {}, {}
    for n in old:
        k = records.key_of(n, spec)
        if k:
            existing.setdefault(k, n.id)
        for b in records.blocks_of(n, spec):
            pool.setdefault(b, []).append(n.id)
    if spec.scorer == "similar":
        existing = {}
    return records.decide(WS, None, new[0].type, spec, new, existing, {n.id: n for n in old}, pool, apart)


NAME = ResolutionSpec(identify_by=("label",), scorer="similar", auto=0.85, review=0.75)
KEYED = ResolutionSpec(identify_by=("customer_no",))


# ── the gates ───────────────────────────────────────────────────────────────────────────────────

def test_a_must_agree_field_that_differs_keeps_two_records_apart_and_is_recorded():
    spec = ResolutionSpec(identify_by=("label",), distinguish=("dob",))
    a = node("John Smith", props={"dob": "1970-01-01"})
    b = node("John Smith", props={"dob": "1985-06-30"})
    plan = _decide(spec, [b], [a])
    (d,) = plan.decisions
    assert plan.same == [] and d["relation"] == records.DISTINCT and not d["needs_review"]
    assert d["evidence"]["differ"] == [{"attribute": "dob", "value": "1985-06-30", "other": "1970-01-01"}]


def test_a_must_agree_field_only_one_record_has_does_not_keep_them_apart():
    spec = ResolutionSpec(identify_by=("label",), distinguish=("dob",), conflict_review=False)
    a, b = node("John Smith", props={"dob": "1970-01-01"}), node("John Smith")
    assert _decide(spec, [b], [a]).same == [(b.id, a.id)]


def test_corroborate_always_holds_even_an_equal_identity_without_a_second_field():
    spec = ResolutionSpec(identify_by=("label",), corroborate="always")
    a, b = node("John Smith"), node("John Smith")
    plan = _decide(spec, [b], [a])
    assert plan.same == [] and plan.decisions[0]["needs_review"]
    a2, b2 = node("John Smith", props={"email": "js@x.io"}), node("John Smith", props={"email": "JS@x.io "})
    assert _decide(spec, [b2], [a2]).same == [(b2.id, a2.id)], "the email agrees (after normalising)"


def test_a_similar_name_alone_is_held_and_with_an_agreeing_field_is_joined():
    a = node("Catherine Zeta", props={"city": "Swansea"})
    b = node("Katherine Zeta")
    plan = _decide(NAME, [b], [a])
    assert plan.same == [] and plan.decisions[0]["needs_review"], "first letters differ: found by the last word"
    c = node("Katherine Zeta", props={"city": "swansea"})
    assert _decide(NAME, [c], [a]).same == [(c.id, a.id)]


def test_andersen_and_anderson_are_never_joined_automatically_by_default():
    a, b = node("Hans Andersen"), node("Hans Anderson")
    plan = _decide(NAME, [b], [a])
    assert plan.same == [] and plan.decisions and plan.decisions[0]["needs_review"]


def test_a_pair_a_person_kept_apart_is_never_matched_again_and_the_next_candidate_is_tried():
    spec = ResolutionSpec(identify_by=("label",), scorer="similar", auto=0.85, review=0.75, corroborate="never")
    a, c = node("Kristin Hannah"), node("Kristin Hannahs")
    b = node("Kristen Hannah")
    plan = _decide(spec, [b], [a, c], apart={records.pair(b.id, a.id)})
    assert all(t != a.id for _, t in plan.same)
    exact = _decide(KEYED, [node("x", "Customer", {"customer_no": "C-1"}, named=False)], [])
    assert exact.same == []


def test_an_exact_match_a_person_split_is_not_joined_again():
    a = node("A", "Customer", {"customer_no": "C-1"}, named=False)
    b = node("B", "Customer", {"customer_no": "C-1"}, named=False)
    assert _decide(KEYED, [b], [a], apart={records.pair(a.id, b.id)}).same == []


def test_numbers_short_words_and_other_types_still_never_join():
    for x, y in [("Studio 54", "Studio 45"), ("ACL", "EACL"), ("Unit 7", "Unit 17")]:
        assert _decide(NAME, [node(y)], [node(x)]).decisions == [], (x, y)


# ── clusters are computed from their members ───────────────────────────────────────────────────

def test_compose_takes_the_first_members_value_a_persons_choice_and_records_who_states_it():
    a = node("Acme", "Org", {"country": "UK", "city": "London"})
    b = node("ACME", "Org", {"country": "US", "phone": "1"})
    b.created_at = a.created_at.replace(day=2)
    c = master.compose([b, a])
    assert c["label"] == "Acme" and c["properties"] == {"country": "UK", "city": "London", "phone": "1"}
    assert c["prov"]["country"] == [str(a.id)] and c["prov"]["phone"] == [str(b.id)]
    chosen = master.compose([a, b], {"country": b.id})
    assert chosen["properties"]["country"] == "US", "a settled conflict decides"


def test_a_master_is_as_restricted_as_its_most_restricted_member():
    one, two = uuid.uuid4(), uuid.uuid4()
    a = node("Acme", "Org", collection_id=one, sensitivity="normal")
    b = node("Acme", "Org", collection_id=one, sensitivity="confidential")
    c = master.compose([a, b])
    assert c["sensitivity"] == "confidential" and c["collection_id"] == one and c["collections"] == []
    d = node("Acme", "Org", collection_id=two)
    mixed = master.compose([a, d])
    assert mixed["collection_id"] is None and mixed["collections"] == sorted([str(one), str(two)])


def test_splitting_a_member_removes_its_values_from_the_master():
    a = node("Acme", "Org", {"country": "UK"})
    b = node("ACME", "Org", {"phone": "1"})
    c = node("Acme.", "Org", {"vat": "GB1"})
    repo = FakeRepo([a, b, c])
    m = asyncio.run(engine.join(repo, WS, [(b.id, a.id), (c.id, a.id)]))[a.id]
    assert repo.nodes[m].properties["phone"] == "1"
    asyncio.run(repo.delete_edges(WS, source_ids=[b.id], target_ids=[m], types=["MEMBER_OF"]))
    asyncio.run(master.rebuild(repo, WS, [m], winners=_no_winners))
    props = repo.nodes[m].properties
    assert "phone" not in props and props["vat"] == "GB1" and props["__members"] == 2


def test_a_master_left_with_one_member_is_removed():
    a, b = node("Acme", "Org"), node("ACME", "Org")
    repo = FakeRepo([a, b])
    m = asyncio.run(engine.join(repo, WS, [(b.id, a.id)]))[a.id]
    asyncio.run(repo.delete_nodes(WS, [b.id]))
    out = asyncio.run(master.rebuild(repo, WS, [m], winners=_no_winners))
    assert out == {"rebuilt": 0, "removed": 1} and m not in repo.nodes and a.id in repo.nodes


async def _no_winners(ws, members):
    return {}


def test_matching_a_member_joins_its_existing_cluster_not_a_second_one():
    """A matched candidate that is itself a member: the new node joins the cluster it is in, so one
    thing never gets two masters."""
    a, b, c = node("Acme", "Org"), node("ACME", "Org"), node("Acme.", "Org")
    repo = FakeRepo([a, b, c])
    m = asyncio.run(engine.join(repo, WS, [(b.id, a.id)]))[a.id]
    again = asyncio.run(engine.join(repo, WS, [(c.id, b.id)]))
    assert again[b.id] == m
    masters = {e["target_id"] for e in repo.edge_map.values() if e["type"] == "MEMBER_OF"}
    assert masters == {m} and repo.nodes[m].properties["__members"] == 3


def test_a_person_joining_two_clusters_folds_one_into_the_other():
    a, b, c, d = (node(x, "Org") for x in ("Acme", "ACME", "Acme Ltd", "ACME Ltd"))
    repo = FakeRepo([a, b, c, d])
    m1 = asyncio.run(engine.join(repo, WS, [(b.id, a.id)]))[a.id]
    m2 = asyncio.run(engine.join(repo, WS, [(d.id, c.id)]))[c.id]
    asyncio.run(engine.join(repo, WS, [(c.id, a.id)], by="person:1"))
    members = {e["source_id"] for e in repo.edge_map.values() if e["target_id"] == m1}
    assert members == {a.id, b.id, c.id, d.id} and m2 not in repo.nodes


def test_an_automatic_match_across_two_clusters_waits_for_a_person():
    a, b, c, d = (node(x, "Org") for x in ("Acme", "ACME", "Acme Ltd", "ACME Ltd"))
    repo = FakeRepo([a, b, c, d])
    asyncio.run(engine.join(repo, WS, [(b.id, a.id)]))
    asyncio.run(engine.join(repo, WS, [(d.id, c.id)]))
    store = engine.Store.__new__(engine.Store)
    store.repo = repo
    plan = records.Plan(same=[(c.id, a.id)], decisions=[records._decision(WS, None, "Org", records.SAME, c.id, a.id)])
    asyncio.run(store.hold_cluster_conflicts(WS, plan))
    assert plan.same == [] and plan.decisions[0]["needs_review"]
    assert plan.decisions[0]["evidence"]["reasons"] == ["both already belong to different clusters"]


# ── the score: labelled pairs, hard negatives ────────────────────────────────────────────────────

def _snap(label, props=None, asset=None, named=True):
    p = dict(props or {})
    if named:
        p["__identity"] = "entity"
    return {"label": label, "type": "Person", "properties": p, "asset_id": asset}


#: The cases a wrong link comes from, and the ones a link should still be found in. Each is
#: (a, b, same). Assets differ, as they do when two files mention the same name.
HARD = [
    (_snap("John Smith", {"dob": "1970-01-01"}, "f1"), _snap("John Smith", {"dob": "1985-06-30"}, "f2"), False),
    (_snap("Hans Andersen", asset="f1"), _snap("Hans Anderson", asset="f2"), False),
    (_snap("Jon Smith", asset="f1"), _snap("John Smith", asset="f3"), False),
    (_snap("Studio 54", asset="f1"), _snap("Studio 45", asset="f2"), False),
    (_snap("Michael Lee", asset="f1"), _snap("Michelle Lee", asset="f2"), False),
    (_snap("Kristin Hannah", {"genre": "fiction"}, "f1"), _snap("Kristen Hannah", {"genre": "Fiction"}, "f2"), True),
    (_snap("ＡＣＭＥ Ltd", asset="f1"), _snap("acme ltd", asset="f2"), True),
    (_snap("Dr. Jane Doe", asset="f1"), _snap("Jane Doe", asset="f2"), True),          # missed, by design
    (_snap("Müller", asset="f1"), _snap("Mueller", asset="f2"), True),                 # missed, by design
]


def test_the_default_rule_makes_no_false_merge_on_the_hard_cases():
    spec = ResolutionSpec(identify_by=("label",), scorer="similar", auto=0.85, review=0.75,
                          distinguish=("dob",), name_scope="asset")
    out = quality.evaluate(spec, [{"a": a, "b": b, "same": s} for a, b, s in HARD],
                           corroborate=True, conflict_review=True)
    assert out["false_merges"] == 0, out["false_merge_examples"]
    assert out["true_merges"] == 2 and out["precision"] == 1.0
    assert out["missed"] == 2 and out["recall"] == 0.5, "titles and transliterations are missed, not guessed"


def test_turning_the_gates_off_shows_the_false_merges_it_would_make():
    spec = ResolutionSpec(identify_by=("label",), scorer="similar", auto=0.85, review=0.75, name_scope="asset")
    out = quality.evaluate(spec, [{"a": a, "b": b, "same": s} for a, b, s in HARD],
                           corroborate=False, conflict_review=False)
    bad = {(e["a"], e["b"]) for e in out["false_merge_examples"]}
    assert ("John Smith", "John Smith") in bad and ("Hans Andersen", "Hans Anderson") in bad
    assert out["precision"] < 1.0


def test_names_merged_at_write_are_counted_as_false_merges_until_scoped_to_the_file():
    pair = [{"a": HARD[0][0], "b": HARD[0][1], "same": False}]
    at_write = quality.evaluate(ResolutionSpec(identify_by=("label",)), pair, corroborate=True, conflict_review=True)
    assert at_write["false_merges"] == 1 and at_write["false_merges_at_write"] == 1
    scoped = quality.evaluate(ResolutionSpec(identify_by=("label",), name_scope="asset", distinguish=("dob",)), pair,
                              corroborate=True, conflict_review=True)
    assert scoped["false_merges"] == 0


def test_a_type_without_identity_reports_every_same_pair_as_missed():
    out = quality.evaluate(None, [{"a": _snap("x", named=False), "b": _snap("y", named=False), "same": True}])
    assert out["rule"] is None and out["missed"] == 1 and out["recall"] == 0.0


def test_labels_are_order_free_and_keyed_by_what_records_say():
    a, b = _snap("Acme", {"x": 1}, "f1"), _snap("ACME", {"x": 1}, "f2")
    assert quality.pair_hash(a, b) == quality.pair_hash(b, a)
    assert quality.pair_hash(a, b) != quality.pair_hash(a, _snap("ACME", {"x": 2}, "f2"))


def test_what_if_settings_are_read_like_saved_ones():
    base = ResolutionSpec(identify_by=("label",))
    s = quality.with_overrides(base, {"scorer": "similar", "auto": 0.9, "distinguish": "dob, email"})
    assert s.scorer == "similar" and s.auto == 0.9 and s.distinguish == ("dob", "email")
    assert quality.with_overrides(base, None) is base


def test_a_spec_round_trips_with_its_new_gates():
    s = ResolutionSpec(identify_by=("a",), distinguish=("dob",), corroborate="always", conflict_review=False,
                       name_scope="asset")
    assert ResolutionSpec.parse(s.to_dict()) == s
    assert ResolutionSpec.parse({"identify_by": ["a"], "corroborate": "sometimes"}).corroborate == "default"
    assert ResolutionSpec.parse({"identify_by": ["a"], "name_scope": "galaxy"}).name_scope == "workspace"


def test_a_what_if_with_a_bad_setting_is_refused_not_scored_as_the_current_rule():
    from pydantic import ValidationError
    from opencortyx.api.schemas.graph.resolution import EvaluateIn
    for bad in ({"auto": "high"}, {"auto": 1.5}, {"scorer": "fuzzy"}, {"threshold": 0.9}, {"corroborate": "sometimes"}):
        with pytest.raises(ValidationError):
            EvaluateIn(node_type="Person", overrides=bad)
    ok = EvaluateIn(node_type="Person", overrides={"auto": 0.9, "distinguish": [" dob ", "dob", ""]})
    assert ok.overrides == {"auto": 0.9, "distinguish": ["dob"]}
    # Review above automatic is judged against the saved rule, not the defaults: 0.97 is fine under a
    # saved automatic of 0.99 and refused under 0.95.
    assert quality.with_overrides(ResolutionSpec(identify_by=("a",), auto=0.99), {"review": 0.97}).review == 0.97
    with pytest.raises(ValueError):
        quality.with_overrides(ResolutionSpec(identify_by=("a",), auto=0.95), {"review": 0.97})
