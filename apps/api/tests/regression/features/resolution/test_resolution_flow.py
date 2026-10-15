"""Resolution after ingest (RSLV): upload records, let the asset complete, read the decision ledger.

Each scenario uses its own node type, so scenarios in one tenant never see each other's records.
"""
from __future__ import annotations

import json
import random
import time

import pytest

from tests.regression.framework.api import Client, principal_headers, wait_run
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


def _type_name() -> str:
    # letters only, ending in a consonant other than "s": the reader keeps the name as it is
    return "Cust" + "".join(random.choice("bcdfghjklmnpqrtvwxz") for _ in range(7))


def _set_spec(c, type_name, **extra):
    r = c.put(f"{V2}/resolution/types/{type_name}",
              json={"identify_by": ["customer_no"], "ignore": ["notes"], **extra})
    assert r.status_code == 200, r.text


def _as(fmt: str, records: list) -> bytes:
    """The same records written as JSON, JSON Lines, YAML or XML."""
    if fmt == "json":
        return json.dumps(records).encode()
    if fmt == "jsonl":
        return "\n".join(json.dumps(r) for r in records).encode()
    if fmt == "yaml":
        return "".join("- " + "\n  ".join(f"{k}: {json.dumps(v)}" for k, v in r.items()) + "\n"
                       for r in records).encode()
    rows = "".join("<customer>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + "</customer>"
                   for r in records)
    return f"<?xml version='1.0'?><customers>{rows}</customers>".encode()


def _ingest(c, type_name, records, fmt: str = "json", name: str | None = None) -> str:
    """Upload records as `type_name` nodes; wait until the asset is resolved; return its source id."""
    body = _as(fmt, records)
    name = name or f"{type_name}-{time.time_ns()}.{fmt}"
    r = c.post(f"{V2}/json/ingest", files={"file": (name, body, "application/octet-stream")},
               data={"options": json.dumps({"root_type": type_name})})
    assert r.status_code == 200, r.text
    run = wait_run(c, r.json()["run_id"], 240)
    assert run.get("status") == "completed", run
    source = run.get("source_ref") or (run.get("result") or {}).get("source_id")
    deadline = time.time() + 240
    while time.time() < deadline:                      # resolution runs after the embedding pass
        node = (c.get(f"{V2}/nodes/{source}", params={"hops": 0}).json() or {}).get("node") or {}
        if (node.get("properties") or {}).get("__resolved_at"):
            return source
        time.sleep(2)
    raise AssertionError(f"asset {source} was not resolved within 240 s")


def _decisions(c, source) -> list:
    return [d for d in c.get(f"{V2}/resolution/decisions", params={"limit": 1000}).json()["items"]
            if d["source_id"] == source]


@pytest.fixture
def a(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec, timeout=120)
    yield c
    c.close()


@pytest.fixture
def b(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["B"]), rec, timeout=120)
    yield c
    c.close()


