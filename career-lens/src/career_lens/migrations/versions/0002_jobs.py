"""Signed-in job postings, keyword job search, and job pages per request."""

from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = Path(__file__).with_suffix(".sql").read_text()
    op.get_bind().connection.cursor().execute(sql)


def downgrade() -> None:
    raise NotImplementedError("append-only evidence has nothing to go back to")
