"""The ontology is per organization over a shared, read-only platform vocabulary (ONTO).

Each scenario acts as tenant A and reads the result as BOTH tenants: a change must show for A and
never for B, and a built-in type is never changed for anyone but the organization that copied it.
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.regression.features.ontology.scenarios import KINDS
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


def _types(c) -> dict:
    """{(name, kind): type} as this tenant sees its ontology."""
    return {(t["name"], t["kind"]): t for t in c.get(f"{V2}/ontology").json().get("types", [])}


def _builtin(c, kind: str, why: str = "", **want) -> dict:
    """A built-in type of this kind, optionally one whose fields match `want` (a callable per field)."""
    for t in _types(c).values():
        if t["kind"] == kind and t.get("built_in") and all(f(t.get(k)) for k, f in want.items()):
            return t
    pytest.skip(f"BLOCKED: no built-in {kind} type {why or 'to exercise'}")


def _name(prefix="Onto") -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("scenario", scenarios("ONTO"), ids=str)
def test_ontology_is_per_organization(scenario, rec, admin, other_tenant):
    a, b, p = admin, other_tenant, scenario.params
    case, kind = p["case"], p.get("kind", "node")
    assert kind in KINDS

    if case == "register_is_private":
        name = _name()
        r = a.post(f"{V2}/ontology/types", json={"name": name, "kind": kind, "description": "A only"})
        rec.verify("registered for A", r.status_code == 200, r.text[:160])
        rec.verify("listed for A", (name, kind) in _types(a))
        rec.verify("not listed for B", (name, kind) not in _types(b))

    elif case == "edit_builtin_copies":
        t = _builtin(a, kind)
        text = f"edited by A {uuid.uuid4().hex[:6]}"
        r = a.patch(f"{V2}/ontology/types", params={"name": t["name"], "kind": kind, "description": text})
        rec.verify("edit accepted", r.status_code == 200 and r.json().get("status") == "updated", r.text[:160])
        mine, theirs = _types(a)[(t["name"], kind)], _types(b)[(t["name"], kind)]
        rec.result({"a": mine["description"][:60], "b": theirs["description"][:60]})
        rec.verify("A sees its edit", mine["description"] == text, mine["description"][:80])
        rec.verify("A's copy is its own", mine.get("built_in") is False, mine.get("built_in"))
        rec.verify("B still sees the built-in text", theirs["description"] == t["description"], theirs["description"][:80])
        rec.verify("B's is still the built-in", theirs.get("built_in") is True)

    elif case == "disable_builtin_copies":
        t = _builtin(a, kind, status=lambda s: s == "active")
        r = a.put(f"{V2}/types/{t['name']}/status", json={"kind": kind, "status": "disabled"})
        rec.verify("toggle accepted", r.status_code == 200 and r.json().get("status") == "ok", r.text[:160])
        rec.verify("off for A", _types(a)[(t["name"], kind)]["status"] == "disabled")
        rec.verify("still active for B", _types(b)[(t["name"], kind)]["status"] == "active")

    elif case == "delete_copy_reverts":
        t = _builtin(a, kind)
        a.patch(f"{V2}/ontology/types", params={"name": t["name"], "kind": kind, "description": "temporary"})
        r = a.delete(f"{V2}/ontology/types", params={"name": t["name"], "kind": kind})
        rec.verify("A's copy deleted", r.status_code == 200 and r.json().get("status") == "deleted", r.text[:160])
        back = _types(a).get((t["name"], kind)) or {}
        rec.verify("the built-in is back for A", back.get("built_in") is True and back.get("description") == t["description"],
                   {k: back.get(k) for k in ("built_in", "description")})

    elif case == "builtin_undeletable":
        t = _builtin(a, kind)          # one A has not copied: deleting A's own copy is allowed (it reverts)
        r = a.delete(f"{V2}/ontology/types", params={"name": t["name"], "kind": kind})
        rec.verify("refused", r.json().get("status") == "not_found_or_system", r.text[:160])
        rec.verify("still listed for A", (t["name"], kind) in _types(a))
        rec.verify("still listed for B", (t["name"], kind) in _types(b))

    elif case == "cross_tenant_write_refused":
        name = _name()
        a.post(f"{V2}/ontology/types", json={"name": name, "kind": kind, "description": "A's own"})
        attempts = {
            "promote": b.post(f"{V2}/ontology/promote", params={"name": name, "kind": kind}),
            "edit": b.patch(f"{V2}/ontology/types", params={"name": name, "kind": kind, "description": "B was here"}),
            "delete": b.delete(f"{V2}/ontology/types", params={"name": name, "kind": kind}),
            "status": b.put(f"{V2}/types/{name}/status", json={"kind": kind, "status": "disabled"}),
        }
        rec.result({k: (r.status_code, r.json().get("status")) for k, r in attempts.items()})
        for action, r in attempts.items():
            rec.verify(f"B's {action} finds nothing", r.json().get("status") in ("not_found", "not_found_or_system"),
                       r.text[:120])
        mine = _types(a).get((name, kind)) or {}
        rec.verify("A's type unchanged", mine.get("description") == "A's own" and mine.get("status") == "active",
                   {k: mine.get(k) for k in ("description", "status")})

    elif case == "edge_widening_is_private":
        t = _builtin(a, "edge", "declares endpoint types, so there is nothing to widen",
                     allowed_source_types=bool, allowed_target_types=bool)
        st, tt = _name("Src"), _name("Dst")
        s = a.post(f"{V2}/nodes", json={"type": st, "label": st}).json()["id"]
        d = a.post(f"{V2}/nodes", json={"type": tt, "label": tt}).json()["id"]
        r = a.post(f"{V2}/edges", json={"source_id": s, "target_id": d, "type": t["name"]})
        rec.verify("edge created", r.status_code == 200, r.text[:160])
        mine, theirs = _types(a)[(t["name"], "edge")], _types(b)[(t["name"], "edge")]
        rec.verify("widened for A", st in (mine.get("allowed_source_types") or [])
                   and tt in (mine.get("allowed_target_types") or []), mine.get("allowed_source_types"))
        rec.verify("unchanged for B", st not in (theirs.get("allowed_source_types") or [])
                   and tt not in (theirs.get("allowed_target_types") or []), theirs.get("allowed_source_types"))

    elif case == "export_has_own_types_only":
        mine, theirs = _name("Mine"), _name("Theirs")
        a.post(f"{V2}/ontology/types", json={"name": mine, "kind": "node", "description": "A's"})
        b.post(f"{V2}/ontology/types", json={"name": theirs, "kind": "node", "description": "B's"})
        r = a.get(f"{V2}/export")
        rec.verify("exported", r.status_code == 200, r.status_code)
        names = {t["name"] for t in json.loads(r.text).get("type_registry", [])}
        rec.result({"types_exported": len(names)})
        rec.verify("A's own type exported", mine in names)
        rec.verify("B's type not exported", theirs not in names)

    elif case == "candidates_are_private":
        name = _name("Cand")
        r = a.post(f"{V2}/extraction-templates", json={
            "name": _name("tmpl"), "mechanism": {"ref": "seed.literal", "items": []},
            "emit": {"nodes": [{"type": name}]}})
        rec.verify("template saved", r.status_code == 200, r.text[:160])
        cand = lambda c: {x["name"] for x in c.get(f"{V2}/ontology/candidates").json().get("candidates", [])}
        rec.verify("a candidate for A", name in cand(a))
        rec.verify("not a candidate for B", name not in cand(b))
        rec.verify("not in B's ontology", all(n != name for n, _k in _types(b)))
