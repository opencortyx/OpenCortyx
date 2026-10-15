"""Contradiction Detection & Resolution — the engine, exercised without a stack.

Every stage is injected (`Deps`), so these are real tests of the real code paths: no database, no
embedding server, no model. What they pin is the behaviour that used to be impossible:

  * a tenant's OWN field names decide where the text, the date and the reason live
  * a MISSING date is unknown, not old — an undated statement never wins on recency
  * the LLM tier can be turned OFF and detection still works (duplicates, entailment)
  * the model may only identify a disagreement; who wins is decided by code alone
  * an undecided pair writes nothing, and a judged pair is judged once
"""
import asyncio

import pytest

from opencortyx.capabilities.modules.contradiction import (
    RESERVED, Deps, Settings, StatementView, Verdict,
    decide, find_candidates, gate, history_of, is_evolution, judge_by_rules, judge_duplicate,
    judge_pair, parse_time, pick_winner, resolve_statement,
)

ORG = "11111111-1111-1111-1111-111111111111"

# What the judge model is asked for now: each statement's subject and attribute, plus whether the
# values differ. The RELATION is derived from these in code, so these are the answers a test supplies.
SAYS_DIFFERENT = {"a_subject": "acme", "a_attribute": "location",
                  "b_subject": "acme", "b_attribute": "location", "values_differ": True}
SAYS_SAME = {**SAYS_DIFFERENT, "values_differ": False}
SAYS_OTHER_SUBJECT = {"a_subject": "acme", "a_attribute": "location",
                      "b_subject": "globex", "b_attribute": "revenue", "values_differ": True}


def node(nid, text=None, type="Fact", props=None, confidence=1.0, created_at=None, **extra):
    """A node in the plain dict shape the engine reads (same shape the SDK hands to extractors)."""
    p = dict(props or {})
    if text is not None:
        p.setdefault("text", text)
    p.update(extra)
    return {"id": nid, "type": type, "label": p.get("text", ""), "properties": p,
            "confidence": confidence, "created_at": created_at}


class FakeDeps(Deps):
    """Records what the engine did, and answers with whatever a test set up."""

    def __init__(self, semantic=None, linked=None, by_key=None, llm=None, nli=None, existing=()):
        self._semantic = semantic or []
        self._linked = linked or []
        self._by_key = by_key or []
        self._llm = llm
        self._nli = nli
        self._existing = set(existing)
        self.edges: list = []
        self.patches: list = []
        self.llm_calls = 0
        self.nli_calls = 0

    async def load(self, workspace_id, node_id):
        for n, _ in self._semantic:
            if n["id"] == str(node_id):
                return n
        return self._loaded

    async def semantic(self, workspace_id, view, cfg):
        return [(n, s) for n, s in self._semantic if n["id"] != view.id and s >= cfg.similarity]

    async def linked(self, workspace_id, view, cfg):
        return [n for n in self._linked if n["id"] != view.id]

    async def by_key(self, workspace_id, view, cfg):
        return [n for n in self._by_key if n["id"] != view.id]

    async def edge_exists(self, workspace_id, a_id, b_id, type):
        return (str(a_id), str(b_id), type) in self._existing

    async def write_edge(self, workspace_id, src, dst, type, properties):
        self.edges.append((str(src), str(dst), type))

    async def patch_props(self, workspace_id, node_id, props):
        self.patches.append((str(node_id), props))

    async def llm_json(self, workspace_id, prompt, system, cfg):
        self.llm_calls += 1
        if isinstance(self._llm, Exception):
            raise self._llm
        return self._llm or {}

    async def nli(self, a_text, b_text):
        self.nli_calls += 1
        return self._nli


