"""Live-API harness: principals for a throwaway tenant, a recorded HTTP client, and the safety rules.

Every live scenario runs as a tenant that exists only for the run (a random org id, removed at the end),
so the suite never reads or writes an operator's data. Operations whose effect is PLATFORM-WIDE rather
than tenant-scoped are never executed by an authorised principal — see `NEVER_EXECUTE`.
"""
from __future__ import annotations

import time
import uuid
from datetime import timedelta

import httpx

#: Platform-wide or operator-destructive operations. An authorised call here would affect every
#: tenant (the shared Celery queue, global runtime settings), so the suite never makes one.
NEVER_EXECUTE = {
    ("DELETE", "/api/v2/reset"): "purges the shared task queue of every tenant",
    ("POST", "/api/v2/queue/purge"): "purges the shared task queue",
    ("POST", "/api/v2/worker/reset"): "restarts the shared workers",
    ("PUT", "/api/v2/config"): "runtime settings are global, not per tenant",
    ("PUT", "/api/v2/capabilities/{name}"): "capability switches are platform settings",
}
#: Tenant-scoped but start model-heavy background jobs; exercised by their own feature suites only.
HEAVY = {("POST", p) for p in ("/api/v2/corpus/enrich", "/api/v2/extraction/run-all", "/api/v2/eval/run",
                               "/api/v2/eval/seed", "/api/v2/structured/enrich-native",
                               "/api/v2/structured/seed-domain", "/api/v2/graph/tree/rebuild", "/api/v2/stats/compute")}


def principal_headers(principal: str, org: uuid.UUID, api_key: str | None = None) -> dict:
    """Authentication headers for one of the matrix's principals, in tenant `org`."""
    import jwt
    from opencortyx.core.config import settings
    from opencortyx.core.security.auth import create_access_token, create_refresh_token
    h = {"X-OpenCortyx-Capture": "client"}
    if principal == "anonymous":
        return h
    if principal == "garbage_token":
        return {**h, "Authorization": "Bearer not-a-jwt"}
    if principal == "wrong_secret":
        tok = jwt.encode({"sub": str(uuid.uuid4()), "workspace_id": str(org), "role": "admin", "typ": "access"},
                         "an-entirely-different-secret-xxxxxxxxxxxxxxxx", algorithm=settings.JWT_ALGORITHM)
        return {**h, "Authorization": f"Bearer {tok}"}
    if principal == "expired":
        return {**h, "Authorization": f"Bearer {create_access_token(uuid.uuid4(), org, 'admin', minutes=-5)}"}
    if principal == "refresh_as_access":
        return {**h, "Authorization": f"Bearer {create_refresh_token(uuid.uuid4(), org)}"}
    if principal == "agent_readonly":
        return {**h, "X-API-Key": api_key or "missing-key"}
    return {**h, "Authorization": f"Bearer {create_access_token(uuid.uuid4(), org, principal)}"}


class Client:
    """httpx with the base URL, a timeout, and every exchange appended to the scenario's steps."""

    def __init__(self, base: str, headers: dict, rec=None, timeout: float = 60):
        self.http = httpx.Client(base_url=base, headers=headers, timeout=timeout)
        self.rec = rec

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        t0 = time.perf_counter()
        r = self.http.request(method, path, **kw)
        if self.rec is not None:
            body = r.text[:300].replace("\n", " ")
            self.rec.step(f"{method} {path} → {r.status_code} ({(time.perf_counter() - t0) * 1000:.0f} ms) {body}")
        return r

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def patch(self, path, **kw):
        return self.request("PATCH", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def close(self):
        self.http.close()


def wait_run(c: Client, run_id: str, timeout: float = 240) -> dict:
    """Poll a run until it leaves queued/running; returns its last state."""
    deadline, last = time.time() + timeout, {}
    while time.time() < deadline:
        r = c.http.get(f"/api/v2/progress/{run_id}")
        if r.status_code == 200:
            last = r.json()
            if (last.get("status") or "") not in ("queued", "running", "pending", ""):
                return last
        time.sleep(1.0)
    return {**last, "status": last.get("status", "timeout"), "timed_out": True}


def fill(path: str, value: str = None) -> str:
    """A concrete path: every {param} becomes a random id (or `value`) so the handler finds nothing."""
    import re
    return re.sub(r"\{[^}]+\}", lambda _: value or str(uuid.uuid4()), path)


EXPIRED = timedelta(minutes=-5)
