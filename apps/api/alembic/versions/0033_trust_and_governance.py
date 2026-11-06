"""Measured cost, measurable resolution, groups for access, and forgetting that reaches every store

  * llm_usage         one row per model call (domain/governance.py)
  * llm_cache         a deterministic extraction call's answer, deleted with its asset
  * tombstone         hashes of what was forgotten, so nothing reads it back
  * deletion_job      one forget request and what each store reported
  * resolution_label  a person's "same / different" for a pair, keyed by identity, not node id
  * app_user.groups   the access groups a person is in

Idempotent; downgrade drops them.
"""
from alembic import op

revision = "0033_trust_and_governance"
down_revision = "0032_data_plane"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.llm_usage (
            id uuid PRIMARY KEY,
            workspace_id uuid,
            run_id varchar(80),
            purpose varchar(120) NOT NULL DEFAULT 'other',
            role varchar(20),
            provider varchar(40),
            model varchar(200),
            tokens_in integer NOT NULL DEFAULT 0,
            tokens_out integer NOT NULL DEFAULT 0,
            measured boolean NOT NULL DEFAULT false,
            paid boolean NOT NULL DEFAULT false,
            cached boolean NOT NULL DEFAULT false,
            ok boolean NOT NULL DEFAULT true,
            ms integer NOT NULL DEFAULT 0,
            cost_usd double precision NOT NULL DEFAULT 0,
            ts timestamptz DEFAULT now())""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_llm_usage_ws_ts ON {S}.llm_usage (workspace_id, ts)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_llm_usage_run ON {S}.llm_usage (workspace_id, run_id)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.llm_cache (
            key varchar(64) PRIMARY KEY,
            workspace_id uuid NOT NULL,
            asset_id uuid,
            response text NOT NULL,
            tokens_in integer NOT NULL DEFAULT 0,
            tokens_out integer NOT NULL DEFAULT 0,
            created_at timestamptz DEFAULT now(),
            used_at timestamptz DEFAULT now())""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_llm_cache_asset ON {S}.llm_cache (workspace_id, asset_id)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.tombstone (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            kind varchar(20) NOT NULL,
            key_hash varchar(64) NOT NULL,
            job_id uuid,
            created_by varchar(120),
            created_at timestamptz DEFAULT now())""")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_tombstone_lookup ON {S}.tombstone "
               f"(workspace_id, kind, key_hash)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.deletion_job (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            kind varchar(20) NOT NULL,
            target jsonb DEFAULT '{{}}'::jsonb,
            status varchar(20) NOT NULL DEFAULT 'queued',
            steps jsonb DEFAULT '{{}}'::jsonb,
            proof jsonb DEFAULT '{{}}'::jsonb,
            attempts integer NOT NULL DEFAULT 0,
            error text,
            created_by varchar(120),
            created_at timestamptz DEFAULT now(),
            finished_at timestamptz)""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_deletion_job_ws ON {S}.deletion_job (workspace_id, created_at)")

    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.resolution_label (
            id uuid PRIMARY KEY,
            workspace_id uuid NOT NULL,
            node_type varchar(80) NOT NULL,
            pair_hash varchar(64) NOT NULL,
            a jsonb DEFAULT '{{}}'::jsonb,
            b jsonb DEFAULT '{{}}'::jsonb,
            same boolean NOT NULL,
            origin varchar(20) NOT NULL DEFAULT 'manual',
            author varchar(120),
            note varchar(500),
            created_at timestamptz DEFAULT now())""")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ix_v2_resolution_label_pair ON {S}.resolution_label "
               f"(workspace_id, node_type, pair_hash)")

    op.execute(f"ALTER TABLE {S}.app_user ADD COLUMN IF NOT EXISTS groups jsonb NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {S}.app_user DROP COLUMN IF EXISTS groups")
    for table in ("resolution_label", "deletion_job", "tombstone", "llm_cache", "llm_usage"):
        op.execute(f"DROP TABLE IF EXISTS {S}.{table}")