def run(coro):
    """Each case gets its own loop, and closes it — an unclosed loop leaks a file descriptor and
    pytest reports it as an unraisable exception at the end of an otherwise green run."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Stage 1: reading a statement through the TENANT's field names ────────────────────────────────
def test_reads_text_from_a_tenant_field_name():
    cfg = Settings(text_fields=["assertion"], time_fields=["captured_on"])
    v = StatementView.of(node("a", type="Note", assertion="Acme is in Delhi", captured_on="2026-03-01"), cfg)
    assert v.text == "Acme is in Delhi"
    assert v.time_known and v.time.year == 2026


def test_falls_back_to_the_label_when_no_text_field_matches():
    cfg = Settings(text_fields=["nope"])
    v = StatementView.of({"id": "a", "type": "Fact", "label": "Acme is in Delhi", "properties": {}}, cfg)
    assert v.text == "Acme is in Delhi"


def test_a_node_with_no_assertion_is_not_a_statement():
    assert StatementView.of({"id": "a", "type": "Fact", "label": "", "properties": {}}, Settings()) is None


def test_missing_date_is_unknown_not_old():
    v = StatementView.of(node("a", "Acme is in Delhi"), Settings())
    assert v.time is None and v.time_known is False


@pytest.mark.parametrize("raw,year", [
    ("2026-03-11T09:00:00Z", 2026), ("2026-03-11", 2026), ("2026-03", 2026), ("2026", 2026),
    (1772000000, 2026),
])
def test_dates_are_read_in_the_shapes_tenants_actually_store(raw, year):
    assert parse_time(raw).year == year


@pytest.mark.parametrize("raw", [None, "", "  ", "not a date", True, {}, []])
def test_unparseable_dates_are_unknown(raw):
    assert parse_time(raw) is None


def test_trust_boost_applies_to_validated_statements():
    cfg = Settings(trust_boost=0.5)
    plain = StatementView.of(node("a", "x", confidence=0.5), cfg)
    human = StatementView.of(node("b", "x", confidence=0.5, __validated=True), cfg)
    assert human.trust > plain.trust


# ── Stage 2: candidates — no field names required ────────────────────────────────────────────────
def test_candidates_come_from_meaning_and_from_links_without_any_key():
    cfg = Settings()
    me = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    deps = FakeDeps(semantic=[(node("b", "Acme is in Mumbai"), 0.9)],
                    linked=[node("c", "Acme is in Chennai")])
    got = run(find_candidates(ORG, me, cfg, deps))
    assert {c.id for c in got} == {"b", "c"}


def test_candidates_below_the_similarity_bar_are_not_compared():
    cfg = Settings(similarity=0.9)
    me = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    deps = FakeDeps(semantic=[(node("b", "Unrelated thing"), 0.5)])
    assert run(find_candidates(ORG, me, cfg, deps)) == []


def test_superseded_statements_are_never_candidates():
    cfg = Settings()
    me = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    deps = FakeDeps(semantic=[(node("b", "Acme is in Mumbai", superseded="true"), 0.95)])
    assert run(find_candidates(ORG, me, cfg, deps)) == []


def test_declared_keys_add_candidates_when_a_tenant_configures_them():
    cfg = Settings(key_fields=["customer_id"])
    me = StatementView.of(node("a", "Plan is Starter", customer_id="C1"), cfg)
    deps = FakeDeps(by_key=[node("b", "Plan is Enterprise", customer_id="C1")])
    assert [c.id for c in run(find_candidates(ORG, me, cfg, deps))] == ["b"]


def test_comparisons_are_bounded_by_the_cost_control():
    cfg = Settings(candidates=2)
    me = StatementView.of(node("a", "x"), cfg)
    deps = FakeDeps(semantic=[(node(str(i), f"y{i}"), 0.99) for i in range(10)])
    assert len(run(find_candidates(ORG, me, cfg, deps))) == 2


# ── Stage 3: the judge cascade ───────────────────────────────────────────────────────────────────
def test_identical_text_is_a_duplicate_decided_without_a_model():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "acme IS in  delhi!"), cfg)
    v = judge_duplicate(a, b, cfg)
    assert v and v.relation == "duplicate" and v.method == "duplicate"


def test_short_statements_differing_in_one_word_are_not_duplicates():
    """The bug the live run caught: over five words, "billing currency is EUR" and "… USD" sat inside
    the SimHash duplicate threshold, so a real contradiction was swallowed as a repeat."""
    cfg = Settings()
    a = StatementView.of(node("a", "The billing currency is EUR."), cfg)
    b = StatementView.of(node("b", "The billing currency is USD."), cfg)
    assert judge_duplicate(a, b, cfg) is None


def test_short_near_miss_still_reaches_the_judge():
    cfg = Settings()
    a = StatementView.of(node("a", "The default region is eu-west-1."), cfg)
    b = StatementView.of(node("b", "The default region is us-east-1."), cfg)
    deps = FakeDeps(llm=SAYS_DIFFERENT)
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "contradiction" and deps.llm_calls == 1


def test_near_identical_long_texts_go_to_the_judge_not_the_duplicate_tier():
    """Measured: one word changed in a 23-word statement is 6 bits, a whole clause added is also 6,
    and an unrelated statement of the same shape is 28. No fingerprint threshold separates a tidier
    restatement from a changed value, so near-duplicates are judged, not assumed identical."""
    cfg = Settings()
    long_a = ("The quarterly compliance review covers access control, data retention, encryption at "
              "rest, incident response and vendor risk for every production system.")
    long_b = long_a.replace("vendor risk", "vendor risks")
    a, b = StatementView.of(node("a", long_a), cfg), StatementView.of(node("b", long_b), cfg)
    assert judge_duplicate(a, b, cfg) is None

    changed_value = long_a.replace("every production system", "three production systems")
    c = StatementView.of(node("c", changed_value), cfg)
    assert judge_duplicate(a, c, cfg) is None
    deps = FakeDeps(llm=SAYS_DIFFERENT)
    assert run(judge_pair(ORG, a, c, cfg, deps)).relation == "contradiction"


def test_whitespace_and_punctuation_do_not_defeat_the_exact_match():
    cfg = Settings()
    a = StatementView.of(node("a", "The retention window is 90 days."), cfg)
    b = StatementView.of(node("b", "the  retention   window is 90 DAYS!!"), cfg)
    v = judge_duplicate(a, b, cfg)
    assert v and v.relation == "duplicate"


def test_the_verdict_trail_distinguishes_no_candidates_from_no_conflict():
    cfg = Settings()
    deps = _deps_for(node("a", "Mumbai"), node("b", "Delhi"), llm=SAYS_SAME)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert r["candidates"] == 1
    assert [v["relation"] for v in r["verdicts"]] == ["compatible"]
    assert r["verdicts"][0]["method"] == "llm"
    # The pair was compared and AGREED, which is now recorded rather than discarded — but nothing was
    # retired, which is what this test exists to pin.
    assert [o["kind"] for o in r["outcomes"]] == ["support"]
    assert not any(o["kind"] in ("supersede", "evolve", "propose") for o in r["outcomes"])


def test_a_duplicate_never_reaches_the_llm():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi"), cfg)
    deps = FakeDeps(llm=SAYS_DIFFERENT)
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "duplicate" and deps.llm_calls == 0


def test_llm_off_still_catches_duplicates():
    cfg = Settings(use_llm=False)
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi"), cfg)
    deps = FakeDeps()
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "duplicate" and deps.llm_calls == 0


def test_llm_off_leaves_an_unrelated_pair_undecided_instead_of_guessing():
    cfg = Settings(use_llm=False)
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Revenue grew 4% last quarter"), cfg)
    deps = FakeDeps()
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "unknown" and deps.llm_calls == 0


def test_a_confident_entailment_model_decides_without_the_llm():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Acme is in Mumbai"), cfg)
    deps = FakeDeps(nli=0.95, llm=SAYS_SAME)
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "contradiction" and v.method == "nli" and deps.llm_calls == 0


def test_an_uncertain_entailment_score_escalates_to_the_llm():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Acme is in Mumbai"), cfg)
    deps = FakeDeps(nli=0.5, llm=SAYS_DIFFERENT)
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "contradiction" and v.method == "llm" and deps.llm_calls == 1


def test_a_judge_outage_is_not_a_verdict():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Delhi"), cfg)
    b = StatementView.of(node("b", "Acme is in Mumbai"), cfg)
    deps = FakeDeps(llm=RuntimeError("model server down"))
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "unknown"


def test_a_malformed_judge_answer_is_undecided_not_a_contradiction():
    cfg = Settings()
    a = StatementView.of(node("a", "x"), cfg)
    b = StatementView.of(node("b", "y"), cfg)
    v = run(judge_pair(ORG, a, b, cfg, FakeDeps(llm={"nonsense": True})))
    assert v.relation == "unknown"


# ── Stage 4: the policy is deterministic ─────────────────────────────────────────────────────────
def test_a_stated_reason_makes_it_an_evolution():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Mumbai", reason="moved office"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi"), cfg)
    assert is_evolution(a, b)


def test_different_known_dates_make_it_an_evolution():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Mumbai", valid_from="2026-03-01"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi", valid_from="2025-01-01"), cfg)
    assert is_evolution(a, b)


def test_two_undated_statements_are_a_flat_conflict_not_an_evolution():
    cfg = Settings()
    a = StatementView.of(node("a", "Acme is in Mumbai"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi"), cfg)
    assert not is_evolution(a, b)


def test_the_newer_statement_wins_under_latest_wins():
    cfg = Settings(mode="latest_wins")
    a = StatementView.of(node("a", "Mumbai", valid_from="2026-03-01"), cfg)
    b = StatementView.of(node("b", "Delhi", valid_from="2025-01-01"), cfg)
    assert pick_winner(a, b, cfg)[0].id == "a"


def test_a_dated_statement_beats_an_undated_one():
    cfg = Settings(mode="latest_wins")
    dated = StatementView.of(node("a", "Mumbai", valid_from="2025-01-01"), cfg)
    undated = StatementView.of(node("b", "Delhi"), cfg)
    assert pick_winner(dated, undated, cfg)[0].id == "a"
    assert pick_winner(undated, dated, cfg)[0].id == "a"


def test_trust_weighted_prefers_the_validated_statement_over_the_newer_one():
    cfg = Settings(mode="trust_weighted", trust_boost=1.0)
    newer = StatementView.of(node("a", "Mumbai", confidence=0.5, valid_from="2026-03-01"), cfg)
    validated = StatementView.of(node("b", "Delhi", confidence=0.5, __validated=True,
                                      valid_from="2025-01-01"), cfg)
    assert pick_winner(newer, validated, cfg)[0].id == "b"


def test_the_winner_is_stable_when_there_is_no_signal_at_all():
    cfg = Settings()
    a = StatementView.of(node("a", "Mumbai"), cfg)
    b = StatementView.of(node("b", "Delhi"), cfg)
    assert pick_winner(a, b, cfg)[0].id == pick_winner(b, a, cfg)[0].id


def test_an_evolution_keeps_both_and_a_flat_conflict_supersedes():
    cfg = Settings()
    evolving = decide(
        StatementView.of(node("a", "Mumbai", valid_from="2026-03-01"), cfg),
        StatementView.of(node("b", "Delhi", valid_from="2025-01-01"), cfg),
        Verdict("contradiction", 0.9, "llm"), cfg)
    flat = decide(StatementView.of(node("a", "Mumbai"), cfg),
                  StatementView.of(node("b", "Delhi"), cfg),
                  Verdict("contradiction", 0.9, "llm"), cfg)
    assert evolving.kind == "evolve" and flat.kind == "supersede"


def test_review_mode_proposes_instead_of_retiring():
    cfg = Settings(review=True)
    out = decide(StatementView.of(node("a", "Mumbai"), cfg),
                 StatementView.of(node("b", "Delhi"), cfg),
                 Verdict("contradiction", 1.0, "llm"), cfg)
    assert out.kind == "propose"


def test_a_low_confidence_verdict_is_proposed_not_applied():
    cfg = Settings(auto_apply=0.9)
    out = decide(StatementView.of(node("a", "Mumbai"), cfg),
                 StatementView.of(node("b", "Delhi"), cfg),
                 Verdict("contradiction", 0.5, "llm"), cfg)
    assert out.kind == "propose"


@pytest.mark.parametrize("relation", ["unknown", "unrelated"])
def test_an_undecided_pair_changes_nothing(relation):
    """The design rule: when no tier could settle it, the statements are left exactly as they were."""
    cfg = Settings()
    out = decide(StatementView.of(node("a", "x"), cfg), StatementView.of(node("b", "y"), cfg),
                 Verdict(relation, 1.0, "llm"), cfg)
    assert out.kind == "none"


@pytest.mark.parametrize("relation,kind", [("duplicate", "consolidate"), ("compatible", "support")])
def test_the_agree_branch_is_recorded_not_discarded(relation, kind):
    """LAYER 6/7. Agreement used to return "none" — the candidates, the embedding and the judge call
    were all paid for and the answer thrown away. Two statements agreeing is evidence."""
    cfg = Settings()
    out = decide(StatementView.of(node("a", "x"), cfg), StatementView.of(node("b", "y"), cfg),
                 Verdict(relation, 1.0, "llm"), cfg)
    assert out.kind == kind


@pytest.mark.parametrize("relation", ["duplicate", "compatible"])
def test_agreement_can_be_switched_off(relation):
    """`contradiction_support` off restores the old behaviour exactly: only disagreement is recorded."""
    cfg = Settings(support=False)
    out = decide(StatementView.of(node("a", "x"), cfg), StatementView.of(node("b", "y"), cfg),
                 Verdict(relation, 1.0, "llm"), cfg)
    assert out.kind == "none"


# ── end to end, through the real entry point ─────────────────────────────────────────────────────
def _deps_for(me, other, **kw):
    deps = FakeDeps(semantic=[(other, 0.95)], **kw)
    deps._loaded = me
    return deps


def test_end_to_end_flat_conflict_supersedes_and_closes_the_window():
    cfg = Settings()
    me, other = node("a", "Acme is in Mumbai"), node("b", "Acme is in Delhi")
    deps = _deps_for(me, other, llm=SAYS_DIFFERENT)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))

    assert [o["kind"] for o in r["outcomes"]] == ["supersede"]
    assert ("a", "b", "CONTRADICTS") in deps.edges
    assert any(t == "SUPERSEDES" for _s, _d, t in deps.edges)
    (_nid, props), = [p for p in deps.patches if p[1].get("superseded") == "true"]
    assert props["superseded_by"] and props["valid_until"] and props["__confidence"] == 0


def test_end_to_end_evolution_keeps_both_and_only_closes_the_older_window():
    cfg = Settings()
    me = node("a", "Acme is in Mumbai", valid_from="2026-03-01")
    other = node("b", "Acme is in Delhi", valid_from="2025-01-01")
    deps = _deps_for(me, other, llm=SAYS_DIFFERENT)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))

    assert [o["kind"] for o in r["outcomes"]] == ["evolve"]
    assert any(t == "EVOLVED_FROM" for _s, _d, t in deps.edges)
    assert not any(t == "SUPERSEDES" for _s, _d, t in deps.edges)
    (nid, props), = deps.patches
    assert nid == "b" and props["valid_until"] and "superseded" not in props


def test_end_to_end_review_writes_a_proposal_and_retires_nothing():
    cfg = Settings(review=True)
    deps = _deps_for(node("a", "Mumbai"), node("b", "Delhi"),
                     llm=SAYS_DIFFERENT)
    run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    (_nid, props), = deps.patches
    assert props["superseded"] == "proposed" and "__confidence" not in props
    assert not any(t == "SUPERSEDES" for _s, _d, t in deps.edges)


def test_an_already_judged_pair_is_not_judged_again():
    cfg = Settings()
    deps = _deps_for(node("a", "Mumbai"), node("b", "Delhi"),
                     llm=SAYS_DIFFERENT,
                     existing=[("a", "b", "CONTRADICTS")])
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert r["judged"] == 0 and deps.edges == [] and deps.llm_calls == 0


def test_nothing_is_written_when_the_judge_cannot_decide():
    cfg = Settings()
    deps = _deps_for(node("a", "Mumbai"), node("b", "Delhi"), llm=SAYS_OTHER_SUBJECT)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert r["outcomes"] == [] and deps.edges == [] and deps.patches == []


def test_a_statement_with_no_candidates_is_a_normal_outcome():
    cfg = Settings()
    deps = FakeDeps()
    deps._loaded = node("a", "Acme is in Mumbai")
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert r["candidates"] == 0 and r["outcomes"] == []


def test_the_engine_does_nothing_at_all_when_turned_off():
    deps = _deps_for(node("a", "Mumbai"), node("b", "Delhi"),
                     llm=SAYS_DIFFERENT)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=Settings(enabled=False)))
    assert r == {"skipped": "disabled"} and deps.edges == []


def test_a_superseded_statement_is_not_re_judged():
    cfg = Settings()
    deps = _deps_for(node("a", "Mumbai", superseded="true"), node("b", "Delhi"))
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert r == {"skipped": "superseded"}


def test_tenant_field_names_work_end_to_end_with_no_date_anywhere():
    """The case that used to detect nothing: a tenant's own field names, and not a date in sight."""
    cfg = Settings(text_fields=["assertion"], time_fields=["effective_on"], use_llm=True)
    me = node("a", type="Record", assertion="Plan is Enterprise")
    other = node("b", type="Record", assertion="Plan is Starter")
    deps = _deps_for(me, other, llm=SAYS_DIFFERENT)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert [o["kind"] for o in r["outcomes"]] == ["supersede"]


