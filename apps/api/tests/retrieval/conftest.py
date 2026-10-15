"""Test configuration for the retrieval workflow.

Two tiers, deliberately separated:

  unit         — pure logic, no I/O. Milliseconds. These must stay green at all times.
  integration  — real database, real embedding service. Seconds. These prove the claims that
                 unit tests cannot, e.g. that a date query actually reaches a turn whose date
                 lives only in a property.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: needs a populated database and an embedding service")


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"
