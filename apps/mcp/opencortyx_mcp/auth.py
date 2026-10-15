"""Inbound authentication for the MCP server + per-caller identity forwarding.

Why this exists
---------------
Over stdio the client OWNS the server process: it spawns it, nobody else can talk to it, and the
credential comes from the environment. Over HTTP that is no longer true — anything that can reach the
port can call the tools, several of which WRITE (ingest, run_ingestion, remember). So the
HTTP transports are gated here:

  1. `AuthMiddleware` rejects any request without a VALID credential before MCP ever sees it — so
     `initialize` and `tools/list` are protected too, not just `tools/call`.
  2. The caller's own credential is then used for the API calls that back each tool
     (`current_credential()`), instead of one shared workspace key. That is what makes role
     enforcement (viewer = read-only), per-key audit, and revocation actually mean something: a
     viewer's key cannot write, whatever the server's own key could do.

Credentials are whatever the OpenCortyx API already accepts — an `ax_…` workspace API key
(`X-API-Key`) or a JWT (`Authorization: Bearer …`). Validation is delegated to the API itself
(`GET /api/v2/auth/me`), so there is ONE source of truth for identity, roles and revocation; this
server never sees a password and stores no credential material.

Hardening notes
---------------
• Default deny: HTTP transports require auth unless `MCP_ALLOW_ANONYMOUS=true` is set explicitly.
• Results are cached briefly (positive AND negative) so a hot path is not one API round-trip per
  call, while revocation still takes effect within `MCP_AUTH_CACHE_TTL` seconds.
• Repeated failures from one peer are throttled (429) to blunt key guessing.
• Raw credentials are never logged, cached by value, or echoed — only an 8-char SHA-256 fingerprint.
• Failures return an identical 401 whether the credential was missing, malformed, or wrong.
"""
import hashlib
import json
import time
from contextvars import ContextVar

import httpx

from opencortyx_mcp import config

# The credential for the request currently being served (HTTP transports only).
_caller: ContextVar[dict | None] = ContextVar("opencortyx_mcp_caller", default=None)

# fingerprint -> (expires_at, identity|None). identity None = known-bad (negative cache).
_cache: dict[str, tuple[float, dict | None]] = {}
_CACHE_MAX = 512

# peer -> (window_started_at, failure_count)
_failures: dict[str, tuple[float, int]] = {}

_UNAUTHORIZED = {
    "error": "unauthorized",
    "detail": ("This MCP server requires a credential. Send an OpenCortyx workspace API key as "
               "`X-API-Key: ax_…` (issue one in the web UI under Users & Access), or a JWT as "
               "`Authorization: Bearer …`."),
}


def fingerprint(cred: str) -> str:
    """A short, non-reversible label for a credential — safe to log."""
    return hashlib.sha256((cred or "").encode()).hexdigest()[:8]


def _auth_headers(cred: str) -> dict:
    """Present the credential to the API the way that credential type is meant to be presented."""
    return {"X-API-Key": cred} if cred.startswith("ax_") else {"Authorization": f"Bearer {cred}"}


def _cache_get(fp: str):
    hit = _cache.get(fp)
    if not hit:
        return False, None
    expires, identity = hit
    if expires < time.monotonic():
        _cache.pop(fp, None)
        return False, None
    return True, identity


def _cache_put(fp: str, identity: dict | None) -> None:
    if len(_cache) >= _CACHE_MAX:                     # crude bound; entries are tiny and short-lived
        _cache.clear()
    ttl = config.AUTH_CACHE_TTL if identity else config.AUTH_NEGATIVE_CACHE_TTL
    _cache[fp] = (time.monotonic() + ttl, identity)


OK, INVALID, UNAVAILABLE = "ok", "invalid", "unavailable"


async def verify(cred: str) -> tuple[str, dict | None]:
    """Resolve a credential against the API. Returns (status, identity).

    Three outcomes, deliberately distinct: a credential the API REJECTED is not the same thing as a
    credential we could not CHECK. Both are refused — this fails closed — but only the first is the
    caller's fault, so only the first answers 401 and counts toward the guessing throttle.

    The API is the single source of truth: a key revoked there stops working here within the cache
    TTL, with no state to keep in sync."""
    if not cred:
        return INVALID, None
    fp = fingerprint(cred)
    cached, identity = _cache_get(fp)
    if cached:
        return (OK, identity) if identity else (INVALID, None)
    try:
        async with httpx.AsyncClient(timeout=config.AUTH_TIMEOUT) as http:
            r = await http.get(f"{config.API_URL}/api/v2/auth/me", headers=_auth_headers(cred))
        if r.status_code == 200:
            me = r.json()
            identity = {"user_id": me.get("user_id"), "workspace_id": me.get("workspace_id"),
                        "role": me.get("role"), "fingerprint": fp}
            _cache_put(fp, identity)
            return OK, identity
        if r.status_code in (401, 403):
            _cache_put(fp, None)
            return INVALID, None
        # 5xx from the API is its problem, not the credential's — don't cache it either way.
        print(f"opencortyx-mcp: auth check got HTTP {r.status_code} from the API", flush=True)
    except Exception as e:
        # Unreachable API is NOT proof the credential is bad — don't poison the negative cache with
        # an outage, don't grant access, and don't tell the client their key is wrong.
        print(f"opencortyx-mcp: auth check could not reach the API "
              f"({type(e).__name__}: {e or 'timed out'})", flush=True)
    return UNAVAILABLE, None