# ── the judge reports; code concludes ────────────────────────────────────────────────────────────
def test_the_relation_is_derived_in_code_from_what_the_model_reported():
    cfg = Settings()
    a, b = StatementView.of(node("a", "Mumbai"), cfg), StatementView.of(node("b", "Delhi"), cfg)
    assert run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=SAYS_DIFFERENT))).relation == "contradiction"
    assert run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=SAYS_SAME))).relation == "compatible"
    assert run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=SAYS_OTHER_SUBJECT))).relation == "unrelated"


def test_a_different_subject_is_unrelated_even_when_the_values_differ():
    """The trap: two statements of the same shape about DIFFERENT subjects ("Acme is in Mumbai" /
    "Globex is in Delhi") do report differing values. The subject check is what saves it."""
    cfg = Settings()
    a, b = StatementView.of(node("a", "Acme is in Mumbai"), cfg), StatementView.of(node("b", "Globex is in Delhi"), cfg)
    v = run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=SAYS_OTHER_SUBJECT)))
    assert v.relation == "unrelated"


@pytest.mark.parametrize("raw,expected", [
    (True, "contradiction"), ("true", "contradiction"), ("yes", "contradiction"),
    (False, "compatible"), ("false", "compatible"), ("no", "compatible"),
])
def test_string_booleans_from_the_model_are_honoured(raw, expected):
    cfg = Settings()
    a, b = StatementView.of(node("a", "Mumbai"), cfg), StatementView.of(node("b", "Delhi"), cfg)
    deps = FakeDeps(llm={**SAYS_DIFFERENT, "values_differ": raw})
    assert run(judge_pair(ORG, a, b, cfg, deps)).relation == expected


