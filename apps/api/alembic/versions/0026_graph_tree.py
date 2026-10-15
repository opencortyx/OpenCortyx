"""graph tree — the browse index the dashboard graph draws (root → source → tag → asset)

Five new tables (see `domain/graph_tree.py`). Idempotent: a fresh database already has them from
`create_all`, so this only does work on an install created before them. The rows are derived from
the graph by the index task (pending upload tags aside, which are minutes-lived), so downgrade
simply drops them.
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_graph_tree"
down_revision = "0025_graph_clusters"
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
        CREATE TABLE IF NOT EXISTS {S}.graph_asset (
            org_id uuid NOT NULL,
            asset_id uuid NOT NULL,
            label varchar(300) NOT NULL DEFAULT '',
            type varchar(80) NOT NULL DEFAULT '',
            source varchar(200) NOT NULL DEFAULT '',
            PRIMARY KEY (org_id, asset_id)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_asset_source "
               f"ON {S}.graph_asset ({_tenant('graph_asset')}, source, label)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_asset_tag (
            org_id uuid NOT NULL,
            asset_id uuid NOT NULL,
            tag varchar(80) NOT NULL,
            source varchar(200) NOT NULL DEFAULT '',
            label varchar(300) NOT NULL DEFAULT '',
            auto boolean NOT NULL DEFAULT false,
            PRIMARY KEY (org_id, asset_id, tag)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_asset_tag_tag "
               f"ON {S}.graph_asset_tag ({_tenant('graph_asset_tag')}, source, tag, label)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_root (
            org_id uuid NOT NULL,
            node_id uuid NOT NULL,
            label varchar(300) NOT NULL DEFAULT '',
            type varchar(80) NOT NULL DEFAULT '',
            size integer NOT NULL DEFAULT 1,
            types jsonb,
            PRIMARY KEY (org_id, node_id)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_root_size ON {S}.graph_root ({_tenant('graph_root')}, size)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_tag_request (
            org_id uuid NOT NULL,
            run_id uuid NOT NULL,
            tags jsonb,
            created_at timestamptz DEFAULT now(),
            PRIMARY KEY (org_id, run_id)
        )""")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_tree_run (
            org_id uuid PRIMARY KEY,
            status varchar(20) NOT NULL DEFAULT 'running',
            nodes integer,
            assets integer,
            roots integer,
            duration_ms integer,
            error text,
            started_at timestamptz,
            finished_at timestamptz
        )""")


def downgrade() -> None:
    for t in ("graph_tree_run", "graph_tag_request", "graph_root", "graph_asset_tag", "graph_asset"):
        op.execute(f"DROP TABLE IF EXISTS {S}.{t}")
