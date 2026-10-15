"""D35: a watched folder that cannot be listed never makes its files look deleted, and a pass that
could list nothing at all fails instead of completing over zero files."""
from opencortyx.ingest.sources import sync


def _manifest(*paths):
    return {p: {"sig": "1:1", "source_id": f"s-{i}"} for i, p in enumerate(paths)}


def test_files_under_an_unreadable_folder_are_not_removed():
    m = _manifest("c|p|/host/a/x.json", "c|p|/host/a/sub/y.json", "c|p|/host/b/z.json")
    plan = sync.compute_plan(m, [("z.json", "c|p|/host/b/z.json", "1:1")])
    spared = sync.spare_unreadable(plan, ["c|p|/host/a"])
    assert {it["path"] for it in spared} == {"c|p|/host/a/x.json", "c|p|/host/a/sub/y.json"}
    assert plan["removed"] == []


def test_a_readable_folder_still_reports_real_removals():
    m = _manifest("c|p|/host/a/x.json", "c|p|/host/b/gone.json")
    plan = sync.compute_plan(m, [])
    spared = sync.spare_unreadable(plan, ["c|p|/host/a"])
    assert [it["path"] for it in spared] == ["c|p|/host/a/x.json"]
    assert [it["path"] for it in plan["removed"]] == ["c|p|/host/b/gone.json"]


def test_a_prefix_is_a_folder_boundary_not_a_string_prefix():
    m = _manifest("c|p|/host/ab/x.json")
    plan = sync.compute_plan(m, [])
    assert sync.spare_unreadable(plan, ["c|p|/host/a"]) == []
    assert len(plan["removed"]) == 1


def test_nothing_unreadable_changes_nothing():
    m = _manifest("c|p|/host/a/x.json")
    plan = sync.compute_plan(m, [])
    assert sync.spare_unreadable(plan, []) == [] and len(plan["removed"]) == 1


# ── a hibernated ingestion waits out the backoff, whatever its schedule says ─────────────────────
def test_hibernation_blocks_until_the_backoff_has_passed():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    at = lambda s: {"hibernated": True, "hibernated_at": (now - timedelta(seconds=s)).isoformat()}
    assert not sync.hibernation_allows(at(60), now, 120)       # the next minute's cron tick waits
    assert sync.hibernation_allows(at(120), now, 120)
    assert sync.hibernation_allows(at(3600), now, 120)


def test_hibernation_without_a_readable_time_does_not_block_forever():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    assert sync.hibernation_allows({"hibernated": True}, now, 120)
    assert sync.hibernation_allows({"hibernated": True, "hibernated_at": "not a date"}, now, 120)
    assert not sync.hibernation_allows({"hibernated": True, "hibernated_at": "2026-09-28T11:59:30"}, now, 120)