def test_confidence_comes_from_the_measured_similarity_not_the_model():
    """The judge's self-reported confidence was 0.0 and 0.5 at random, and `auto_apply` decides
    whether a statement is retired without a human — so the pair's own similarity is used."""
    cfg = Settings()
    me = StatementView.of(node("a", "Mumbai"), cfg)
    deps = FakeDeps(semantic=[(node("b", "Delhi"), 0.93)], llm=SAYS_DIFFERENT)
    peer, = run(find_candidates(ORG, me, cfg, deps))
    assert peer.score == 0.93
    assert run(judge_pair(ORG, me, peer, cfg, deps)).confidence == 0.93


def test_a_candidate_found_without_a_score_stays_below_the_auto_apply_gate():
    """Found by link or key, never scored: it must not be retired automatically."""
    cfg = Settings()
    me = StatementView.of(node("a", "Mumbai"), cfg)
    deps = FakeDeps(linked=[node("b", "Delhi")], llm=SAYS_DIFFERENT)
    peer, = run(find_candidates(ORG, me, cfg, deps))
    v = run(judge_pair(ORG, me, peer, cfg, deps))
    assert v.confidence < cfg.auto_apply
    assert decide(me, peer, v, cfg).kind == "propose"


def test_short_subject_names_are_not_silently_unrelated():
    """"HQ", "ID", "IP", "AZ" are real attribute names. Dropping words under three characters emptied
    the comparison, and an empty comparison scored zero — so those pairs were ruled unrelated with no
    sign anything had happened."""
    cfg = Settings()
    a, b = StatementView.of(node("a", "Mumbai"), cfg), StatementView.of(node("b", "Delhi"), cfg)
    short = {"a_subject": "HQ", "a_attribute": "IP", "b_subject": "HQ", "b_attribute": "IP",
             "values_differ": True}
    assert run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=short))).relation == "contradiction"

    different = {**short, "b_subject": "DR"}
    assert run(judge_pair(ORG, a, b, cfg, FakeDeps(llm=different))).relation == "unrelated"


