# Contributing to OpenCortyx

Thanks for helping. This guide covers the development setup, checks, and conventions.

## Development setup

Requirements: Docker, Node.js 20 (the version CI and the web image use; `apps/web/.nvmrc`), and
Python 3.11 for running API tools outside containers. Newer npm releases accept dependency
conflicts that npm 10 rejects, so install web dependencies with Node 20.

```bash
git clone https://github.com/opencortyx/OpenCortyx.git
cd OpenCortyx
bin/opencortyx init
bin/opencortyx dev
```

`dev` uses `compose.dev.yaml`: source directories are mounted into the containers, the dashboard
runs `next dev`, and Postgres/Redis are published on localhost. The Celery workers do not reload
automatically; after changing backend code run `bin/opencortyx restart worker`.

## Checks

Run these before opening a pull request:

```bash
# API unit tests (inside a container, no running stack needed)
bin/opencortyx test

# API integration tests (stack running; the admin account, or OPENCORTYX_API_KEY=ax_...)
docker compose exec -e OPENCORTYX_ADMIN_EMAIL=admin@example.com -e OPENCORTYX_ADMIN_PASSWORD='...' \
  api python -m pytest -m integration

# Web
cd apps/web
npm ci
npx tsc --noEmit
npm test
npm run build
```

Lint (the same rules CI enforces):

```bash
pip install ruff==0.16.7
ruff check --select E9,F63,F7,F82,F401 apps/api/opencortyx apps/api/tests apps/api/alembic apps/mcp scripts
```

CI runs the same checks on every pull request.

## End-to-end tests

The Playwright suite in `apps/web/e2e` drives a running stack through a real browser: sign-in,
every dashboard page, ingestion into the catalogue, and an answer from the AI Assistant.

```bash
bin/opencortyx up
cd apps/web
npx playwright install chromium
export OPENCORTYX_ADMIN_EMAIL=admin@example.com OPENCORTYX_ADMIN_PASSWORD='...'
npm run e2e                               # the full suite
npm run e2e -- --grep-invert "@llm|@ingest"   # skip tests that need a working model
npm run screenshots                       # refresh docs/assets/screenshots
```

On a fresh installation the suite creates the admin account with those credentials. Point it at
another deployment with `OPENCORTYX_WEB_URL` and `OPENCORTYX_API_URL`.

## API layout

The REST API lives in `apps/api/opencortyx/api`, one resource per module in each layer:

```
api/router.py               includes every resource router under /api/v2
api/deps.py                 helpers shared by several resources (scoping, auditing, budgets)
api/routes/<resource>.py    HTTP handlers: parse the request, check the role, call a service
api/schemas/<resource>.py   Pydantic request and response models
api/services/<resource>.py  the logic behind the handlers
```

To add an endpoint, put the handler in the matching `routes` module (or create a module and include
its router in `api/router.py`), its models in `schemas`, and anything beyond request handling in
`services`. Routes never import other route modules. Retrieval, ingestion and storage code stays in
the domain packages (`retrieval/`, `ingest/`, `core/`), which the API layer calls.

## Database changes

Create an Alembic migration for schema changes:

```bash
docker compose exec api alembic revision -m "describe the change"
docker compose exec api alembic upgrade head
```

The `setup` service applies migrations automatically on start. See [docs/MIGRATIONS.md](docs/MIGRATIONS.md).

## Conventions

- Branches: `feature/...`, `fix/...`, `chore/...`. Keep pull requests focused.
- Match the style of the surrounding code.
- Tests: unit tests must not need a running stack. Tests that do are marked
  `@pytest.mark.integration`.
- Never commit secrets, `.env` files, database dumps or API keys.

### Comments

- Code says what it does; a comment explains *why*, only when that is not obvious.
- Keep comments to one or two lines. No incident write-ups, measurements, dates or history.
  Those belong in the pull request description or in `docs/`.
- No commented-out code. No TODO without an issue link.
- Docstrings: a one-line summary, plus arguments and return values only when the signature does
  not make them clear.

## Dependencies

- API: edit `apps/api/pyproject.toml`, then refresh the lock with Poetry 1.8:
  `docker run --rm -v "$PWD/apps/api:/src" -w /src python:3.11-slim sh -c "pip install poetry==1.8.2 && poetry lock --no-update"`
- MCP: edit `apps/mcp/requirements.txt`, build the image, then regenerate the pinned lock:
  `docker run --rm --entrypoint pip <mcp image> freeze > apps/mcp/requirements.lock`
- Web: `npm install <package>@<version>` in `apps/web`; keep `npm audit --omit=dev` clean.

## Releasing

Two channels, chosen by the tag:

| Tag | Channel | Images | GitHub release |
|---|---|---|---|
| `vX.Y.Z-beta.N` | beta | `:X.Y.Z-beta.N` and `:beta` | pre-release |
| `vX.Y.Z` | stable | `:X.Y.Z` and `:latest` | normal ("Latest") |

A version ships as betas (`-beta.1`, `-beta.2`, ...) until one passes the checks below, then the
same code is released as `vX.Y.Z`. Never put `dev` in a release version: `bin/opencortyx up` builds
from source for any version containing it, and the workflow refuses it.

1. Set the new version in `VERSION`, `apps/api/pyproject.toml`, `apps/api/opencortyx/__init__.py`,
   `apps/web/package.json` and the two `version` fields at the top of `apps/web/package-lock.json`
   (CI fails if the first four differ).
2. Move the `Unreleased` entries in `CHANGELOG.md` under a `## X.Y.Z` (or `## X.Y.Z-beta.N`) heading.
3. Merge to `main` and wait for the `CI` and `E2E` workflows on that commit to pass.
4. Once it passes, tag and push: `git tag vX.Y.Z-beta.N && git push origin vX.Y.Z-beta.N`.
   Tags are immutable: never move or reuse one; fix forward with the next number.

The `Release` workflow runs the CI checks on the tagged commit, then builds `linux/amd64` and
`linux/arm64` images for the API, web app and MCP server, pushes them to
`ghcr.io/<owner>/opencortyx-{api,web,mcp}`, and creates the GitHub release from the changelog.
`bin/opencortyx up` on a release checkout pulls those images instead of building.

Before promoting a beta to stable: CI, E2E and the regression suite pass on its commit; a fresh
install works on Windows, macOS and Linux; `bin/opencortyx update` from the previous release keeps
the data; no known release blockers are open.

## Reporting bugs

Open an issue with steps to reproduce, the output of `bin/opencortyx doctor`, and relevant logs
(`bin/opencortyx logs api`).
