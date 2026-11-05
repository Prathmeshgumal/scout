"""The eval runs in Postgres. The metrics page reads them."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@dataclass
class EvalRun:
    suite: str
    model: str
    prompt_sha: str
    total: int
    passed: int
    score: float  # 0 to 1: the main number of the suite
    cost_usd: float = 0.0
    duration_s: float = 0.0
    summary: dict = field(default_factory=dict)
    cases: list = field(default_factory=list)


class EvalStore:
    def __init__(self, pool):
        self.pool = pool

    async def save(self, run: EvalRun) -> dict:
        data = asdict(run)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute("""
                INSERT INTO eval_runs (suite, model, prompt_sha, total, passed, score, cost_usd, duration_s, summary, cases)
                VALUES (%(suite)s, %(model)s, %(prompt_sha)s, %(total)s, %(passed)s, %(score)s, %(cost_usd)s,
                        %(duration_s)s, %(summary)s, %(cases)s)
                RETURNING id::text AS id, created_at
            """, {**data, "summary": Jsonb(data["summary"]), "cases": Jsonb(data["cases"])})
            return await cursor.fetchone()

    async def recent(self, limit: int = 50) -> list[dict]:
        """The newest runs, without the cases."""
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            await cursor.execute("""
                SELECT id::text AS id, suite, model, prompt_sha, total, passed, score::float AS score,
                       cost_usd::float AS cost_usd, duration_s::float AS duration_s, summary, created_at
                FROM eval_runs ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            return await cursor.fetchall()