def _pair(a_props, b_props, origin="linked", cfg=None, keys=None):
    cfg = cfg or Settings(key_fields=list(keys or []))
    a = StatementView.of(node("a", "statement a", props=a_props), cfg)
    b = StatementView.of(node("b", "statement b", props=b_props), cfg)
    b.origin = origin
    return a, b, cfg


@pytest.mark.parametrize("x,y", [
    ("90 days", "120 days"),          # same unit, different magnitude
    ("4 hours", "8 hours"),
    (4, 8),                           # bare numbers
    ("2024-01-15", "2024-06-01"),     # dates
    ("EUR 100", "USD 100"),           # same amount, different currency IS a different value
    ("$100", "$250"),
    ("10%", "25%"),
    (True, False),
    ("yes", "no"),
    ("eu-west-1", "ap-south-1"),      # identifiers: machine codes have no synonyms
    ("SKU-123", "SKU-456"),
    ("v2.1", "v2.2"),
])
def test_rules_settle_certain_conflicts_with_no_model(x, y):
    a, b, cfg = _pair({"sla": x}, {"sla": y})
    v = judge_by_rules(a, b, cfg)
    assert v is not None and v.relation == "contradiction"
    assert v.method == "rule" and v.lane == "clear" and v.confidence == 1.0


@pytest.mark.parametrize("x,y", [
    ("90 days", "3 months"),          # the same duration said two ways
    ("24 hours", "1 day"),
    ("1,000", "1000"),                # thousands separator
    (100, 100.0),
    ("EUR 100", "EUR 100"),
])
def test_rules_never_call_the_same_value_a_conflict(x, y):
    """The false-positive case. '90 days' and '3 months' are one value in two notations; calling that
    a contradiction would retire a perfectly good statement."""
    a, b, cfg = _pair({"term": x}, {"term": y})
    assert judge_by_rules(a, b, cfg) is None


