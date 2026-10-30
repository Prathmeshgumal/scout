"""Files that the user uploads."""

from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE files (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            goal_id uuid NULL REFERENCES goals (id) ON DELETE CASCADE,
            role text NOT NULL CHECK (role IN ('resume', 'attachment')),
            name text NOT NULL,
            content_type text NOT NULL,
            kind text NOT NULL,
            size_bytes bigint NOT NULL,
            sha256 text NOT NULL,
            object_key text NOT NULL UNIQUE,
            text text NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX files_goal ON files (goal_id)")
    op.execute("CREATE UNIQUE INDEX files_one_resume ON files (role) WHERE role = 'resume'")


def downgrade() -> None:
    op.execute("DROP TABLE files")
