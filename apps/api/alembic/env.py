"""Alembic environment for the OpenCortyx v2 store.

Wires Alembic to the app's own ORM metadata (`V2Base.metadata`, including the control-plane tables)
and the isolated `opencortyx_v2` schema. The DB URL comes from app settings (DATABASE_URL) and is
coerced to the sync psycopg2 driver — Alembic runs synchronously; the app runs async (asyncpg).

Autogenerate is scoped to the `opencortyx_v2` schema, and the alembic_version bookkeeping table lives
there too, so migrations never touch `public.*`.
"""
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# Make the app package importable + load its metadata.
import opencortyx.domain  # noqa: F401 — registers every table on V2Base
from opencortyx.domain import V2Base
from opencortyx.core.config import settings, V2_SCHEMA

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = V2Base.metadata


def _sync_url() -> str:
    """The app uses postgresql+asyncpg://; Alembic needs a sync driver."""
    url = settings.DATABASE_URL
    return url.replace("+asyncpg", "+psycopg2").replace("postgresql://", "postgresql+psycopg2://")


def _include_object(obj, name, type_, reflected, compare_to):
    # Only manage objects in our schema (defensive — keeps public.* untouched).
    if type_ == "table":
        return getattr(obj, "schema", None) in (V2_SCHEMA, None)
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        version_table_schema=V2_SCHEMA,
        include_object=_include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _sync_url()
    connectable = engine_from_config(cfg, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.begin() as connection:
        connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {V2_SCHEMA}")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema=V2_SCHEMA,
            include_object=_include_object,
            compare_type=True,
        )
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
