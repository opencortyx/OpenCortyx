"""baseline — everything the schema needs beyond `V2Base.metadata.create_all`

Squashes the 22 migrations that led here. Start-up (`bootstrap.init_v2_schema`) creates every ORM
table; this adds what the ORM does not declare: the raw-SQL write ledgers (`write_accounting`,
`write_quarantine`) and four indexes. Every statement is idempotent, so it is safe on a fresh
database and a no-op on one the old chain already brought to head.

The revision id is the old head's on purpose. A database stamped `0022_graph_to_stores` is already
at head, so upgrading an existing install runs nothing; a new id would make Alembic refuse it with
"Can't locate revision". The next migration sets `down_revision = "0022_graph_to_stores"`.

Equivalence was checked by schema dump: a fresh database built by create_all + this file matches one
built by create_all + the 22 old migrations.
"""
import sqlalchemy as sa
from alembic import op

revision = "0022_graph_to_stores"
down_revision = None
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
        CREATE TABLE IF NOT EXISTS {S}.write_accounting (
            org_id uuid PRIMARY KEY,
            attempted bigint DEFAULT 0 NOT NULL,
            written bigint DEFAULT 0 NOT NULL,
            quarantined bigint DEFAULT 0 NOT NULL,
            unaccounted bigint DEFAULT 0 NOT NULL,
            updated_at timestamptz DEFAULT now() NOT NULL,
            escaped bigint DEFAULT 0 NOT NULL,
            upstream_dropped bigint DEFAULT 0 NOT NULL,
            created bigint DEFAULT 0 NOT NULL,
            updated bigint DEFAULT 0 NOT NULL,
            deleted bigint DEFAULT 0 NOT NULL,
            baseline_at timestamptz DEFAULT now() NOT NULL,
            baseline_reason text
        )""")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.write_quarantine (
            id bigserial PRIMARY KEY,
            org_id uuid NOT NULL,
            kind varchar(16) NOT NULL,
            fingerprint varchar(64) NOT NULL,
            payload jsonb NOT NULL,
            reason varchar(64) NOT NULL,
            retryable boolean DEFAULT true NOT NULL,
            attempts integer DEFAULT 0 NOT NULL,
            last_error text,
            asset varchar(500),
            created_at timestamptz DEFAULT now() NOT NULL,
            last_tried_at timestamptz,
            resolved_at timestamptz
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_quarantine_pending "
               f"ON {S}.write_quarantine (org_id, resolved_at, retryable, attempts)")
    op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_quarantine_fp "
               f"ON {S}.write_quarantine (org_id, fingerprint)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_run_live ON {S}.ingest_run (status, updated_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_schema_mapping_org ON {S}.schema_mapping ({_tenant('schema_mapping')}, paradigm)")


def downgrade() -> None:
    raise NotImplementedError("the baseline has no predecessor to return to")
