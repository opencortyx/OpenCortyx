"""Resolution after ingest, observed through uploads and the decision ledger (RSLV)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

CASES = {
    "same_across_files": "linked SAME_AS; region CONTRADICTS (kept, review), tier SUPPORTS, owner EXTENDS the "
                         "existing node, notes ignored",
    "same_within_file": "the second record is linked SAME_AS the first",
    "different_identity": "no SAME decision",
    "no_settings": "a type without settings is not resolved",
    "tenant_isolation": "tenant B's identical record never links to tenant A's",
    "rerun_is_noop": "resolving the asset again adds no decisions",
    "multi_format_customers": "the same customers as JSON, XML, YAML and JSON Lines link to one canonical node "
                              "each; differing values are conflicts, never overwritten",
    "new_version_of_file": "a changed copy of a file is found as a new version, and what changed is classified",
    "similar_names_gated": "with the similar scorer a near-identical name is linked, a borderline one waits in "
                           "Conflicts, and deciding 'same thing' links it",
}


def build() -> list[Scenario]:
    return [Scenario("RSLV", "features", "resolution", "ingest → extract → resolve", f"case={case}", "record policy",
                     expect, "e2e", "API + workers up; tenants A and B", {"case": case})
            for case, expect in CASES.items()]
