# API tests

Unit tests need no running services:

    bin/opencortyx test                     # from the repository root, in a container

Integration tests (`@pytest.mark.integration`) run against a live stack and a workspace API key
created in the dashboard under Users & Access:

    docker compose exec -e OPENCORTYX_API_KEY=ax_... api python -m pytest -m integration

Tests that need the stack skip themselves when it is unreachable.
