"""The order of the cards in each column of a board. The user drags a card up or down to give it a higher place."""

from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE board_order (
            board text NOT NULL CHECK (board IN ('pipeline', 'jobs')),
            item_key text NOT NULL,
            column_id text NOT NULL,
            position integer NOT NULL,
            PRIMARY KEY (board, item_key)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE board_order")
