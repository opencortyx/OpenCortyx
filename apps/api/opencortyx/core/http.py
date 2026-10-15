"""Pooled outbound HTTP, keyed by event loop.

WHY THIS EXISTS. `async with httpx.AsyncClient(...)` builds a fresh connection pool, and throwing
it away at the end of the block throws away the TLS handshake with it. On an admin endpoint called
once that is invisible; on the rerank and embedding paths it is paid per query and per batch, which
is where the latency nobody can account for comes from.

Keyed by the running loop, not process-global: an `httpx.AsyncClient` binds to the loop that created
its transport, and Celery workers run several loops over their lifetime. A client shared across
loops raises `Event loop is closed` on the second one. A `WeakKeyDictionary` means a finished loop's
client is collected with it rather than pinned forever.

    client = get_client(timeout=20.0)
    r = await client.post(url, json=payload)     # do NOT close it — the pool outlives the call

Callers never close what they get back. A one-shot request to an unusual host with its own settings
is still free to build its own client; this is for the paths that repeat.
"""
from __future__ import annotations

import asyncio
import weakref

import httpx

from opencortyx.core.config import settings

_CLIENTS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()

#: Requests slower than this are a queue, not a hang — see `limits()` for the pool behind it.
DEFAULT_TIMEOUT_S = 30.0


def limits() -> httpx.Limits:
    """Pool ceilings, shared so the transport never becomes the limit on in-flight work."""
    return httpx.Limits(max_connections=settings.LLM_MAX_CONNECTIONS,
                        max_keepalive_connections=settings.LLM_MAX_KEEPALIVE)


def get_client(timeout: float | httpx.Timeout | None = None) -> httpx.AsyncClient:
    """The pooled client for this event loop, creating it on first use.

    `timeout` applies only when the client is created; a later call with a different value gets the
    existing client, because a per-request deadline belongs on the request. Pass `timeout=` to
    `client.post(...)` when one call needs longer than the pool's default.
    """
    loop = asyncio.get_running_loop()
    client = _CLIENTS.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=timeout or DEFAULT_TIMEOUT_S, limits=limits())
        _CLIENTS[loop] = client
    return client


async def aclose() -> None:
    """Close this loop's client. For shutdown hooks and tests; ordinary callers never need it."""
    loop = asyncio.get_running_loop()
    client = _CLIENTS.pop(loop, None)
    if client is not None and not client.is_closed:
        await client.aclose()
