"""The whole schema: tables, triggers, grants and Procrastinate's queue."""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The raw cursor sends the file as it is, with no parameter parsing, because
    # the function bodies contain `%` and the check constraints contain `::`.
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().connection.cursor().execute(sql)


def downgrade() -> None:
    raise NotImplementedError("the baseline has nothing to go back to")
