"""ingest_run.batch_id: the files dropped together, so a batch's progress is one query

Add data reported a batch's progress by asking for every run in it, one request each; a
1000-file drop was 1000 requests every poll. With the batch on the run, it is one GROUP BY.
Nullable, no backfill: earlier runs have no batch to report. Idempotent; downgrade drops it.
"""
from alembic import op

revision = "0031_ingest_run_batch"
down_revision = "0030_registry_only_models"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {S}.ingest_run ADD COLUMN IF NOT EXISTS batch_id varchar(80)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_ingest_run_batch ON {S}.ingest_run "
               f"(workspace_id, batch_id) WHERE batch_id IS NOT NULL")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {S}.ix_ingest_run_batch")
    op.execute(f"ALTER TABLE {S}.ingest_run DROP COLUMN IF EXISTS batch_id")
