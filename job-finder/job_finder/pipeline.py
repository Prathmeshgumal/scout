"""The job pipeline: the user moves roles through the stages and keeps notes.

A row belongs to the role and keeps a copy of its facts. So a tracked role and its notes stay when its goal goes, and
a role that two goals found shows once. A fit role has no row until the user changes it: it is in the first stage."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

STAGES = ("fit", "referral", "applied", "responded")
REMOVED = "removed"
FACTS = ("title", "company_name", "location", "workplace_type", "posting_status", "url", "goal_title", "fit", "fit_reasons")

# The best job of each role: a fit verdict first, then the newest.
BEST_JOBS = """
    SELECT DISTINCT ON (j.role_key) j.role_key, j.id::text AS job_id, j.goal_id::text AS goal_id, j.title, j.company_name,
           j.location, j.workplace_type, j.posting_status, j.url, j.evidence_level,
           coalesce(g.brief->>'title', g.title) AS goal_title,
           j.fit, j.fit_reasons
    FROM jobs j JOIN goals g ON g.id = j.goal_id
    {where}
    ORDER BY j.role_key, coalesce(j.fit = 'fit', false) DESC, j.created_at DESC
"""


def _newest(item: dict) -> tuple:
    return (item["updated_at"] is not None, item["updated_at"] or 0)


class PipelineStore:
    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def board(self) -> dict:
        """The roles in the pipeline and the removed roles, with their notes. The role that changed last comes first."""
        async with self.pool.connection() as conn:
            jobs = await (await conn.execute(BEST_JOBS.format(where=""))).fetchall()
            rows = await (await conn.execute("SELECT * FROM pipeline_jobs")).fetchall()
            notes = await (await conn.execute(
                "SELECT id::text AS id, pipeline_job_id, text, created_at FROM pipeline_notes ORDER BY created_at, id"
            )).fetchall()
        tracked = {row["role_key"]: row for row in rows}
        best = {job["role_key"]: job for job in jobs}
        items = []
        removed = []
        for row in rows:
            job = best.get(row["role_key"])
            facts = job or {**{key: row[key] for key in FACTS}, "job_id": None, "goal_id": None}
            (removed if row["stage"] == REMOVED else items).append(
                {**facts, "role_key": row["role_key"], "stage": row["stage"], "updated_at": row["updated_at"],
                 "notes": [{key: n[key] for key in ("id", "text", "created_at")} for n in notes
                           if n["pipeline_job_id"] == row["id"]]})
        for job in jobs:
            if job["fit"] == "fit" and job["role_key"] not in tracked:
                items.append({**job, "stage": STAGES[0], "updated_at": None, "notes": []})
        return {"items": sorted(items, key=_newest, reverse=True), "removed": sorted(removed, key=_newest, reverse=True)}

    async def items(self) -> list[dict]:
        """The roles in the pipeline."""
        return (await self.board())["items"]

    async def item(self, role_key: str) -> dict | None:
        return next((item for item in await self.items() if item["role_key"] == role_key), None)

    async def stage_of(self, role_key: str) -> str | None:
        """The stage of a role. None: it is not in the pipeline."""
        async with self.pool.connection() as conn:
            row = await (await conn.execute("SELECT stage FROM pipeline_jobs WHERE role_key = %s", (role_key,))).fetchone()
            if row:
                return None if row["stage"] == REMOVED else row["stage"]
            fit = await (await conn.execute(
                "SELECT 1 FROM jobs WHERE role_key = %s AND fit = 'fit' LIMIT 1", (role_key,))).fetchone()
        return STAGES[0] if fit else None

    async def _track(self, conn, role_key: str, stage: str | None) -> str | None:
        """Make or change the row of a role, with the newest facts. stage None keeps the stage. Return the row id."""
        job = await (await conn.execute(BEST_JOBS.format(where="WHERE j.role_key = %s"), (role_key,))).fetchone()
        if job is None:
            row = await (await conn.execute(
                "UPDATE pipeline_jobs SET stage = coalesce(%s, stage), updated_at = now() WHERE role_key = %s "
                "RETURNING id::text AS id", (stage, role_key))).fetchone()
            return row["id"] if row else None
        columns = ", ".join(FACTS)
        updates = ", ".join(f"{key} = EXCLUDED.{key}" for key in FACTS)
        row = await (await conn.execute(
            f"INSERT INTO pipeline_jobs (role_key, stage, {columns}) "
            f"VALUES (%s, %s, {', '.join(['%s'] * len(FACTS))}) ON CONFLICT (role_key) DO UPDATE SET "
            f"stage = coalesce(%s, pipeline_jobs.stage), updated_at = now(), {updates} RETURNING id::text AS id",
            (role_key, stage or STAGES[0], *(job[key] for key in FACTS), stage))).fetchone()
        return row["id"]

    async def set_stage(self, role_key: str, stage: str) -> dict | None:
        """Move a role. REMOVED takes it out of the pipeline. None: the app does not know this role."""
        async with self.pool.connection() as conn, conn.transaction():
            found = await self._track(conn, role_key, stage)
        if found is None:
            return None
        return await self.item(role_key) if stage != REMOVED else {"role_key": role_key, "stage": REMOVED}

    async def add_note(self, role_key: str, text: str) -> dict | None:
        async with self.pool.connection() as conn, conn.transaction():
            row_id = await self._track(conn, role_key, None)
            if row_id is None:
                return None
            return await (await conn.execute(
                "INSERT INTO pipeline_notes (pipeline_job_id, text) VALUES (%s, %s) "
                "RETURNING id::text AS id, text, created_at", (row_id, text))).fetchone()

    async def delete_note(self, note_id: str) -> bool:
        async with self.pool.connection() as conn:
            row = await (await conn.execute("DELETE FROM pipeline_notes WHERE id = %s RETURNING id", (note_id,))).fetchone()
        return row is not None
