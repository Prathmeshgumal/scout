"""The results of the eval suites. Each run of one suite is one row. The metrics page shows them."""

from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE eval_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            suite text NOT NULL,
            model text NOT NULL,
            prompt_sha text NOT NULL,
            total integer NOT NULL CHECK (total >= 0),
            passed integer NOT NULL CHECK (passed >= 0),
            score numeric NOT NULL,
            cost_usd numeric NOT NULL DEFAULT 0,
            duration_s numeric NOT NULL DEFAULT 0,
            summary jsonb NOT NULL DEFAULT '{}'::jsonb,
            cases jsonb NOT NULL DEFAULT '[]'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX eval_runs_suite_created ON eval_runs (suite, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE eval_runs")
