# Database migrations

The schema is created in two steps, both run by the one-shot `setup` service on every start
(`python -m opencortyx.setup`, after it has readied the graph and vector stores):

1. `init_v2_schema()` creates the `opencortyx_v2` schema, core tables and the base ontology
   (idempotent).
2. `alembic upgrade head` applies the migrations in `apps/api/alembic/versions`.

The API and workers start only after `setup` succeeds. If it fails, see `bin/opencortyx logs setup`.

## Adding a migration

```bash
bin/opencortyx dev
docker compose exec api alembic revision -m "add X"
# edit apps/api/alembic/versions/<new file>.py
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade -1   # check that it reverses
```

Take a backup (`bin/opencortyx backup`) before applying migrations to a database with real data.

## Inspecting state

```bash
docker compose exec api alembic current
docker compose exec api alembic history
```
