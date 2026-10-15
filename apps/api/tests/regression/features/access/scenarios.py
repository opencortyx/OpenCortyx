"""Users, roles, API keys and sign-in (ACCS)."""
from __future__ import annotations

from tests.regression.framework.matrix import Scenario

ROLES = {"admin": "accept", "editor": "accept", "viewer": "accept", "superuser": "reject"}
PASSWORDS = {"len_5": ("12345", "reject"), "len_6": ("123456", "accept"), "len_64": ("p" * 64, "accept"),
             "unicode": ("pässwörd-密码", "accept"), "spaces": ("   six   ", "accept")}
EMAILS = {"plain": "accept", "uppercase": "accept", "no_at_sign": "reject", "duplicate": "reject"}
KEY_ROLES = ("admin", "editor", "viewer", "bogus")
AUTH_FLOWS = {
    "login_ok": "200 with access and refresh tokens",
    "login_wrong_password": "401",
    "login_unknown_user": "401",
    "login_deactivated_user": "401",
    "refresh_ok": "new access token that works",
    "refresh_with_access_token": "401",
    "refresh_garbage": "401",
    "me_reflects_role_change": "role in /auth/me follows the latest change after refresh",
    "setup_after_users_exist": "409; no second first-admin",
}


def build() -> list[Scenario]:
    out = []

    def add(wf, inp, cfg, exp, typ, params):
        out.append(Scenario("ACCS", "features", "access", wf, inp, cfg, exp, typ, "API up; tenant A", params))

    for role, r_exp in ROLES.items():
        for pname, (pw, p_exp) in PASSWORDS.items():
            for ename, e_exp in EMAILS.items():
                ok = "accept" if (r_exp, p_exp, e_exp) == ("accept",) * 3 else "reject"
                add("create user → sign in → change role → deactivate → delete", f"email={ename}",
                    f"role={role} password={pname}", "full lifecycle holds" if ok == "accept" else "refused; no user created",
                    "state-transition" if ok == "accept" else "negative",
                    {"role": role, "password": pw, "email": ename, "expect": ok})
    for kr in KEY_ROLES:
        for use in ("read", "write", "admin_op", "after_revoke"):
            add("API key", f"key role={kr}", f"use={use}", "permissions follow the key's role; revoked key refused",
                "security", {"role": kr, "use": use})
    for flow, exp in AUTH_FLOWS.items():
        add("sign-in flow", flow, "default", exp, "security", {"flow": flow})
    return out
