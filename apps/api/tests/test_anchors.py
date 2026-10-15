"""Anchor rule matching — pure path logic, no DB, no fixtures.

The one exception is `apply`'s TYPING decisions at the bottom, which are stubbed rather than
skipped: "which of two rules wins" is a rule of the feature, not a database behaviour, and it is
exactly the kind of thing that silently becomes rule-order-dependent without a test.
"""
import pytest

from opencortyx.ingest.extraction import anchors as anchors_mod
from opencortyx.ingest.extraction.anchors import (
    AnchorRuleError, apply, match_path, parse_rules, preview,
)

RULES = [
    {"match": "research/alice/**", "node_id": "n-alice", "edge_type": "AUTHORED"},
    {"match": "research/{person}/**", "resolve_as": "Person", "edge_type": "AUTHORED"},
    {"match": "**/*.pdf", "node_id": "n-papers", "edge_type": "HAS_DOC"},
]


def _targets(path, rules):
    return sorted((h.node_id or h.name) for h in match_path(path, rules))


def test_all_matching_rules_apply_not_first_match():
    """A paper IS authored by Alice AND is a PDF. First-match would drop the second edge."""
    assert _targets("research/alice/p1.pdf", parse_rules(RULES)) == ["alice", "n-alice", "n-papers"]


def test_no_match_is_not_an_error():
    assert match_path("notes/todo.md", parse_rules(RULES)) == []


def test_windows_paths_and_case_normalize():
    """The same corpus arrives from Windows shares and POSIX mounts; it must match identically."""
    assert "n-alice" in _targets(r"Research\Alice\p9.PDF", parse_rules(RULES))


def test_capture_is_one_segment_not_a_subtree():
    """`{person}` spanning `/` would capture "bob/sub" and resolve every nested file differently."""
    hits = match_path("research/bob/sub/p3.txt", parse_rules(RULES))
    assert [h.name for h in hits if h.name] == ["bob"]


def test_double_star_matches_zero_directories():
    rules = parse_rules([{"match": "a/**/b.txt", "node_id": "n"}])
    assert match_path("a/b.txt", rules)          # zero dirs
    assert match_path("a/x/y/b.txt", rules)      # many


def test_single_star_does_not_cross_separator():
    """The distinction fnmatch gets wrong, and the reason the glob is hand-compiled."""
    rules = parse_rules([{"match": "research/*/p.pdf", "node_id": "n"}])
    assert match_path("research/alice/p.pdf", rules)
    assert not match_path("research/alice/drafts/p.pdf", rules)


@pytest.mark.parametrize("bad,frag", [
    ({"match": ""}, "no `match`"),
    ({"match": "a/{9x}/b"}, "capture name"),
    ({"match": "a/{unclosed/b"}, "unclosed"),
    ({"match": "a/**"}, "captures nothing"),          # derived rule with no node and no group
])
def test_bad_rules_rejected_at_parse_time(bad, frag):
    """Validation happens on SAVE, not part-way through a 10,000-file run."""
    with pytest.raises(AnchorRuleError) as e:
        parse_rules([bad])
    assert frag in str(e.value)


def test_preview_counts_people_not_spellings():
    """`Alice/` and `alice/` are one researcher — resolution matches case-insensitively, so a raw
    string count would report a folder-naming split that does not exist."""
    rules = parse_rules([{"match": "research/{person}/**", "resolve_as": "Person"}])
    pv = preview(["research/Alice/a.pdf", "research/alice/b.pdf", "research/bob/c.pdf"], rules)
    assert pv["rules"][0]["distinct_names"] == 2         # Alice+alice = one person, plus bob
    assert "Alice" in pv["rules"][0]["names"]            # both spellings still visible


def test_preview_reports_unmatched():
    pv = preview(["research/alice/p.pdf", "notes/todo.md"], parse_rules(RULES))
    assert pv["matched"] == 1 and pv["unmatched"] == 1
    assert pv["unmatched_sample"] == ["notes/todo.md"]


# ── as_type: what the file IS, as opposed to whose it is ────────────────────────────────────────

def test_a_rule_may_only_say_what_the_files_are():
    """`aadhaar/** → Aadhaar` is a COMPLETE rule. Requiring an owner would make the common case —
    a folder per document kind — impossible to express."""
    rules = parse_rules([{"match": "aadhaar/**", "as_type": "Aadhaar"}])
    assert rules[0].is_type_only and not rules[0].is_derived
    hits = match_path("aadhaar/ravi_scan.pdf", rules)
    assert len(hits) == 1
    assert hits[0].node_id is None and hits[0].name is None      # types it, points at nothing
    assert hits[0].rule.as_type == "Aadhaar"


def test_typing_and_targeting_are_independent_halves():
    """One rule can do both: the scan IS an Aadhaar AND belongs to Ravi."""
    rules = parse_rules([{"match": "aadhaar/**", "as_type": "Aadhaar",
                          "node_id": "n-ravi", "edge_type": "HAS_ID"}])
    hit = match_path("aadhaar/scan.pdf", rules)[0]
    assert hit.node_id == "n-ravi"
    assert hit.rule.as_type == "Aadhaar" and hit.rule.edge_type == "HAS_ID"
    assert not hit.rule.is_type_only


def test_as_type_survives_a_derived_target():
    """The captured owner and the declared kind are orthogonal — neither suppresses the other."""
    rules = parse_rules([{"match": "ids/{person}/pan/**", "as_type": "PAN",
                          "resolve_as": "Person", "edge_type": "HAS_ID"}])
    hit = match_path("ids/Ravi/pan/card.pdf", rules)[0]
    assert hit.name == "Ravi" and hit.rule.as_type == "PAN"
    assert hit.rule.is_derived and not hit.rule.is_type_only


