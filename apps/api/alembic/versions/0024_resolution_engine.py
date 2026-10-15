"""resolution engine — the identity index and the decision ledger

Two new tables (see `domain/resolution.py`). Idempotent: a fresh database already has them from
`create_all`, so this only does work on an install created before them.
"""
import sqlalchemy as sa
from alembic import op

revision = "0024_resolution_engine"
down_revision = "0023_ontology_per_org"
branch_labels = None
depends_on = None

S = "opencortyx_v2"


def _tenant(table: str) -> str:
    """The tenant column as `table` names it now. `create_all` builds a fresh database with
    `workspace_id`; a table this chain created on an older install still has `org_id` until
    0029_workspace renames it. An index must name whichever one is there."""
    found = op.get_bind().execute(sa.text(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = :s AND table_name = :t "
        "AND column_name = 'workspace_id'"), {"s": S, "t": table}).first()
    return "workspace_id" if found else "org_id"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.resolution_key (
            org_id uuid NOT NULL,
            node_type varchar(80) NOT NULL,
            key_hash varchar(64) NOT NULL,
            node_id uuid NOT NULL,
            created_at timestamptz DEFAULT now(),
            PRIMARY KEY (org_id, node_type, key_hash, node_id)
        )""")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.resolution_decision (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL,
            source_id uuid,
            node_type varchar(80) NOT NULL,
            relation varchar(20) NOT NULL,
            subject uuid NOT NULL,
            target uuid,
            attribute varchar(200),
            confidence double precision,
            method varchar(20),
            evidence jsonb,
            needs_review boolean,
            created_at timestamptz DEFAULT now()
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_resolution_subject ON {S}.resolution_decision ({_tenant('resolution_decision')}, subject)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_resolution_review ON {S}.resolution_decision ({_tenant('resolution_decision')}, needs_review)")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {S}.resolution_decision")
    op.execute(f"DROP TABLE IF EXISTS {S}.resolution_key")
