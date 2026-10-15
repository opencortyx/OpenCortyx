"""workspace — `org_id` is renamed `workspace_id`, and each workspace gets a name and a mode

1. Every `org_id` column in the schema becomes `workspace_id`. Found by catalogue rather than listed,
   so a table no model declares (the write ledgers) is renamed too. A table that already has
   `workspace_id` — a fresh database `create_all` built after the rename — is left alone.
2. A `workspace` row per workspace: its name and mode. Both were install-wide runtime settings
   (`workspace_name`, `mode`), shared by every workspace on an install; their values carry over to
   the workspaces that have people in them, and the two setting rows are dropped.

The graph and vector stores carry the same field; each renames it when it starts (see their
adapters), since Alembic does not reach them. Idempotent. Downgrade renames the columns back and
drops the table; it does not restore the two settings.
"""
from alembic import op

revision = "0029_workspace"
down_revision = "0028_api_key_created_by"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def _rename(old: str, new: str) -> None:
    op.execute(f"""
    DO $$
    DECLARE r record;
    BEGIN
      FOR r IN SELECT c.table_name FROM information_schema.columns c
               WHERE c.table_schema = '{S}' AND c.column_name = '{old}'
                 AND NOT EXISTS (SELECT 1 FROM information_schema.columns n
                                 WHERE n.table_schema = '{S}' AND n.table_name = c.table_name
                                   AND n.column_name = '{new}')
      LOOP
        EXECUTE format('ALTER TABLE %I.%I RENAME COLUMN {old} TO {new}', '{S}', r.table_name);
      END LOOP;
    END $$;""")


def upgrade() -> None:
    _rename("org_id", "workspace_id")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.workspace (
            workspace_id uuid PRIMARY KEY,
            name varchar(120) NOT NULL DEFAULT '',
            mode varchar(20) NOT NULL DEFAULT 'person',
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now()
        )""")
    op.execute(f"""
        INSERT INTO {S}.workspace (workspace_id, name, mode)
        SELECT DISTINCT u.workspace_id,
               COALESCE(LEFT((SELECT value->>'v' FROM {S}.runtime_setting WHERE key = 'workspace_name'), 120), ''),
               COALESCE((SELECT value->>'v' FROM {S}.runtime_setting
                         WHERE key = 'mode' AND value->>'v' IN ('person', 'team')), 'person')
        FROM {S}.app_user u
        ON CONFLICT (workspace_id) DO NOTHING""")
    op.execute(f"DELETE FROM {S}.runtime_setting WHERE key IN ('workspace_name', 'mode')")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {S}.workspace")
    _rename("workspace_id", "org_id")
