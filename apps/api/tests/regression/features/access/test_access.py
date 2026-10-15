"""Users, roles, API keys and sign-in, verified by actually signing in with what was created (ACCS)."""
from __future__ import annotations

import uuid

import pytest

from tests.regression.framework.api import Client, principal_headers
from tests.regression.framework.matrix import scenarios

pytestmark = pytest.mark.regression
V2 = "/api/v2"


@pytest.fixture
def admin(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


@pytest.fixture
def anon(live_api, rec):
    c = Client(live_api, {}, rec)
    yield c
    c.close()


def _email(kind: str) -> str:
    base = f"reg-{uuid.uuid4().hex[:10]}@example.test"
    return {"uppercase": base.upper(), "no_at_sign": base.replace("@", "-")}.get(kind, base)


def _login(anon, email, pw):
    return anon.post(f"{V2}/auth/token", json={"username": email, "password": pw})


@pytest.mark.parametrize("scenario", scenarios("ACCS"), ids=str)
def test_access(scenario, rec, admin, anon, live_api):
    wf, p = scenario.workflow, scenario.params
    if wf.startswith("create user"):
        _user_lifecycle(p, rec, admin, anon)
    elif wf == "API key":
        _api_key(p, rec, admin, live_api)
    else:
        _flow(p, rec, admin, anon, live_api)


def _user_lifecycle(p, rec, admin, anon):
    email = _email(p["email"])
    if p["email"] == "duplicate":
        first = admin.post(f"{V2}/users", json={"email": email, "password": "123456", "role": "viewer"})
        rec.verify("first account created", first.status_code == 200, first.text[:160])
    rec.generated(email=email, role=p["role"], password_len=len(p["password"]))
    r = admin.post(f"{V2}/users", json={"email": email, "password": p["password"], "role": p["role"]})
    rec.result({"status": r.status_code, "body": r.text[:200]})
    rec.verify("no server error", r.status_code < 500, r.text[:200])
    users = {u["email"].lower(): u for u in admin.get(f"{V2}/users").json().get("items", admin.get(f"{V2}/users").json().get("users", []))}
    if p["expect"] == "reject":
        rec.verify("refused", r.status_code in (400, 409, 422), r.status_code)
        if p["email"] != "duplicate":
            rec.verify("no user created", email.lower() not in users)
        return
    rec.verify("created", r.status_code == 200, r.text[:200])
    uid = r.json()["id"]
    rec.verify("listed with its role", users.get(email.lower(), {}).get("role") == p["role"], users.get(email.lower()))
    t = _login(anon, email, p["password"])
    rec.verify("can sign in with the password", t.status_code == 200, t.text[:160])
    tok = t.json().get("access_token")
    me = Client(anon.http.base_url, {"Authorization": f"Bearer {tok}"}).get(f"{V2}/auth/me").json()
    rec.verify("signed-in role is the assigned role", me.get("role") == p["role"], me)
    new_role = "viewer" if p["role"] != "viewer" else "editor"
    everyone = admin.get(f"{V2}/users").json().get("items", [])
    admins = [u for u in everyone if u.get("role") == "admin" and u.get("status", "active") == "active"]
    r = admin.patch(f"{V2}/users/{uid}", json={"role": new_role})
    if p["role"] == "admin" and len(admins) == 1:
        # the tenant's only admin: demoting it would lock everyone out, so it must be refused
        rec.verify("last admin cannot be demoted", r.status_code == 400 and "last admin" in r.text, r.text[:120])
        rec.verify("deleted", admin.delete(f"{V2}/users/{uid}").status_code in (200, 400))
        return
    rec.verify("role change accepted", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    t2 = _login(anon, email, p["password"])
    me2 = Client(anon.http.base_url, {"Authorization": f"Bearer {t2.json().get('access_token')}"}).get(f"{V2}/auth/me").json()
    rec.verify("new sign-in carries the new role", me2.get("role") == new_role, me2)
    rec.verify("deactivation accepted", admin.patch(f"{V2}/users/{uid}", json={"status": "disabled"}).status_code == 200)
    rec.verify("deactivated user cannot sign in", _login(anon, email, p["password"]).status_code in (401, 403))
    rec.verify("deleted", admin.delete(f"{V2}/users/{uid}").status_code == 200)
    rec.verify("deleted user cannot sign in", _login(anon, email, p["password"]).status_code in (401, 403))


def _api_key(p, rec, admin, live_api):
    r = admin.post(f"{V2}/apikeys", json={"app": "regression", "role": p["role"]})
    rec.verify("key created", r.status_code == 200, r.text[:160])
    body = r.json()
    raw, kid, eff = body["key"], body["id"], body["role"]
    rec.result({"requested_role": p["role"], "effective_role": eff})
    if p["role"] == "bogus":
        rec.verify("unknown role falls back to editor, never to admin", eff == "editor", eff)
    listed = admin.get(f"{V2}/apikeys").json().get("items", [])
    rec.verify("raw key never listed", all(raw not in str(k) for k in listed))
    k = Client(live_api, {"X-API-Key": raw}, rec)
    try:
        if p["use"] == "after_revoke":
            rec.verify("revoked", admin.delete(f"{V2}/apikeys/{kid}").status_code == 200)
            rec.verify("revoked key refused", k.get(f"{V2}/auth/me").status_code == 401)
            return
        if p["use"] == "read":
            rec.verify("key can read", k.get(f"{V2}/collections").status_code == 200)
        elif p["use"] == "write":
            r = k.post(f"{V2}/collections", json={"name": f"key-{uuid.uuid4().hex[:6]}"})
            want = 403 if eff == "viewer" else 200
            rec.verify("write follows the key role", r.status_code == want, f"{r.status_code} for role {eff}")
        else:
            r = k.get(f"{V2}/users")
            want = 200 if eff == "admin" else 403
            rec.verify("admin operation follows the key role", r.status_code == want, f"{r.status_code} for role {eff}")
    finally:
        k.close()


def _flow(p, rec, admin, anon, live_api):
    email, pw = _email("plain"), "flow-pass-123"
    admin.post(f"{V2}/users", json={"email": email, "password": pw, "role": "editor"})
    f = p["flow"]
    if f == "login_ok":
        r = _login(anon, email, pw)
        rec.verify("200", r.status_code == 200, r.status_code)
        rec.verify("both tokens issued", {"access_token", "refresh_token"} <= set(r.json()), list(r.json()))
    elif f == "login_wrong_password":
        rec.verify("401", _login(anon, email, pw + "x").status_code == 401)
    elif f == "login_unknown_user":
        rec.verify("401", _login(anon, "nobody-" + email, pw).status_code == 401)
    elif f == "login_deactivated_user":
        uid = next(u["id"] for u in admin.get(f"{V2}/users").json().get("items", []) if u["email"] == email)
        admin.patch(f"{V2}/users/{uid}", json={"status": "disabled"})
        rec.verify("401", _login(anon, email, pw).status_code in (401, 403))
    elif f in ("refresh_ok", "refresh_with_access_token", "me_reflects_role_change"):
        t = _login(anon, email, pw).json()
        if f == "refresh_with_access_token":
            rec.verify("401", anon.post(f"{V2}/auth/refresh", json={"refresh_token": t["access_token"]}).status_code == 401)
            return
        if f == "me_reflects_role_change":
            uid = next(u["id"] for u in admin.get(f"{V2}/users").json().get("items", []) if u["email"] == email)
            admin.patch(f"{V2}/users/{uid}", json={"role": "viewer"})
        r = anon.post(f"{V2}/auth/refresh", json={"refresh_token": t["refresh_token"]})
        rec.verify("refresh accepted", r.status_code == 200, r.text[:160])
        me = Client(live_api, {"Authorization": f"Bearer {r.json()['access_token']}"}).get(f"{V2}/auth/me")
        rec.verify("new token works", me.status_code == 200)
        if f == "me_reflects_role_change":
            rec.verify("refreshed token carries the current role", me.json().get("role") == "viewer", me.json())
    elif f == "refresh_garbage":
        rec.verify("401", anon.post(f"{V2}/auth/refresh", json={"refresh_token": "garbage"}).status_code == 401)
    elif f == "setup_after_users_exist":
        if anon.get(f"{V2}/auth/needs-setup").json().get("needs_setup"):
            rec.block("install has no users: calling setup would create a real first admin")
        r = anon.post(f"{V2}/auth/setup", json={"email": _email("plain"), "password": "setup-pass-1"})
        rec.verify("setup refused once users exist", r.status_code == 409, r.status_code)
