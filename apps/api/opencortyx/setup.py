"""Make every store ready before anything else starts: graph, vectors, then the database.

Run by the one-shot `setup` service (`python -m opencortyx.setup`) on every start; the API and every
worker wait for it to exit 0. Each step is idempotent, so on an install that is already current the
whole run is a few seconds of checks.

The order is load-bearing:

1. Graph store. Registers NebulaGraph's storage host (`ADD HOSTS`), creates the space, schema and
   indexes, and renames an older space's `org_id`. It goes first, and the service waits only for
   graphd — never for storaged: NebulaGraph 3.x does not register a storage host by itself, and
   storaged never opens its port (so never turns healthy) until this registration exists. Waiting
   for it deadlocked every first install.
2. Vector store. Creates the collection and its payload indexes, and renames an older collection's
   `org_id`.
3. Database. Creates the schema and every table (`bootstrap.init_v2_schema`), then applies the
   migrations (`alembic upgrade head`).
4. Workspaces. Every workspace that has people in it gets its row (name, mode) and its central node:
   the Person or Organization, id = workspace_id, at the top of its graph. A fresh install has none
   until first-run setup creates one (which does the same); an upgraded install gets them here, so
   no workspace depends on someone opening a page first. Needs steps 1 and 3 done.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger("opencortyx.setup")


async def _stores() -> None:
    from opencortyx.stores.graph import get_graph_store
    from opencortyx.stores.vector import get_vector_store
    log.info("1/4 graph store: space, schema and indexes")
    await get_graph_store().initialize()
    log.info("2/4 vector store: collection and indexes")
    await get_vector_store().initialize()
    from opencortyx import bootstrap
    log.info("3/4 database: schema and tables")
    await bootstrap.init_v2_schema()


def _migrate() -> None:
    from alembic import command
    from alembic.config import Config
    log.info("3/4 database: migrations")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    command.upgrade(Config(os.path.join(root, "alembic.ini")), "head")
    # Alembic's env.py applies alembic.ini's logging config: it disables every logger that already
    # existed (this one included) and sets the root to warnings only, so step 4 would run silently,
    # failures and all. Put this script's logging back.
    _logging()
    log.disabled = False


async def _workspaces() -> None:
    from sqlalchemy import select
    from opencortyx.core.db import V2Session
    from opencortyx.domain import User, Workspace
    from opencortyx.knowledge.workspace import ensure_central_node, save_profile
    async with V2Session() as db:
        with_people = set((await db.execute(select(User.workspace_id).distinct())).scalars())
        have_row = set((await db.execute(select(Workspace.workspace_id))).scalars())
    log.info("4/4 workspaces: %d with people", len(with_people))
    for wid in sorted(with_people, key=str):
        if wid in have_row:
            await ensure_central_node(wid)
        else:
            await save_profile(wid)                # writes the row, then the central node


async def _run() -> None:
    await _stores()
    await asyncio.to_thread(_migrate)          # Alembic is synchronous; one event loop throughout
    await _workspaces()


def _logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", force=True)


def main() -> int:
    _logging()
    asyncio.run(_run())
    log.info("setup complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
