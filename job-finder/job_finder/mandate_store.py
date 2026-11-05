"""SQL for mandates and their runs. A run is a goal with mandate_id and run_slot."""

from __future__ import annotations

from datetime import datetime

from psycopg.types.json import Jsonb

from .store import GOAL, Queries, goal_title

# The settings that a user can change, in the chat of the mandate. The profile comes from the chat, ready.
FIELDS = ("title", "instruction", "profile", "profile_ready", "every_unit", "every_count", "jobs_per_run",
          "window_days", "window_start", "window_end", "max_spend_per_run", "max_scrapes_per_run", "max_total_spend",
          "ends_on", "timezone", "time_limit_minutes", "grace_minutes")
WRITABLE = (*FIELDS, "status", "next_run_at")
MANDATE = ("id::text AS id, home_goal_id::text AS home_goal_id, title, instruction, profile, profile_ready, "
           "status, every_unit, every_count, jobs_per_run, window_days, window_start, window_end, timezone, "
           "max_spend_per_run::float8 AS max_spend_per_run, max_scrapes_per_run, "
           "max_total_spend::float8 AS max_total_spend, time_limit_minutes, grace_minutes, ends_on, next_run_at, goals_started, created_at, updated_at")
SETTLED = ("done", "capped", "failed", "stopped")
RUN_NUMBER = "g.run_number AS number"  # kept on the goal: a deleted goal does not move the numbers of the others


def _adapted(values: dict) -> dict:
    """The values for SQL: the profile is JSON."""
    return {**values, "profile": Jsonb(values["profile"])} if values.get("profile") is not None else values


