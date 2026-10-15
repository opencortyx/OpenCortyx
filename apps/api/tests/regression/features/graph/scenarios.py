"""Nodes, edges and relationships through the API, with the resulting graph read back (GRPH)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

NODE_TYPES = {"Person": "accept", "Company": "accept", "Custom_Type_42": "accept", "Ünïcødé": "accept",
              "T" * 120: "accept", "": "reject", "   ": "reject"}
PROPERTIES = {
    "none": None,
    "scalars": {"age": 41, "active": True, "score": 0.25, "city": "Oslo"},
    "nested": {"address": {"city": "Oslo", "zip": "0150"}, "tags": ["a", "b"]},
    "unicode": {"名前": "東京", "note": "Zürich — مرحبا"},
    "wide_60": {f"k{i}": i for i in range(60)},
    "reserved_keys": {"__source": "forged", "__validated": False, "name": "kept"},
}
EDGE_TYPES = ("RELATES_TO", "WORKS_AT", "lower_case_rel", "ÆØÅ_REL")
ENDPOINTS = {
    "valid_pair": "created, visible from both nodes",
    "self_loop": "refused with 422: an edge connects two different nodes",
    "missing_target": "refused with 404; no ghost edge",
    "missing_source": "refused with 404; no ghost edge",
    "other_tenant_target": "refused with 404; tenants never link",
    "malformed_id": "refused with 422, not a server error",
}


def build() -> list[Scenario]:
    out = []

    def add(wf, inp, cfg, exp, typ, params):
        out.append(Scenario("GRPH", "features", "graph", wf, inp, cfg, exp, typ, "API up; tenants A and B", params))

    for t, exp in NODE_TYPES.items():
        for pn, props in PROPERTIES.items():
            add("create node → read back → patch → delete", f"type={t[:24]!r}", f"properties={pn}",
                "persisted exactly; patch merges; delete removes" if exp == "accept" else "refused, nothing stored",
                "integration" if exp == "accept" else "negative", {"type": t, "props": props, "expect": exp, "pname": pn})
    for et in EDGE_TYPES:
        for ep, exp in ENDPOINTS.items():
            add("create edge", f"endpoints={ep}", f"type={et}", exp,
                "integration" if ep in ("valid_pair", "self_loop") else "negative", {"etype": et, "endpoints": ep})
            add("create relationship", f"endpoints={ep}", f"relation={et}", exp,
                "integration" if ep in ("valid_pair", "self_loop") else "negative",
                {"etype": et, "endpoints": ep, "relationship": True})
    for hops in (1, 2, 3):
        add("delete node with edges", "chain a→b→c", f"read hops={hops}", "deleted node and its edges vanish from neighbours",
            "state-transition", {"hops": hops})
    for q in ("exact_label", "prefix", "lowercase", "other_tenant"):
        add("search nodes", f"query={q}", "default", "finds own node; never another tenant's", "integration", {"q": q})
    return out
