"""The ontology is per organization, over a shared read-only platform vocabulary (ONTO).

Every case is observed from BOTH tenants: what tenant A does to the ontology must be visible to A
and invisible to B, and a built-in type is never changed for anyone but the organization that
edited its own copy.
"""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

KINDS = ("node", "entity", "edge")
CASES = {
    "register_is_private": "A's new type is listed for A, never for B",
    "edit_builtin_copies": "A's description edit applies to A only; B still sees the built-in text",
    "disable_builtin_copies": "A switching a built-in type off leaves it active for B",
    "delete_copy_reverts": "deleting A's edited copy brings back the built-in for A",
    "builtin_undeletable": "a built-in type cannot be deleted; it stays listed for both",
    "cross_tenant_write_refused": "B cannot promote, edit, delete or toggle A's own type",
}


def build() -> list[Scenario]:
    out = []
    for case, expect in CASES.items():
        for kind in KINDS:
            out.append(Scenario("ONTO", "features", "ontology", "per-organization ontology", f"case={case}",
                                f"kind={kind}", expect, "security", "API up; tenants A and B",
                                {"case": case, "kind": kind}))
    for case, expect in {
        "edge_widening_is_private": "linking two node types through a built-in edge widens A's copy, not B's",
        "export_has_own_types_only": "A's export carries A's own types and none of B's",
        "candidates_are_private": "a type A's mapping publishes is not in B's ontology or candidates",
    }.items():
        out.append(Scenario("ONTO", "features", "ontology", "per-organization ontology", f"case={case}",
                            "default", expect, "security", "API up; tenants A and B", {"case": case}))
    return out
