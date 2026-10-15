"""Shared fixtures for the live API test suite.

These tests exercise the REAL running stack (api + worker + Postgres + vLLM), not mocks — that's the
point: prove the product actually works end-to-end, the gap that static `py_compile`/CI never covered.

Run them inside the api container so they can reach the API:

    docker compose exec -e OPENCORTYX_API_KEY=ax_... api python -m pytest tests -m integration

If the stack isn't reachable, every test SKIPS (never fails) so CI stays green on static-only runs.
"""
import os
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("OPENCORTYX_API_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.environ.get("OPENCORTYX_API_KEY", "").strip()


_STACK_PROBE_TIMEOUT = 15.0


def _stack_up() -> bool:
    try:
        httpx.get(f"{BASE_URL}/health", timeout=_STACK_PROBE_TIMEOUT).raise_for_status()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def _require_stack():
    """Skip a test that genuinely needs a running server.

    Deliberately NOT autouse. It used to be, which meant pure unit tests — format routing, decoders,
    row-limit parsing, none of which open a socket — were skipped whenever the stack was down or
    merely busy. Tests that need the server ask for it by depending on `client`; everything else
    runs offline, always.
    """
    if not _stack_up():
        pytest.skip(f"live stack not reachable at {BASE_URL}")


@pytest.fixture
def client(_require_stack):
    with httpx.Client(base_url=f"{BASE_URL}/api/v2", timeout=120.0) as c:
        yield c


@pytest.fixture(scope="session")
def auth_headers(_require_stack):
    """An API key when one is given, otherwise a token for the admin account."""
    if API_KEY:
        return {"X-API-Key": API_KEY}
    email = os.environ.get("OPENCORTYX_ADMIN_EMAIL", "").strip()
    password = os.environ.get("OPENCORTYX_ADMIN_PASSWORD", "")
    if not (email and password):
        pytest.skip("set OPENCORTYX_API_KEY, or OPENCORTYX_ADMIN_EMAIL and OPENCORTYX_ADMIN_PASSWORD")
    res = httpx.post(f"{BASE_URL}/api/v2/auth/token", json={"username": email, "password": password},
                     timeout=30.0)
    res.raise_for_status()
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def mint_token(workspace_id: uuid.UUID, role: str = "admin") -> str:
    """Mint a JWT for an arbitrary org using the app's own signer (works in-container).
    Skips the test if the auth module / secret isn't importable (e.g. running outside the container)."""
    try:
        from opencortyx.core.security.auth import create_access_token
    except Exception as e:
        pytest.skip(f"cannot import app auth to mint tenant token ({e}); run inside the api container")
    return create_access_token(uuid.uuid4(), workspace_id, role=role)


@pytest.fixture
def tenant_headers():
    """Bearer headers for a brand-new, empty org (fresh tenant) — used for isolation tests.

    The org is removed afterwards. Asking the live stack a question as this tenant writes to it (the
    Assistant capture keeps a Session + Turn per answer), and without the teardown every run left a
    stranded org's nodes and vectors in the shared graph and collection."""
    workspace_id = uuid.uuid4()
    # `client` capture: this tenant's answers are not recorded as Assistant chats. The capture runs
    # after the response, so it could otherwise land after the teardown below and strand the org.
    yield {"Authorization": f"Bearer {mint_token(workspace_id)}", "X-OpenCortyx-Capture": "client"}, workspace_id
    _drop_org(workspace_id)


def _drop_org(workspace_id: uuid.UUID) -> None:
    """Delete everything a throwaway test org wrote — graph, vectors and its relational rows."""
    import asyncio
    try:
        from sqlalchemy import text
        from opencortyx.core.config import V2_SCHEMA
        from opencortyx.core.db import V2Session
        from opencortyx.core.substrate import NodeRepo
    except Exception:                               # noqa: BLE001 — outside the container: nothing to clean
        return

    async def _go():
        await NodeRepo().clear_org(workspace_id)
        async with V2Session() as db:
            tables = (await db.execute(text(
                "SELECT table_name FROM information_schema.columns "
                "WHERE table_schema = :s AND column_name = 'workspace_id'"), {"s": V2_SCHEMA})).scalars().all()
            for t in tables:
                await db.execute(text(f'DELETE FROM {V2_SCHEMA}."{t}" WHERE workspace_id = :o'), {"o": workspace_id})
            await db.commit()
    asyncio.run(_go())