@pytest.mark.parametrize("x,y", [
    ("Mumbai", "Bombay"),             # synonyms — code cannot know
    ("active", "Active - renewed"),
    ("90", "90 days"),                # bare number vs a quantity: not comparable
    ("5 widgets", "7 widgets"),       # a unit the engine does not model
    ("PostgreSQL 16", "MySQL 8"),     # prose, not a code: whitespace disqualifies it
    ("US", "USA"),                    # a word-like abbreviation can still be a synonym
])
def test_rules_decline_what_they_cannot_be_certain_about(x, y):
    """Declining is the whole safety property: these fall through to the semantic tier rather than
    being guessed at by arithmetic."""
    a, b, cfg = _pair({"place": x}, {"place": y})
    assert judge_by_rules(a, b, cfg) is None


def test_rules_never_fire_on_a_merely_similar_neighbour():
    """THE false positive that matters most: two different customers each have a `region`. They are
    semantically similar and their values differ, but that is not a conflict — it is two records."""
    a, b, cfg = _pair({"region": "eu-west-1"}, {"region": "ap-south-1"}, origin="semantic")
    assert judge_by_rules(a, b, cfg) is None
    # ... and the same pair IS judged once the graph says they describe the same subject.
    b.origin = "linked"
    assert judge_by_rules(a, b, cfg).relation == "contradiction"


def test_declared_keys_that_disagree_are_settled_as_unrelated_without_a_model():
    cfg = Settings(key_fields=["customer_id"])
    a = StatementView.of(node("a", "plan is enterprise", props={"customer_id": "C1"}), cfg)
    b = StatementView.of(node("b", "plan is starter", props={"customer_id": "C2"}), cfg)
    b.origin = "key"
    v = judge_by_rules(a, b, cfg)
    assert v is not None and v.relation == "unrelated" and v.method == "rule"


def test_rules_never_report_agreement():
    """Deliberate asymmetry: matching structured values do not certify the prose around them, and a
    false SUPPORT inflates confidence in something nobody verified. Agreement is LAYER 3b's to call."""
    a, b, cfg = _pair({"region": "eu-west-1"}, {"region": "eu-west-1"})
    assert judge_by_rules(a, b, cfg) is None


def test_system_attributes_are_never_compared_as_tenant_values():
    """A node already marked by the engine must not be judged on its own bookkeeping."""
    a, b, cfg = _pair({"region": "eu-west-1", RESERVED.VALID_UNTIL: "2024-01-01"},
                      {"region": "eu-west-1", RESERVED.VALID_UNTIL: "2025-09-09"})
    assert set(a.values) == {"region"} and RESERVED.VALID_UNTIL not in a.values
    assert judge_by_rules(a, b, cfg) is None