def test_different_formats_route_to_different_types_and_owners():
    """The shape the feature exists for: one folder tree, many document kinds, many owners."""
    rules = parse_rules([
        {"match": "ids/{person}/aadhaar/**", "as_type": "Aadhaar", "resolve_as": "Person",
         "edge_type": "HAS_ID"},
        {"match": "ids/{person}/pan/**", "as_type": "PAN", "resolve_as": "Person",
         "edge_type": "HAS_ID"},
        {"match": "contracts/**", "as_type": "Contract", "node_id": "n-acme",
         "edge_type": "HAS_DOCUMENT"},
    ])
    got = {p: [(h.rule.as_type, h.node_id or h.name) for h in match_path(p, rules)]
           for p in ["ids/Ravi/aadhaar/a.pdf", "ids/Ravi/pan/p.jpg", "contracts/msa.docx"]}
    assert got["ids/Ravi/aadhaar/a.pdf"] == [("Aadhaar", "Ravi")]
    assert got["ids/Ravi/pan/p.jpg"] == [("PAN", "Ravi")]
    assert got["contracts/msa.docx"] == [("Contract", "n-acme")]


@pytest.mark.parametrize("bad_type", ["9lives", "has/slash", "with\nnewline", "x" * 81])
def test_unusable_type_names_rejected_at_parse_time(bad_type):
    """`as_type` becomes a node type in an 80-char column and an entry in every type dropdown —
    a hand-edited config must not be able to put a path or a newline there."""
    with pytest.raises(AnchorRuleError) as e:
        parse_rules([{"match": "a/**", "as_type": bad_type}])
    assert "not a usable node type" in str(e.value)


def test_a_rule_that_says_nothing_is_still_rejected():
    """Relaxing the check for `as_type` must not let an empty rule through."""
    with pytest.raises(AnchorRuleError):
        parse_rules([{"match": "a/**"}])


def test_preview_reports_what_each_rule_would_type():
    """The retype has no visible symptom until after it has rewritten every matching asset's
    type, so the count has to be available BEFORE the run."""
    rules = parse_rules([{"match": "aadhaar/**", "as_type": "Aadhaar"},
                         {"match": "pan/**", "as_type": "PAN"}])
    pv = preview(["aadhaar/a.pdf", "aadhaar/b.pdf", "pan/c.pdf", "notes/d.txt"], rules)
    assert [(r["as_type"], r["files"], r["type_only"]) for r in pv["rules"]] == [
        ("Aadhaar", 2, True), ("PAN", 1, True)]
    assert pv["matched"] == 3 and pv["unmatched"] == 1


# ── apply(): the typing half, with the write stubbed out ────────────────────────────────────────

@pytest.fixture
def retypes(monkeypatch):
    """Record what `apply` WOULD write, without a database.

    Only the write is stubbed — the ordering, conflict and skip decisions under test are all in
    `apply` itself and run for real."""
    calls = []

    async def _fake(db, *, workspace_id, node_id, as_type, rule):
        calls.append((as_type, rule))
        return True

    monkeypatch.setattr(anchors_mod, "_retype_asset", _fake)
    return calls


@pytest.mark.asyncio
async def test_first_matching_type_wins_and_the_loser_is_reported(retypes):
    """A node has ONE type, so two rules claiming different ones is a config error. Silent
    last-wins would make the graph depend on the order the rules happen to sit in."""
    rules = parse_rules([{"match": "ids/**", "as_type": "Aadhaar"},
                         {"match": "**/*.pdf", "as_type": "PAN"}])
    out = await apply(None, workspace_id="o", asset_node_id="n1", path="ids/scan.pdf", rules=rules)
    assert out["typed_as"] == "Aadhaar"
    assert [c[0] for c in retypes] == ["Aadhaar"]                  # written once, not twice
    assert out["type_conflicts"] == [{"rule": "**/*.pdf", "as_type": "PAN", "kept": "Aadhaar"}]


@pytest.mark.asyncio
async def test_two_rules_agreeing_on_a_type_is_not_a_conflict(retypes):
    rules = parse_rules([{"match": "ids/**", "as_type": "Aadhaar"},
                         {"match": "**/*.pdf", "as_type": "Aadhaar"}])
    out = await apply(None, workspace_id="o", asset_node_id="n1", path="ids/scan.pdf", rules=rules)
    assert out["typed_as"] == "Aadhaar" and out["type_conflicts"] == []


@pytest.mark.asyncio
async def test_a_type_only_rule_writes_no_edge(retypes):
    """It said what the file is, not whose it is — reaching the edge code at all would need a
    target it does not have."""
    rules = parse_rules([{"match": "aadhaar/**", "as_type": "Aadhaar"}])
    out = await apply(None, workspace_id="o", asset_node_id="n1", path="aadhaar/a.pdf", rules=rules)
    assert out["typed_as"] == "Aadhaar"
    assert out["edges"] == 0 and out["unresolved"] == []
    assert out["matched_rules"] == 1


@pytest.mark.asyncio
async def test_no_asset_node_means_no_work(retypes):
    """Guard the caller relies on: an ingestion that produced no single asset must not retype
    something else by accident."""
    rules = parse_rules([{"match": "**", "as_type": "Aadhaar"}])
    out = await apply(None, workspace_id="o", asset_node_id=None, path="a.pdf", rules=rules)
    assert out["typed_as"] is None and retypes == []
