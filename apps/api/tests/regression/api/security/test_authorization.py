"""Every API operation × every kind of caller: who is let in, who is refused, and nobody crashes (AUTH).

The expected outcome comes from the guard contract (`framework.matrix.expected_status`), the operation
list from the live app's route table. Requests carry random ids and an empty body, so an authorised
call finds nothing to act on; platform-wide operations are never sent by an authorised principal.
"""
from __future__ import annotations

import pytest

from tests.regression.framework.api import Client, fill, HEAVY, NEVER_EXECUTE, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module")
def viewer_key(live_api, tenants):
    c = Client(live_api, principal_headers("admin", tenants["A"]))
    r = c.post("/api/v2/apikeys", json={"app": "regression", "role": "viewer"})
    c.close()
    return r.json().get("key") if r.status_code == 200 else None


@pytest.mark.parametrize("scenario", scenarios("AUTH"), ids=str)
def test_authorization(scenario, rec, live_api, tenants, viewer_key):
    p = scenario.params
    op = (p["method"], p["path"])
    authorised = p["expect"] == "allowed"
    role_denied = p["expect"] == "403"
    if op in NEVER_EXECUTE and (authorised or role_denied):
        rec.block(f"not executed: {NEVER_EXECUTE[op]} (only unauthenticated callers are sent)")
    if op in HEAVY and authorised:
        rec.block("not executed: starts a model-heavy background job (covered by its feature suite)")
    if p["principal"] == "agent_readonly" and not viewer_key:
        rec.block("could not create a read-only API key for the tenant")
    headers = principal_headers(p["principal"], tenants["A"], viewer_key)
    path = fill(p["path"])
    kw = {}
    if p["method"] in ("POST", "PUT", "PATCH"):
        kw["json"] = {}
    rec.generated(principal=p["principal"], guard=p["level"], path=path)
    c = Client(live_api, headers, rec)
    try:
        r = c.request(p["method"], path, **kw)
    finally:
        c.close()
    rec.result({"status": r.status_code, "body": r.text[:200]})
    if p["expect"] == "401":
        rec.verify("unauthenticated caller refused with 401", r.status_code == 401, r.status_code)
    elif p["expect"] == "403":
        rec.verify("caller without the role refused with 403", r.status_code == 403, r.status_code)
    else:
        rec.verify("authorised caller let through", r.status_code not in (401, 403), r.status_code)
    rec.verify("no server error", r.status_code < 500, f"{r.status_code}: {r.text[:200]}")