def _throttled(peer: str) -> bool:
    """True when this peer has failed auth too often recently."""
    started, count = _failures.get(peer, (0.0, 0))
    now = time.monotonic()
    if now - started > config.AUTH_FAIL_WINDOW:
        return False
    return count >= config.AUTH_FAIL_LIMIT


def _record_failure(peer: str) -> None:
    started, count = _failures.get(peer, (0.0, 0))
    now = time.monotonic()
    if now - started > config.AUTH_FAIL_WINDOW:
        _failures[peer] = (now, 1)
    else:
        _failures[peer] = (started, count + 1)
    if len(_failures) > 4096:                          # bound the map; oldest windows are stale anyway
        cutoff = now - config.AUTH_FAIL_WINDOW
        for k, (s, _c) in list(_failures.items()):
            if s < cutoff:
                _failures.pop(k, None)


def current_credential() -> str | None:
    """The credential of the caller being served right now, or None (stdio / no HTTP request).

    Read from two places because a tool coroutine may not run in the same task as the ASGI handler:
    the middleware's contextvar first, then the MCP SDK's own per-request context."""
    caller = _caller.get()
    if caller and caller.get("credential"):
        return caller["credential"]
    try:
        from mcp.server.lowlevel.server import request_ctx
        request = getattr(request_ctx.get(), "request", None)
        headers = getattr(request, "headers", None)
        if headers:
            return (headers.get("x-api-key")
                    or (headers.get("authorization") or "").removeprefix("Bearer ").strip()
                    or None)
    except LookupError:
        pass                                            # not inside a request — stdio, or startup
    except Exception:
        pass
    return None


def current_identity() -> dict | None:
    """Who the current caller is (as resolved at the gate), or None outside an HTTP request."""
    caller = _caller.get()
    return caller.get("identity") if caller else None


def _json_response(status: int, body: dict, extra_headers: list | None = None):
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers += (extra_headers or [])

    async def _send(send):
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})
    return _send


class AuthMiddleware:
    """Pure-ASGI gate in front of the MCP app.

    Pure ASGI (not BaseHTTPMiddleware) so lifespan events pass straight through to the app — the
    streamable-HTTP session manager starts in that lifespan, and wrapping it in a second Starlette
    app would break it.
    """

    def __init__(self, app, require_auth: bool):
        self.app = app
        self.require_auth = require_auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":                     # lifespan / websocket → untouched
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.rstrip("/") in ("/healthz", "/health"):
            # Liveness must not require a credential (container healthchecks, load balancers) and
            # must not disclose anything about the workspace.
            await _json_response(200, {"status": "ok", "auth": "required" if self.require_auth
                                       else "anonymous"})(send)
            return

        if not self.require_auth:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        peer = ((headers.get("x-forwarded-for", "").split(",")[0].strip()
                 if config.TRUST_FORWARDED_FOR else "")
                or (scope.get("client") or ("unknown", 0))[0])

        cred = (headers.get("x-api-key")
                or (headers.get("authorization") or "").removeprefix("Bearer ").strip())
        status, identity = await verify(cred) if cred else (INVALID, None)

        if status == UNAVAILABLE:
            await _json_response(
                503, {"error": "auth_unavailable",
                      "detail": "Could not verify the credential — the OpenCortyx API is not "
                                "responding. This is not a problem with your key; retry shortly."},
                [(b"retry-after", b"5")])(send)
            return

        if status != OK:
            _record_failure(peer)
            if _throttled(peer):
                await _json_response(
                    429, {"error": "too_many_attempts",
                          "detail": "Too many failed authentication attempts. Wait and retry."},
                    [(b"retry-after", str(int(config.AUTH_FAIL_WINDOW)).encode())])(send)
                return
            # Same response for missing / malformed / wrong — nothing to probe.
            await _json_response(401, _UNAUTHORIZED,
                                 [(b"www-authenticate", b'Bearer realm="OpenCortyx MCP"')])(send)
            return

        token = _caller.set({"credential": cred, "identity": identity})
        try:
            await self.app(scope, receive, send)
        finally:
            _caller.reset(token)
