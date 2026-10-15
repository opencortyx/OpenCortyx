"""Making a database ready to run OpenCortyx: schema, tables, built-in ontology, first admin.

This is the composition root for start-up. It sits ABOVE `core` on purpose. Creating the schema is
infrastructure and belongs in `core.db`; deciding that a fresh install ships with the
Person/Organization templates is a product decision, and wiring the two together
is neither. Keeping that composition here is what lets `core.db` stay a database module that the
ontology can depend on, instead of one that reaches back up into the ontology to finish its own job.

`init_v2_schema()` keeps the contract it has always had — idempotent, safe to call from every task
and every process, serialised across processes by an advisory lock — so callers only changed where
they import it from.
"""
from __future__ import annotations

import os
import uuid as _uuid

from sqlalchemy import func as _func, select

from opencortyx.core.db import V2Session, create_schema_and_tables, schema_exists
from opencortyx.core.config import settings

__all__ = ["init_v2_schema"]

#: PIDs whose bootstrap has completed. Per-process, not shared: a forked Celery child has its own
#: connections and must satisfy itself the schema is there before it writes.
_bootstrapped: set = set()


async def init_v2_schema(force: bool = False):
    """Create the schema, the control-plane tables and the built-in ontology. Idempotent.

    Serialised across processes by an advisory lock and skipped after it has succeeded once in a
    process. `force=True` re-runs it, for tooling that recreates the schema under a live process.
    """
    pid = os.getpid()
    from opencortyx.core.config import runtime as _rc
    try:
        await _rc.load()
    except Exception:                                   # noqa: BLE001 - never block ingestion
        pass
    if not force and pid in _bootstrapped:
        return
    if not force and await schema_exists():
        _bootstrapped.add(pid)
        return

    await create_schema_and_tables()

    from opencortyx.ontology import seed_org_ontology, seed_person_ontology
    from opencortyx.ingest.structured.catalog import seed_structured_ontology
    await seed_structured_ontology()
    await seed_person_ontology()
    await seed_org_ontology()

    from opencortyx.core.config import runtime as runtime_config
    await runtime_config.load()
    await _seed_default_admin()
    _bootstrapped.add(pid)


async def _seed_default_admin():
    from opencortyx.core.security.auth import hash_password
    from opencortyx.core.observability import get_logger
    from opencortyx.domain import User
    pw = settings.DEFAULT_ADMIN_PASSWORD
    if not pw:
        return                  # secure default: no shipped credential → first-run Setup page is used
    email = settings.DEFAULT_ADMIN_EMAIL.strip().lower()
    async with V2Session() as db:
        n = (await db.execute(select(_func.count()).select_from(User))).scalar() or 0
        if n > 0:
            return                                        # an admin already exists
        workspace_id = _uuid.uuid5(_uuid.NAMESPACE_DNS, "axiom-org:demo")   # adopt existing demo data
        db.add(User(workspace_id=workspace_id, email=email, password_hash=hash_password(pw),
                    role="admin", status="active"))
        await db.commit()
        get_logger().info("bootstrap.seeded_default_admin", email=email)
