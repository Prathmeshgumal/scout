"""Postgres: migrations, the connection pool and the lock on a goal."""

from __future__ import annotations

import psycopg
from alembic import command
from alembic.config import Config
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import ROOT


def migrate(database_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("postgresql://", "postgresql+psycopg://", 1))
    command.upgrade(config, "head")


async def open_pool(database_url: str) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(database_url, open=False, kwargs={"row_factory": dict_row, "autocommit": True})
    await pool.open()
    return pool


def mandate_lock_key(mandate_id: str) -> str:
    """The GoalLock key of a mandate. The scheduler and a delete hold it, so that a goal does not start in a delete."""
    return f"mandate:{mandate_id}"


class GoalLock:
    """A Postgres advisory lock on one goal, held on its own connection while this process drives the goal."""

    def __init__(self, conn: psycopg.AsyncConnection):
        self._conn = conn

    @classmethod
    async def acquire(cls, database_url: str, goal_id: str) -> GoalLock | None:
        conn = await psycopg.AsyncConnection.connect(database_url, autocommit=True)
        cursor = await conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (goal_id,))
        if not (await cursor.fetchone())[0]:
            await conn.close()
            return None
        return cls(conn)

    async def release(self) -> None:
        await self._conn.close()  # closing the session releases the lock