@pytest.mark.parametrize("scenario", scenarios("RSLV"), ids=str)
def test_resolution_after_ingest(scenario, rec, a, b):
    case, t = scenario.params["case"], _type_name()
    rec.generated(node_type=t)
    if case != "no_settings":
        _set_spec(a, t)

    if case == "same_across_files":
        _ingest(a, t, [{"customer_no": "C-1", "region": "eu-west-1", "tier": "gold"}])
        src = _ingest(a, t, [{"customer_no": "c-1 ", "region": "ap-south-1", "tier": "GOLD",
                              "owner": "Ana", "notes": "call back"}])
        got = _decisions(a, src)
        rel = {d["attribute"]: d["relation"] for d in got}
        rec.result({"decisions": rel})
        rec.verify("linked as the same customer", rel.get(None) == "SAME", rel)
        rec.verify("a different region is a conflict, kept for review",
                   rel.get("region") == "CONTRADICTS" and any(d["needs_review"] for d in got if d["attribute"] == "region"), rel)
        rec.verify("the same tier (any case) supports it", rel.get("tier") == "SUPPORTS", rel)
        rec.verify("a new owner extends it", rel.get("owner") == "EXTENDS", rel)
        rec.verify("ignored attributes are not compared", "notes" not in rel, rel)
        same = next(d for d in got if d["relation"] == "SAME")
        canonical = a.get(f"{V2}/nodes/{same['target']}", params={"hops": 1}).json()
        rec.verify("the existing customer gained the owner",
                   (canonical["node"]["properties"] or {}).get("owner") == "Ana", canonical["node"]["properties"])
        rec.verify("its region was not overwritten",
                   (canonical["node"]["properties"] or {}).get("region") == "eu-west-1", canonical["node"]["properties"])
        edges = canonical.get("graph", {}).get("edges", [])
        rec.verify("a SAME_AS edge joins them",
                   any(e.get("label") == "SAME_AS" and e.get("source") == same["subject"] for e in edges),
                   sorted({str(e.get("label")) for e in edges}))

    elif case == "same_within_file":
        src = _ingest(a, t, [{"customer_no": "C-7", "tier": "gold"}, {"customer_no": "C-7", "tier": "gold"}])
        rel = [d["relation"] for d in _decisions(a, src)]
        rec.result({"decisions": rel})
        rec.verify("the duplicate is linked", rel.count("SAME") == 1, rel)

    elif case == "different_identity":
        _ingest(a, t, [{"customer_no": "C-1"}])
        src = _ingest(a, t, [{"customer_no": "C-2"}])
        rel = [d["relation"] for d in _decisions(a, src)]
        rec.verify("no SAME decision", "SAME" not in rel, rel)

    elif case == "no_settings":
        _ingest(a, t, [{"customer_no": "C-1"}])
        src = _ingest(a, t, [{"customer_no": "C-1"}])
        rel = [d["relation"] for d in _decisions(a, src)]
        rec.verify("not resolved without settings", rel == [], rel)

    elif case == "tenant_isolation":
        _ingest(a, t, [{"customer_no": "C-1", "tier": "gold"}])
        _set_spec(b, t)
        src = _ingest(b, t, [{"customer_no": "C-1", "tier": "gold"}])
        rel = [d["relation"] for d in _decisions(b, src)]
        rec.verify("tenant B's record never links to tenant A's", "SAME" not in rel, rel)

    elif case == "multi_format_customers":
        base = [{"customer_no": "C-1", "region": "eu-west-1", "tier": "gold"},
                {"customer_no": "C-2", "region": "us-east-1", "tier": "silver"},
                {"customer_no": "C-3", "region": "ap-south-1", "tier": "bronze"}]
        _ingest(a, t, base, "json")
        later = {}
        for fmt, change in (("xml", {"C-1": {"tier": "platinum"}}), ("yaml", {"C-2": {"owner": "Ana"}}),
                            ("jsonl", {"C-3": {"region": "ap-south-2"}})):
            recs = [{**r, **change.get(r["customer_no"], {})} for r in base]
            later[fmt] = _decisions(a, _ingest(a, t, recs, fmt))
        rec.result({fmt: sorted((d["relation"], d["attribute"]) for d in ds) for fmt, ds in later.items()})
        for fmt, ds in later.items():
            same = [d for d in ds if d["relation"] == "SAME"]
            rec.verify(f"{fmt}: all three customers linked", len(same) == 3, len(same))
            rec.verify(f"{fmt}: every link goes to the first file's customer",
                       len({d["target"] for d in same}) == 3, [d["target_label"] for d in same])
        conflicts = {(d["attribute"], str(d["evidence"].get("value"))) for d in later["xml"] + later["jsonl"]
                     if d["relation"] == "CONTRADICTS"}
        rec.verify("a changed tier and region are conflicts",
                   {("tier", "platinum"), ("region", "ap-south-2")} <= conflicts, sorted(conflicts))
        rec.verify("a new owner extends the customer",
                   any(d["relation"] == "EXTENDS" and d["attribute"] == "owner" for d in later["yaml"]))
        c1 = next(d["target"] for d in later["xml"] if d["relation"] == "SAME"
                  and (d["evidence"].get("identity") or {}).get("customer_no") == "C-1")
        props = a.get(f"{V2}/nodes/{c1}", params={"hops": 0}).json()["node"]["properties"]
        rec.verify("the canonical customer kept its tier (no overwrite)", props.get("tier") == "gold", props.get("tier"))

    elif case == "new_version_of_file":
        v1 = [{"customer_no": f"C-{i}", "region": "eu-west-1", "tier": "gold", "note": "quarterly export"}
              for i in range(1, 21)]
        _ingest(a, t, v1, "json", name=f"{t}-customers-2026-q2.json")
        v2 = [dict(r, tier=("platinum" if r["customer_no"] == "C-4" else r["tier"])) for r in v1]
        src2 = _ingest(a, t, v2, "json", name=f"{t}-customers-2026-q3.json")
        ds = _decisions(a, src2)
        rec.result({"same": sum(d["relation"] == "SAME" for d in ds),
                    "conflicts": [(d["attribute"], d["evidence"].get("value")) for d in ds if d["relation"] == "CONTRADICTS"]})
        rec.verify("every record of the new version is linked to the old one",
                   sum(d["relation"] == "SAME" for d in ds) == 20, sum(d["relation"] == "SAME" for d in ds))
        rec.verify("only the changed value is a conflict",
                   [(d["attribute"], d["evidence"].get("value")) for d in ds if d["relation"] == "CONTRADICTS"]
                   == [("tier", "platinum")])
        node = a.get(f"{V2}/nodes/{src2}", params={"hops": 1}).json()
        caps = (a.get(f"{V2}/resolution/proposals").json().get("proposals") or [])
        superseded = any(e.get("label") == "SUPERSEDES" for e in node.get("graph", {}).get("edges", []))
        proposed = any(p.get("kind") == "version" and (p.get("winner") or {}).get("id") == src2 for p in caps)
        rec.verify("the new file is found as a version of the old one (applied or awaiting review)",
                   superseded or proposed, {"superseded": superseded, "proposed": proposed})

    elif case == "similar_names_gated":
        spec = {"identify_by": ["company"], "scorer": "similar", "auto": 0.95, "review": 0.80}
        r = a.put(f"{V2}/resolution/types/{t}", json=spec)
        rec.verify("similar scorer and gate accepted", r.status_code == 200, r.text[:160])
        _ingest(a, t, [{"company": "Acme Corporation", "city": "Oslo"}])
        close = _decisions(a, _ingest(a, t, [{"company": "Acme Corporation.", "city": "Oslo"}]))
        near = _decisions(a, _ingest(a, t, [{"company": "Acme Corporatn", "city": "Oslo"}]))
        far = _decisions(a, _ingest(a, t, [{"company": "Acmeworks Foundry Ltd", "city": "Oslo"}]))
        rec.result({"close": [(d["relation"], d["confidence"], d["needs_review"]) for d in close if d["relation"] == "SAME"],
                    "near": [(d["relation"], d["confidence"], d["needs_review"]) for d in near if d["relation"] == "SAME"],
                    "far": [d["relation"] for d in far]})
        rec.verify("a near-identical name is linked automatically",
                   any(d["relation"] == "SAME" and not d["needs_review"] and d["confidence"] >= 0.95 for d in close))
        pending = [d for d in near if d["relation"] == "SAME" and d["needs_review"]]
        rec.verify("a borderline name waits for a person", len(pending) == 1 and 0.80 <= pending[0]["confidence"] < 0.95,
                   [(d["relation"], d["confidence"]) for d in near])
        rec.verify("a different name is not a match", not any(d["relation"] == "SAME" for d in far))
        if pending:
            r = a.post(f"{V2}/resolution/decisions/{pending[0]['id']}/decide", json={"choice": "use_new"})
            rec.verify("deciding 'same thing' is accepted", r.status_code == 200, r.text[:160])
            edges = a.get(f"{V2}/nodes/{pending[0]['subject']}", params={"hops": 1}).json().get("graph", {}).get("edges", [])
            rec.verify("the two are now linked SAME_AS",
                       any(e.get("label") == "SAME_AS" and e.get("target") == pending[0]["target"] for e in edges))
            left = [d for d in a.get(f"{V2}/resolution/decisions", params={"needs_review": "true", "limit": 1000}).json()["items"]
                    if d["id"] == pending[0]["id"]]
            rec.verify("it left the review queue", left == [])

    elif case == "rerun_is_noop":
        _ingest(a, t, [{"customer_no": "C-1", "tier": "gold"}])
        src = _ingest(a, t, [{"customer_no": "C-1", "tier": "gold"}])
        before = len(_decisions(a, src))
        r = a.post(f"{V2}/resolution/assets/{src}")
        rec.verify("re-resolve queued", r.status_code == 200, r.text[:160])
        time.sleep(20)
        after = len(_decisions(a, src))
        rec.verify("no new decisions", before == after and before > 0, {"before": before, "after": after})
