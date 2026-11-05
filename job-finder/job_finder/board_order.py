"""The order of the cards in each column of a board, from the top (the highest place) down.

A place is for one column. When a card goes to another column, its old place no longer counts: the board puts it
with the cards that have no place. A card that comes later has no place either. The browser puts those cards first."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

BOARDS = ("pipeline", "jobs")


class BoardOrder:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def positions(self, board: str) -> dict[str, dict]:
        async with self.pool.connection() as conn:
            rows = await (await conn.execute(
                "SELECT item_key, column_id, position FROM board_order WHERE board = %s", (board,))).fetchall()
        return {row["item_key"]: {"column": row["column_id"], "position": row["position"]} for row in rows}

    async def save(self, board: str, column: str, keys: list[str]) -> None:
        """Give the cards of a column their places: the first key is at the top."""
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("DELETE FROM board_order WHERE board = %s AND column_id = %s", (board, column))
            if keys:
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO board_order (board, item_key, column_id, position) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (board, item_key) DO UPDATE SET column_id = EXCLUDED.column_id, position = EXCLUDED.position",
                        [(board, key, column, i) for i, key in enumerate(keys)])
