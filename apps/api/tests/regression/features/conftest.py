"""Fixtures every feature suite shares: an admin client for throwaway tenant A (and B for isolation)."""
import pytest

from tests.regression.framework.api import Client, principal_headers


@pytest.fixture
def admin(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["A"]), rec)
    yield c
    c.close()


@pytest.fixture
def other_tenant(live_api, tenants, rec):
    c = Client(live_api, principal_headers("admin", tenants["B"]), rec)
    yield c
    c.close()


def items(resp, *keys):
    """The list in a list response, whatever envelope key this resource uses."""
    body = resp.json()
    if isinstance(body, list):
        return body
    for k in keys + ("items", "connectors", "collections", "templates", "users", "models"):
        if isinstance(body.get(k), list):
            return body[k]
    return []