def test_the_rule_tier_can_be_switched_off():
    a, b, cfg = _pair({"sla": "4 hours"}, {"sla": "8 hours"}, cfg=Settings(use_rules=False))
    assert judge_by_rules(a, b, cfg) is None


def test_rules_run_before_the_model_and_save_the_call():
    cfg = Settings()
    a = StatementView.of(node("a", "sla", props={"sla_hours": 4}), cfg)
    b = StatementView.of(node("b", "sla", props={"sla_hours": 8}), cfg)
    b.origin = "linked"
    deps = FakeDeps(llm=SAYS_SAME)                          # the model would have said "compatible"
    v = run(judge_pair(ORG, a, b, cfg, deps))
    assert v.relation == "contradiction" and v.method == "rule"
    assert deps.llm_calls == 0 and deps.nli_calls == 0


def test_rules_still_work_with_the_llm_switched_off():
    """The toggle degrades coverage, it does not disable detection."""
    cfg = Settings(use_llm=False)
    a = StatementView.of(node("a", "term", props={"term": "90 days"}), cfg)
    b = StatementView.of(node("b", "term", props={"term": "120 days"}), cfg)
    b.origin = "linked"
    v = run(judge_pair(ORG, a, b, cfg, FakeDeps()))
    assert v.relation == "contradiction" and v.method == "rule"


# ══ LAYER 5 — THE CONFIDENCE GATE ═════════════════════════════════════════════════════════════════
def test_the_gate_accepts_on_confidence_and_routes_the_rest_by_lane():
    cfg = Settings(auto_apply=0.75)
    assert gate(Verdict("contradiction", 1.0, "rule", lane="clear"), cfg) == "accept"
    assert gate(Verdict("contradiction", 0.9, "llm", lane="complex"), cfg) == "accept"
    assert gate(Verdict("contradiction", 0.5, "llm", lane="uncertain"), cfg) == "review"
    # nothing outside the model says these are even related, and it is not confident → a person
    assert gate(Verdict("contradiction", 0.5, "llm", lane="complex"), cfg) == "human"


def test_review_mode_sends_everything_to_a_human():
    cfg = Settings(review=True)
    assert gate(Verdict("contradiction", 1.0, "rule", lane="clear"), cfg) == "human"


def test_a_proposal_records_how_badly_a_person_is_needed():
    cfg = Settings(auto_apply=0.75)
    a = StatementView.of(node("a", "Acme is in Mumbai"), cfg)
    b = StatementView.of(node("b", "Acme is in Delhi"), cfg)
    out = decide(a, b, Verdict("contradiction", 0.5, "llm", lane="complex"), cfg)
    assert out.kind == "propose" and out.priority == "human"
    assert out.to_dict()["priority"] == "human"


# ══ LAYER 7 — REINFORCEMENT ═══════════════════════════════════════════════════════════════════════
def _agreeing(conf_a=0.5, conf_b=0.5, src_a="s1", src_b="s2"):
    a = node("a", "The retention window is 90 days.", confidence=conf_a)
    b = node("b", "Retention is ninety days.", confidence=conf_b)
    a["source_ref"], b["source_ref"] = src_a, src_b
    return a, b


def test_agreement_writes_a_supports_edge_and_raises_belief():
    cfg = Settings()
    a, b = _agreeing()
    deps = _deps_for(a, b, llm=SAYS_SAME)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert [o["kind"] for o in r["outcomes"]] == ["support"]
    assert ("a", "b", "SUPPORTS") in deps.edges
    raised = {nid: props for nid, props in deps.patches}
    assert raised and all(p[RESERVED.CONFIDENCE] > 0.5 for p in raised.values())
    assert all(p[RESERVED.SUPPORT_COUNT] == 1 for p in raised.values())


def test_reinforcement_is_bounded_and_never_reaches_certainty():
    """`cur + step * (1 - cur)` approaches the cap asymptotically: repetition cannot manufacture a
    fact, and a much-repeated statement never outranks a human-validated one."""
    cfg = Settings(support_step=0.5, support_cap=0.95)
    cur = 0.0
    for _ in range(50):
        cur = min(cfg.support_cap, cur + cfg.support_step * (1.0 - cur))
    assert cur <= cfg.support_cap < 1.0


def test_the_same_source_twice_is_not_corroboration():
    """Re-ingesting one document must not inflate every fact in it."""
    cfg = Settings()
    a, b = _agreeing(src_a="same-doc", src_b="same-doc")
    deps = _deps_for(a, b, llm=SAYS_SAME)
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert ("a", "b", "SUPPORTS") in deps.edges            # they really do agree — record it
    assert deps.patches == []                              # ... but nothing was learned
    assert r["outcomes"][0]["reinforced"] is False


