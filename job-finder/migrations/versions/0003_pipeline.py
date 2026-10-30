"""The job pipeline. It belongs to the role and not to a goal, so it stays when a goal goes."""

from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE pipeline_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            role_key text NOT NULL UNIQUE,
            stage text NOT NULL CHECK (stage IN ('fit', 'referral', 'applied', 'responded', 'removed')),
            title text NOT NULL,
            company_name text NOT NULL,
            location text NULL,
            workplace_type text NULL,
            posting_status text NULL,
            url text NOT NULL,
            goal_title text NULL,
            fit text NULL,
            fit_reasons text NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE TABLE pipeline_notes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            pipeline_job_id uuid NOT NULL REFERENCES pipeline_jobs (id) ON DELETE CASCADE,
            text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX pipeline_notes_job ON pipeline_notes (pipeline_job_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE pipeline_notes")
    op.execute("DROP TABLE pipeline_jobs")
