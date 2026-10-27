"""The data plane's control rows: data_asset, evidence, source_key (domain/data.py)

Plus workspace.data_store, the per-workspace switch between the graph and the Delta data store,
defaulting to today's behaviour ('graph'). Idempotent; downgrade drops them.
"""
from alembic import op

revision = "0032_data_plane"
down_revision = "0031_ingest_run_batch"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.data_asset (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            kind varchar(20) NOT NULL,
            storage varchar(10) NOT NULL DEFAULT 'managed',
            format varchar(20) NOT NULL DEFAULT 'delta',
            location text NOT NULL DEFAULT '',
            connector_id uuid,
            version_ref varchar(200),
            name varchar(300) NOT NULL DEFAULT '',
            metadata jsonb DEFAULT '{{}}'::jsonb,
            status varchar(20) NOT NULL DEFAULT 'active',
            tags jsonb DEFAULT '[]'::jsonb,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now())""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_data_asset_ws_kind ON {S}.data_asset (workspace_id, kind)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_data_asset_connector ON {S}.data_asset "
               f"(workspace_id, connector_id)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.evidence (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            target_id uuid NOT NULL,
            target_kind varchar(10) NOT NULL DEFAULT 'node',
            property varchar(200),
            asset_id uuid NOT NULL,
            unit_kind varchar(10) NOT NULL,
            unit_key varchar(300) NOT NULL,
            version_ref varchar(200),
            quote text,
            value_hash varchar(64),
            extractor varchar(200),
            run_id uuid,
            confidence double precision,
            observed_at timestamptz DEFAULT now(),
            valid_from timestamptz,
            valid_to timestamptz)""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_evidence_target ON {S}.evidence (workspace_id, target_id)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_evidence_unit ON {S}.evidence "
               f"(workspace_id, asset_id, unit_key)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.source_key (
            workspace_id uuid NOT NULL,
            asset_id uuid NOT NULL,
            native_id varchar(300) NOT NULL,
            entity_type varchar(120) NOT NULL,
            node_id uuid NOT NULL,
            match_method varchar(40) NOT NULL DEFAULT 'declared',
            confidence double precision NOT NULL DEFAULT 1.0,
            first_seen timestamptz DEFAULT now(),
            last_seen timestamptz DEFAULT now(),
            PRIMARY KEY (workspace_id, asset_id, native_id, entity_type))""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_source_key_node ON {S}.source_key (workspace_id, node_id)")
    # Where a workspace's data is written: 'graph' (as before) until it is switched to 'delta'.
    op.execute(f"ALTER TABLE {S}.workspace ADD COLUMN IF NOT EXISTS data_store varchar(10) "
               f"NOT NULL DEFAULT 'graph'")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {S}.workspace DROP COLUMN IF EXISTS data_store")
    for t in ("source_key", "evidence", "data_asset"):
        op.execute(f"DROP TABLE IF EXISTS {S}.{t}")
