"""ontology per organization — `type_registry` gains `org_id`, part of its key

Before this, one table served every organization: a candidate type discovered in one tenant's data
showed in every tenant's review inbox, and any editor's promotion or description edit changed
extraction for all of them.

Built-in types (seeded at start-up: sources `system`, `person`, `organization`) stay shared, owned
by the all-zero PLATFORM org. Every other existing row was created by some tenant, and the table
never recorded which, so each is copied to every organization that has users: nobody loses a type
they can see today, and from here on each copy changes independently.

Idempotent: a fresh database already has the column (`create_all` builds it from the model), so the
block only runs on an install created before it.
"""
from alembic import op

revision = "0023_ontology_per_org"
down_revision = "0022_graph_to_stores"
branch_labels = None
depends_on = None

S = "opencortyx_v2"
PLATFORM = "00000000-0000-0000-0000-000000000000"
BUILT_IN = "('system', 'person', 'organization')"


def upgrade() -> None:
    op.execute(f"""
    DO $$
    BEGIN
      -- either name: a database created after 0029_workspace's rename has `workspace_id` already
      IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_schema = '{S}' AND table_name = 'type_registry'
                       AND column_name IN ('org_id', 'workspace_id')) THEN
        ALTER TABLE {S}.type_registry ADD COLUMN org_id uuid NOT NULL DEFAULT '{PLATFORM}'::uuid;
        ALTER TABLE {S}.type_registry DROP CONSTRAINT type_registry_pkey;
        ALTER TABLE {S}.type_registry ADD PRIMARY KEY (org_id, name, kind);

        IF EXISTS (SELECT 1 FROM {S}.app_user) THEN
          INSERT INTO {S}.type_registry (org_id, name, kind, description, properties_schema,
                                         allowed_source_types, allowed_target_types, is_open,
                                         source, status, created_at)
          SELECT o.org_id, t.name, t.kind, t.description, t.properties_schema,
                 t.allowed_source_types, t.allowed_target_types, t.is_open, t.source, t.status, t.created_at
          FROM {S}.type_registry t
          CROSS JOIN (SELECT DISTINCT org_id FROM {S}.app_user) o
          WHERE t.org_id = '{PLATFORM}'::uuid AND COALESCE(t.source, '') NOT IN {BUILT_IN};

          DELETE FROM {S}.type_registry
          WHERE org_id = '{PLATFORM}'::uuid AND COALESCE(source, '') NOT IN {BUILT_IN};
        END IF;
      END IF;
    END $$;""")


def downgrade() -> None:
    raise NotImplementedError("per-organization rows cannot be folded back into one shared table safely")