class MandateStore(Queries):
    async def create(self, home_goal_id: str, values: dict) -> dict:
        keys = [key for key in WRITABLE if key in values]
        columns = ", ".join(["home_goal_id", *keys])
        params = ", ".join(["%(home_goal_id)s", *(f"%({key})s" for key in keys)])
        return await self._one(f"INSERT INTO mandates ({columns}) VALUES ({params}) RETURNING {MANDATE}",
                               {"home_goal_id": home_goal_id, **_adapted({key: values[key] for key in keys})})

    async def get(self, mandate_id: str) -> dict | None:
        return await self._one(f"SELECT {MANDATE} FROM mandates WHERE id = %s", (mandate_id,))

    async def get_for_run(self, goal_id: str) -> dict | None:
        return await self._one(f"SELECT {MANDATE} FROM mandates WHERE id = (SELECT mandate_id FROM goals WHERE id = %s)",
                               (goal_id,))

    async def by_home(self, goal_id: str) -> dict | None:
        return await self._one(f"SELECT {MANDATE} FROM mandates WHERE home_goal_id = %s", (goal_id,))

    async def by_prefix(self, prefix: str) -> list[str]:
        rows = await self._all("SELECT id::text AS id FROM mandates WHERE id::text LIKE %s ORDER BY created_at LIMIT 2",
                               (f"{prefix}%",))
        return [row["id"] for row in rows]

    async def list(self) -> list[dict]:
        return await self._all(f"SELECT {MANDATE} FROM mandates ORDER BY created_at DESC")

    async def update(self, mandate_id: str, values: dict, *, seen: datetime | None = None) -> dict | None:
        """Write the values. With `seen`, write only when updated_at is still `seen`, else return None: an other
        change came first, and the caller reads the mandate again."""
        unknown = sorted(set(values) - set(WRITABLE))
        if unknown:
            raise ValueError(f"Not a schedule setting: {', '.join(unknown)}")
        sets = ", ".join([*(f"{key} = %({key})s" for key in values), "updated_at = clock_timestamp()"])
        where = "id = %(id)s" + (" AND updated_at = %(seen)s" if seen else "")
        return await self._one(f"UPDATE mandates SET {sets} WHERE {where} RETURNING {MANDATE}",
                               {**_adapted(values), "id": mandate_id, "seen": seen})

    async def claim(self, m: dict, next_run_at: datetime) -> bool:
        """Move an active mandate to its next slot, only when nothing changed it since the scheduler read it."""
        row = await self._one("UPDATE mandates SET next_run_at = %s, updated_at = clock_timestamp() "
                              "WHERE id = %s AND status = 'active' AND updated_at = %s RETURNING id",
                              (next_run_at, m["id"], m["updated_at"]))
        return row is not None

    async def due(self, now: datetime) -> list[dict]:
        return await self._all(f"SELECT {MANDATE} FROM mandates WHERE status = 'active' AND next_run_at <= %s "
                               "ORDER BY next_run_at", (now,))

    async def history(self, mandate_id: str) -> dict:
        """What the earlier runs found, for the kick-off message of the next run. It stays the same size: the
        counts and the 3 newest reasons for not_fit."""
        counts = await self._one("""
            SELECT (SELECT count(*) FROM goals WHERE mandate_id = %(m)s)::int AS runs,
                   count(l.id)::int AS jobs,
                   count(l.id) FILTER (WHERE l.fit = 'fit')::int AS fit,
                   count(l.id) FILTER (WHERE l.fit = 'not_fit')::int AS not_fit,
                   count(l.id) FILTER (WHERE l.fit = 'unsure')::int AS unsure
            FROM jobs l JOIN goals g ON g.id = l.goal_id WHERE g.mandate_id = %(m)s
        """, {"m": mandate_id})
        reasons = await self._all("""
            SELECT l.fit_reasons AS text FROM jobs l JOIN goals g ON g.id = l.goal_id
            WHERE g.mandate_id = %s AND l.fit = 'not_fit' AND l.fit_reasons IS NOT NULL
            ORDER BY l.updated_at DESC LIMIT 3
        """, (mandate_id,))
        return {**counts, "reasons": [r["text"] for r in reasons]}

    # Runs

    async def runs(self, mandate_id: str) -> list[dict]:
        return await self._all(f"""
            SELECT g.id::text AS id, g.title, g.status, g.run_slot, g.spend_usd::float8 AS spend_usd, {RUN_NUMBER}
            FROM goals g WHERE g.mandate_id = %s ORDER BY g.run_slot
        """, (mandate_id,))

    async def slots(self, mandate_id: str) -> tuple[datetime | None, datetime | None]:
        """The slots of the first and the newest goal of the mandate. The schedule counts from them."""
        row = await self._one("SELECT min(run_slot) AS first, max(run_slot) AS last FROM goals WHERE mandate_id = %s",
                              (mandate_id,))
        return row["first"], row["last"]

    async def has_running_run(self, mandate_id: str) -> bool:
        # A run that waits on scrapes is in progress too: one run at a time.
        row = await self._one("SELECT EXISTS (SELECT 1 FROM goals WHERE mandate_id = %s "
                              "AND status IN ('running', 'waiting')) AS yes", (mandate_id,))
        return row["yes"]

    async def total_spend(self, mandate_id: str) -> float:
        # The spend of deleted goals still counts to the total limit.
        row = await self._one("""
            SELECT ((SELECT coalesce(sum(spend_usd), 0) FROM goals WHERE mandate_id = m.id) + m.deleted_spend_usd)::float8
                   AS total
            FROM mandates m WHERE m.id = %s
        """, (mandate_id,))
        return row["total"]

    async def create_run(self, mandate: dict, slot: datetime, max_spend_usd: float, number: int) -> dict | None:
        """The run goal for one slot, with its brief and caps. None when the slot has a run already.
        The mandate counts the goals that started, so that a number is not used again after a delete. The goal takes
        the model and the effort of the mandate's chat, and the time zone of the mandate."""
        brief = {"title": mandate["title"], "criteria": mandate["instruction"],
                 "target_count": mandate["jobs_per_run"]}
        return await self._one(f"""
            WITH run AS (
                INSERT INTO goals (title, status, max_fresh_scrapes, max_spend_usd, profile, profile_ready, brief,
                                   mandate_id, run_slot, run_number, model, effort, timezone, time_limit_s, grace_s)
                SELECT %(title)s, 'running', %(scrapes)s, %(spend)s, m.profile, m.profile_ready, %(brief)s, %(mandate)s,
                       %(slot)s, %(number)s, h.model, h.effort, m.timezone, m.time_limit_minutes * 60,
                       m.grace_minutes * 60
                FROM mandates m JOIN goals h ON h.id = m.home_goal_id WHERE m.id = %(mandate)s
                ON CONFLICT (mandate_id, run_slot) WHERE mandate_id IS NOT NULL DO NOTHING
                RETURNING {GOAL}
            ), counted AS (
                UPDATE mandates SET goals_started = greatest(goals_started, %(number)s)
                WHERE id = %(mandate)s AND EXISTS (SELECT 1 FROM run)
            )
            SELECT * FROM run
        """, {"number": number, "title": goal_title(f"{mandate['title']}: mission {number}"), "scrapes": mandate["max_scrapes_per_run"],
              "spend": max_spend_usd, "brief": Jsonb(brief),
              "mandate": mandate["id"], "slot": slot})

    async def set_run_target(self, mandate_id: str, quota: int) -> None:
        """A new quota for the run in progress. The target loop reads the brief each time."""
        await self._all("""
            UPDATE goals SET brief = jsonb_set(brief, '{target_count}', to_jsonb(%s::int)), updated_at = now()
            WHERE mandate_id = %s AND status IN ('running', 'waiting') AND brief IS NOT NULL RETURNING id
        """, (quota, mandate_id))

    async def set_run_time_limit(self, mandate_id: str, minutes: int, grace_minutes: int) -> None:
        """A new time limit for the run in progress. A run that reached its limit keeps its old limit: it stops
        new work already. Continue gives it the new limit after it stops."""
        await self._all("""
            UPDATE goals SET time_limit_s = %s, grace_s = %s, updated_at = now()
            WHERE mandate_id = %s AND status IN ('running', 'waiting') AND soft_limit_at IS NULL RETURNING id
        """, (minutes * 60, grace_minutes * 60, mandate_id))

    async def timed_runs(self) -> list[dict]:
        """The runs in progress that have a time limit."""
        return await self._all(f"SELECT {GOAL} FROM goals WHERE mandate_id IS NOT NULL "
                               "AND time_limit_s IS NOT NULL AND status IN ('running', 'waiting') ORDER BY run_slot")

    async def unannounced_runs(self) -> list[dict]:
        return await self._all(f"""
            SELECT * FROM (
                SELECT g.id::text AS id, g.status, g.run_slot, g.spend_usd::float8 AS spend_usd, g.run_announced,
                       m.home_goal_id::text AS home_goal_id, {RUN_NUMBER}
                FROM goals g JOIN mandates m ON m.id = g.mandate_id) r
            WHERE NOT run_announced AND status = ANY(%s) ORDER BY run_slot
        """, (list(SETTLED),))

    async def mark_announced(self, goal_id: str) -> None:
        await self._all("UPDATE goals SET run_announced = true WHERE id = %s RETURNING id", (goal_id,))
