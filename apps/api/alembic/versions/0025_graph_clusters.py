"""graph clusters — the community hierarchy the dashboard graph draws

Four new tables (see `domain/graph_clusters.py`). Idempotent: a fresh database already has them from
`create_all`, so this only does work on an install created before them. The rows are derived data,
rebuilt by the clustering task, so downgrade simply drops them.
"""
import sqlalchemy as sa
from alembic import op

revision = "0025_graph_clusters"
down_revision = "0024_resolution_engine"
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
        CREATE TABLE IF NOT EXISTS {S}.graph_cluster (
            org_id uuid NOT NULL,
            id varchar(40) NOT NULL,
            parent_id varchar(40) NOT NULL DEFAULT '',
            depth integer NOT NULL DEFAULT 0,
            kind varchar(20) NOT NULL DEFAULT 'community',
            label varchar(300) NOT NULL DEFAULT '',
            size integer NOT NULL DEFAULT 0,
            n_children integer NOT NULL DEFAULT 0,
            n_members integer NOT NULL DEFAULT 0,
            internal_edges integer NOT NULL DEFAULT 0,
            external_edges integer NOT NULL DEFAULT 0,
            rep_node_id uuid,
            types jsonb,
            preview jsonb,
            PRIMARY KEY (org_id, id)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_cluster_parent "
               f"ON {S}.graph_cluster ({_tenant('graph_cluster')}, parent_id)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_cluster_member (
            org_id uuid NOT NULL,
            node_id uuid NOT NULL,
            cluster_id varchar(40) NOT NULL,
            label varchar(300) NOT NULL DEFAULT '',
            type varchar(80) NOT NULL DEFAULT '',
            degree integer NOT NULL DEFAULT 0,
            PRIMARY KEY (org_id, node_id)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_cluster_member_rank "
               f"ON {S}.graph_cluster_member ({_tenant('graph_cluster_member')}, cluster_id, degree)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_cluster_link (
            org_id uuid NOT NULL,
            a_id varchar(40) NOT NULL,
            b_id varchar(40) NOT NULL,
            parent_id varchar(40) NOT NULL DEFAULT '',
            weight double precision NOT NULL DEFAULT 0,
            edges integer NOT NULL DEFAULT 0,
            edge_types jsonb,
            PRIMARY KEY (org_id, a_id, b_id)
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_graph_cluster_link_parent "
               f"ON {S}.graph_cluster_link ({_tenant('graph_cluster_link')}, parent_id)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.graph_cluster_run (
            org_id uuid PRIMARY KEY,
            status varchar(20) NOT NULL DEFAULT 'running',
            nodes integer,
            edges integer,
            clusters integer,
            depth integer,
            modularity double precision,
            duration_ms integer,
            error text,
            started_at timestamptz,
            finished_at timestamptz,
            updated_at timestamptz DEFAULT now()
        )""")


def downgrade() -> None:
    for t in ("graph_cluster_run", "graph_cluster_link", "graph_cluster_member", "graph_cluster"):
        op.execute(f"DROP TABLE IF EXISTS {S}.{t}")
