"""chat history — the Assistant sidebar's conversations and their turns

Two new tables (see `domain/chat.py`). Idempotent: a fresh database already has them from
`create_all`, so this only does work on an install created before them. Downgrade drops them,
which deletes every saved chat.
"""
import sqlalchemy as sa
from alembic import op

revision = "0027_chat_history"
down_revision = "0026_graph_tree"
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
        CREATE TABLE IF NOT EXISTS {S}.chat_thread (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL,
            user_id uuid NOT NULL,
            title varchar(200) NOT NULL DEFAULT 'New chat',
            message_count integer NOT NULL DEFAULT 0,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now()
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_chat_thread_owner "
               f"ON {S}.chat_thread ({_tenant('chat_thread')}, user_id, updated_at)")
    op.execute(f"""
        CREATE TABLE IF NOT EXISTS {S}.chat_message (
            id uuid PRIMARY KEY,
            thread_id uuid NOT NULL REFERENCES {S}.chat_thread (id) ON DELETE CASCADE,
            ordinal integer NOT NULL,
            role varchar(20) NOT NULL,
            text text NOT NULL DEFAULT '',
            meta jsonb,
            created_at timestamptz DEFAULT now()
        )""")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_v2_chat_message_thread "
               f"ON {S}.chat_message (thread_id, ordinal)")


def downgrade() -> None:
    op.execute(f"DROP TABLE IF EXISTS {S}.chat_message")
    op.execute(f"DROP TABLE IF EXISTS {S}.chat_thread")