def test_reinforcement_is_idempotent():
    """A sweep, the peer's own write and a curator's edit all re-check the same pair."""
    cfg = Settings()
    a, b = _agreeing()
    deps = _deps_for(a, b, llm=SAYS_SAME, existing=[("a", "b", "SUPPORTS")])
    r = run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    # the pair is already decided, so it is not even re-judged — no second model call, no second raise
    assert deps.llm_calls == 0 and deps.patches == [] and r["judged"] == 0


def test_a_contradiction_never_reinforces():
    cfg = Settings()
    a, b = _agreeing()
    deps = _deps_for(a, b, llm=SAYS_DIFFERENT)
    run(resolve_statement(ORG, "a", deps=deps, cfg=cfg))
    assert not any(t == "SUPPORTS" for _s, _t, t in deps.edges)
    assert any(t == "CONTRADICTS" for _s, _t, t in deps.edges)


# ══ LAYER 8 — HISTORY ═════════════════════════════════════════════════════════════════════════════
class _ChainDeps(FakeDeps):
    """A three-version history: c (current) -> b -> a (oldest)."""

    def __init__(self, nodes, chain):
        super().__init__()
        self._nodes = {n["id"]: n for n in nodes}
        self._chain = chain

    async def load(self, workspace_id, node_id):
        return self._nodes.get(str(node_id))

    async def evolved_from(self, workspace_id, node_id):
        return self._chain.get(str(node_id))


def test_history_walks_the_chain_newest_first():
    """'Where are they now?' is the head; 'where were they before?' is one hop back. An exact graph
    answer — which is the whole reason a superseded statement is kept rather than deleted."""
    nodes = [
        node("c", "Priya is in Tokyo", props={RESERVED.VALID_FROM: "2026-09-01"}),
        node("b", "Priya is in Berlin",
             props={RESERVED.VALID_FROM: "2026-05-01", RESERVED.VALID_UNTIL: "2026-09-01"}),
        node("a", "Priya is in Mumbai",
             props={RESERVED.VALID_FROM: "2026-01-01", RESERVED.VALID_UNTIL: "2026-05-01"}),
    ]
    deps = _ChainDeps(nodes, {"c": "b", "b": "a"})
    h = run(history_of(ORG, "c", deps=deps))
    assert [e["id"] for e in h] == ["c", "b", "a"]
    assert [e["current"] for e in h] == [True, False, False]
    assert h[1]["text"] == "Priya is in Berlin"            # "where did she travel BEFORE?"


def test_history_terminates_on_a_cycle():
    """A cycle is a data bug, not a reason to hang the request."""
    nodes = [node("a", "one"), node("b", "two")]
    deps = _ChainDeps(nodes, {"a": "b", "b": "a"})
    h = run(history_of(ORG, "a", deps=deps))
    assert [e["id"] for e in h] == ["a", "b"]


def test_history_of_a_statement_that_never_changed_is_just_itself():
    deps = _ChainDeps([node("a", "only version")], {})
    assert [e["id"] for e in run(history_of(ORG, "a", deps=deps))] == ["a"]


def test_a_structural_candidate_keeps_its_origin_even_when_semantic_found_it_first():
    """Semantic runs first and dedupes by id. Without the upgrade, a statement that is BOTH similar
    and linked kept origin="semantic" purely because of source order, and LAYER 3a then refused to
    judge it — measured on the real graph, two SLA statements on one entity went to the LLM at
    lane=complex when arithmetic should have settled them with no model call."""
    cfg = Settings()
    me = StatementView.of(node("a", "Support response time", props={"sla": "4 hours"}), cfg)
    peer = node("b", "Support response time commitment", props={"sla": "8 hours"})
    got = run(find_candidates(ORG, me, cfg, FakeDeps(semantic=[(peer, 0.95)], linked=[peer])))
    assert len(got) == 1, "the same node must not be listed twice"
    assert got[0].origin == "linked"
    assert got[0].score == 0.95, "the measured similarity must survive the upgrade"
    assert judge_by_rules(me, got[0], cfg).relation == "contradiction"


def test_an_exact_key_match_outranks_both_other_sources():
    cfg = Settings(key_fields=["customer_id"])
    me = StatementView.of(node("a", "Plan is Starter", customer_id="C1"), cfg)
    peer = node("b", "Plan is Enterprise", customer_id="C1")
    got = run(find_candidates(ORG, me, cfg,
                              FakeDeps(semantic=[(peer, 0.9)], linked=[peer], by_key=[peer])))
    assert len(got) == 1 and got[0].origin == "key"


def test_a_merely_similar_candidate_is_not_promoted():
    cfg = Settings()
    me = StatementView.of(node("a", "x", props={"region": "eu-west-1"}), cfg)
    peer = node("b", "y", props={"region": "ap-south-1"})
    got = run(find_candidates(ORG, me, cfg, FakeDeps(semantic=[(peer, 0.95)])))
    assert got[0].origin == "semantic"
    assert judge_by_rules(me, got[0], cfg) is None
