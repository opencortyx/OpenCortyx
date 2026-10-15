"""api_key.created_by — who issued a key, so an agent's sessions name a real person

Nullable, no backfill: a key issued before this column existed has no recorded issuer, and guessing
one (the workspace's first admin was the previous stand-in) would attribute an agent's activity to
someone who may never have issued it. Idempotent. Downgrade drops the column.
"""
from alembic import op

revision = "0028_api_key_created_by"
down_revision = "0027_chat_history"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {S}.api_key ADD COLUMN IF NOT EXISTS created_by uuid")


def downgrade() -> None:
    op.execute(f"ALTER TABLE {S}.api_key DROP COLUMN IF EXISTS created_by")
