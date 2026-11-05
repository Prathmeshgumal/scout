"""Every SQL query the app runs. Ids are returned as strings."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool


def goal_title(text: str, limit: int = 80) -> str:
    """The chat title of a goal: the first words of the user's message. A cut title ends with "…"."""
    words = " ".join(text.split())
    if len(words) <= limit:
        return words
    cut = words[:limit - 1]
    if " " in cut and words[limit - 1] != " ":
        cut = cut[:cut.rindex(" ")]  # do not cut a word in two
    return cut.rstrip(" ,.;:-") + "…"

GOAL = ("id::text AS id, title, status, status_reason, sdk_session_id, max_fresh_scrapes, "
        "max_spend_usd::float8 AS max_spend_usd, spend_usd::float8 AS spend_usd, sdk_cost_usd::float8 AS sdk_cost_usd, "
        "brief, agent, profile, profile_ready, mandate_id::text AS mandate_id, run_slot, model, effort, timezone, "
        "time_limit_s, grace_s, soft_limit_at, session_prompt_sha, "
        "(SELECT m.id::text FROM mandates m WHERE m.home_goal_id = goals.id) AS home_of, "
        "created_at, updated_at")

TASK = "task_id, name, description, tool_use_id"


class GoalNotFound(LookupError):
    """The goal does not exist: it was deleted."""

    def __init__(self, goal_id: str):
        super().__init__(f"Mission {goal_id} does not exist.")
        self.goal_id = goal_id


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {"seq": row["seq"], "at": row["at"].isoformat(), "type": row["type"], **row["data"]}


class Queries:
    """Runs SQL on the pool. Each store class adds its own queries."""

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    async def _one(self, query: str, params: tuple | dict = ()) -> dict | None:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchone()

    async def _all(self, query: str, params: tuple | dict = ()) -> list[dict]:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(query, params)
            return await cursor.fetchall()


class Store(Queries):
    # Goals

    async def create_goal(self, title: str, *, max_fresh_scrapes: int = 15, max_spend_usd: float = 2.0,
                          agent: str | None = None, model: str | None = None, effort: str | None = None,
                          timezone: str | None = None) -> dict:
        """A new goal. model and effort None: the default of .env. timezone: the time zone of the user, from the
        browser."""
        return await self._one(
            "INSERT INTO goals (title, status, max_fresh_scrapes, max_spend_usd, agent, model, effort, timezone) "
            f"VALUES (%s, 'running', %s, %s, %s, %s, %s, %s) RETURNING {GOAL}",
            (title, max_fresh_scrapes, max_spend_usd, agent, model, effort, timezone))

    async def set_timezone(self, goal_id: str, timezone: str) -> None:
        """The newest time zone of the user. updated_at does not change: the goal keeps its place in the list."""
        await self._one("UPDATE goals SET timezone = %s WHERE id = %s RETURNING id", (timezone, goal_id))

    async def set_model(self, goal_id: str, model: str, effort: str) -> None:
        """The model and the effort of the goal's agents, from the next session."""
        await self._one("UPDATE goals SET model = %s, effort = %s, updated_at = now() WHERE id = %s RETURNING id",
                        (model, effort, goal_id))

    async def set_profile(self, goal_id: str, profile: dict, *, ready: bool) -> None:
        """The candidate profile of the goal. `ready`: it has what discovery needs, so the other subagents can start."""
        await self._one("UPDATE goals SET profile = %s, profile_ready = %s, updated_at = now() WHERE id = %s RETURNING id",
                        (Jsonb(profile), ready, goal_id))

    async def get_goal(self, goal_id: str) -> dict | None:
        return await self._one(f"SELECT {GOAL} FROM goals WHERE id = %s", (goal_id,))

    async def list_goals(self) -> list[dict]:
        return await self._all("""
            SELECT g.id::text AS id, coalesce(g.brief->>'title', g.title) AS title, g.status, g.agent, g.created_at,
                   g.updated_at, g.spend_usd::float8 AS spend_usd, g.max_spend_usd::float8 AS max_spend_usd,
                   (SELECT count(*) FROM scrapes s
                    WHERE s.goal_id = g.id AND s.status = 'pending')::int AS pending_scrapes,
                   (SELECT count(*) FROM jobs j WHERE j.goal_id = g.id)::int AS jobs,
                   (SELECT count(*) FROM jobs j WHERE j.goal_id = g.id AND j.fit = 'fit')::int AS fit,
                   (g.brief->>'target_count')::int AS target,
                   g.mandate_id::text AS mandate_id, g.run_number,
                   (SELECT m.id::text FROM mandates m WHERE m.home_goal_id = g.id) AS home_of
            FROM goals g ORDER BY g.updated_at DESC LIMIT 200
        """)

    async def set_status(self, goal_id: str, status: str, reason: str | None = None) -> str | None:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute("SELECT status FROM goals WHERE id = %s FOR UPDATE", (goal_id,))
                row = await cursor.fetchone()
                await conn.execute(
                    "UPDATE goals SET status = %s, status_reason = %s, updated_at = now() WHERE id = %s",
                    (status, reason, goal_id))
        return row["status"] if row else None

    async def set_soft_limit(self, goal_id: str) -> bool:
        """The goal reached its time limit: from now on, no new work starts. False when it reached it before."""
        row = await self._one("UPDATE goals SET soft_limit_at = now() WHERE id = %s AND soft_limit_at IS NULL "
                              "RETURNING id", (goal_id,))
        return row is not None

    async def set_time_limit(self, goal_id: str, limit_s: int, grace_s: int) -> None:
        """A new time limit for the goal, from the start of its run time. The goal can start new work again."""
        await self._one("UPDATE goals SET time_limit_s = %s, grace_s = %s, soft_limit_at = NULL WHERE id = %s "
                        "RETURNING id", (limit_s, grace_s, goal_id))

    async def delete_preview(self, goal_ids: list[str]) -> dict:
        """What a delete of these goals removes: for the warning before it."""
        return await self._one("""
            SELECT (SELECT count(*) FROM goals WHERE id = ANY(%(ids)s::uuid[]))::int AS goals,
                   (SELECT count(*) FROM jobs WHERE goal_id = ANY(%(ids)s::uuid[]))::int AS jobs,
                   (SELECT count(*) FROM application_notes WHERE goal_id = ANY(%(ids)s::uuid[]))::int AS notes,
                   EXISTS (SELECT 1 FROM goals WHERE id = ANY(%(ids)s::uuid[])
                           AND status IN ('running', 'waiting')) AS working
        """, {"ids": goal_ids})

    async def delete_goals(self, goal_ids: list[str]) -> list[str]:
        """Delete the goals and all their data, in one transaction. Return the ids of their SDK sessions, so that
        the caller can delete the transcripts.

        The goal's rows go with ON DELETE CASCADE. The companies and facts that only these goals used go too. A
        company that an other goal has a role at stays, with its facts: that goal uses it. A deleted goal of a mandate
        adds its spend to the mandate: the spend still counts to the total limit."""
        params = {"ids": goal_ids}
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT id FROM goals WHERE id = ANY(%(ids)s::uuid[]) ORDER BY id FOR UPDATE",
                                   params)
                sessions = await (await conn.execute("""
                    SELECT sdk_session_id AS id FROM goals WHERE id = ANY(%(ids)s::uuid[]) AND sdk_session_id IS NOT NULL
                    UNION
                    SELECT unnest(past_session_ids) FROM goals WHERE id = ANY(%(ids)s::uuid[])
                """, params)).fetchall()
                companies = await (await conn.execute("""
                    SELECT company_id AS id FROM jobs WHERE goal_id = ANY(%(ids)s::uuid[]) AND company_id IS NOT NULL
                    UNION
                    SELECT company_id FROM company_research WHERE goal_id = ANY(%(ids)s::uuid[])
                """, params)).fetchall()
                await conn.execute("""
                    UPDATE mandates m SET deleted_spend_usd = m.deleted_spend_usd + d.spend
                    FROM (SELECT mandate_id, sum(spend_usd) AS spend FROM goals
                          WHERE id = ANY(%(ids)s::uuid[]) AND mandate_id IS NOT NULL GROUP BY mandate_id) d
                    WHERE m.id = d.mandate_id
                """, params)
                await conn.execute("DELETE FROM goals WHERE id = ANY(%(ids)s::uuid[])", params)
                await conn.execute("""
                    DELETE FROM companies c WHERE c.id = ANY(%s::uuid[])
                    AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.company_id = c.id)
                """, ([row["id"] for row in companies],))
        return [row["id"] for row in sessions]

    async def goals_by_prefix(self, prefix: str) -> list[str]:
        """Ids of the goals whose id starts with prefix. At most 2: enough to tell if the prefix is unique."""
        rows = await self._all("SELECT id::text AS id FROM goals WHERE id::text LIKE %s ORDER BY created_at LIMIT 2",
                               (f"{prefix}%",))
        return [row["id"] for row in rows]

    async def set_brief(self, goal_id: str, brief: dict) -> None:
        await self._one("UPDATE goals SET brief = %s, updated_at = now() WHERE id = %s RETURNING id",
                        (Jsonb(brief), goal_id))

    async def set_sdk_session(self, goal_id: str, session_id: str | None, cost_usd: float) -> None:
        """Remember the SDK session and its cumulative cost. The SDK counts on from this cost after a resume. The goal
        keeps the id of each session, so that a delete can remove the transcripts."""
        await self._one("""
            UPDATE goals SET past_session_ids = CASE
                    WHEN sdk_session_id IS NOT NULL AND sdk_session_id IS DISTINCT FROM %(new)s
                         AND NOT sdk_session_id = ANY(past_session_ids)
                    THEN array_append(past_session_ids, sdk_session_id) ELSE past_session_ids END,
                sdk_session_id = %(new)s, sdk_cost_usd = %(cost)s
            WHERE id = %(goal)s RETURNING id
        """, {"new": session_id, "cost": cost_usd, "goal": goal_id})

    async def set_session_prompt(self, goal_id: str, sha: str) -> None:
        """The system prompt of the new SDK session: a resumed session keeps its first prompt."""
        await self._one("UPDATE goals SET session_prompt_sha = %s WHERE id = %s RETURNING id", (sha, goal_id))

    async def goals_with_status(self, *statuses: str) -> list[dict]:
        return await self._all(f"SELECT {GOAL} FROM goals WHERE status = ANY(%s) ORDER BY updated_at",
                               (list(statuses),))

    async def usage(self, goal_id: str) -> dict:
        return await self._one("""
            SELECT (SELECT count(*) FROM scrapes WHERE goal_id = %(g)s AND counts_to_cap)::int AS fresh_scrapes,
                   (SELECT spend_usd FROM goals WHERE id = %(g)s)::float8 AS spend_usd
        """, {"g": goal_id})

    async def add_spend(self, goal_id: str, usd: float) -> float:
        """Add spend to the goal and return the new total. A negative amount adds nothing."""
        row = await self._one("UPDATE goals SET spend_usd = spend_usd + %s WHERE id = %s "
                              "RETURNING spend_usd::float8 AS spend_usd", (max(usd, 0.0), goal_id))
        return row["spend_usd"]

    async def count_jobs(self, goal_id: str) -> int:
        row = await self._one("SELECT count(*)::int AS n FROM jobs WHERE goal_id = %s", (goal_id,))
        return row["n"]

    # Subagent tasks: the lifecycle of the subagent runs, by the SDK's task id (the agentId).

    async def start_task(self, goal_id: str, task_id: str, name: str, description: str,
                         tool_use_id: str | None) -> None:
        """A subagent task started (TaskStartedMessage). A start of a known task is a resume. A resume keeps the first
        name and the original Agent call id."""
        await self._one("""
            INSERT INTO subagent_tasks (task_id, goal_id, name, description, tool_use_id, status)
            VALUES (%s, %s, %s, %s, %s, 'running')
            ON CONFLICT (task_id) DO UPDATE SET status = 'running', ended_at = NULL,
                name = CASE WHEN subagent_tasks.name = '' THEN EXCLUDED.name ELSE subagent_tasks.name END
            RETURNING task_id
        """, (task_id, goal_id, name or "", description or "", tool_use_id))

    async def finish_task(self, task_id: str, status: str, summary: str | None) -> bool:
        """Set a terminal status. The first terminal message sets the status; a later one can add the summary. True when
        the status changed."""
        row = await self._one("""
            WITH old AS (SELECT status FROM subagent_tasks WHERE task_id = %(task)s)
            UPDATE subagent_tasks t SET
                status = CASE WHEN old.status IN ('running', 'interrupted') THEN %(status)s ELSE t.status END,
                summary = coalesce(%(summary)s, t.summary), ended_at = coalesce(t.ended_at, now())
            FROM old WHERE t.task_id = %(task)s
            RETURNING old.status IN ('running', 'interrupted') AS changed
        """, {"status": status, "summary": summary, "task": task_id})
        return bool(row and row["changed"])

    async def list_tasks(self, goal_id: str, *statuses: str) -> list[dict]:
        where = "AND status = ANY(%(statuses)s)" if statuses else ""
        return await self._all(f"""
            SELECT {TASK}, status, summary, started_at, ended_at FROM subagent_tasks
            WHERE goal_id = %(goal)s {where} ORDER BY started_at
        """, {"goal": goal_id, "statuses": list(statuses)})

    async def task_name(self, goal_id: str, task_id: str) -> str | None:
        row = await self._one("SELECT name FROM subagent_tasks WHERE goal_id = %s AND task_id = %s", (goal_id, task_id))
        return row["name"] if row and row["name"] else None

    async def interrupt_tasks(self, goal_id: str) -> list[dict]:
        """Mark the goal's running subagent tasks interrupted, so that the main agent can resume them later."""
        return await self._all(f"""
            UPDATE subagent_tasks SET status = 'interrupted' WHERE goal_id = %s AND status = 'running' RETURNING {TASK}
        """, (goal_id,))

    # Events

    async def append_event(self, goal_id: str, type_: str, data: dict, detail: dict | None = None) -> dict:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cursor = await conn.execute(
                    "UPDATE goals SET last_event_seq = last_event_seq + 1, updated_at = now() "
                    "WHERE id = %s RETURNING last_event_seq", (goal_id,))
                row = await cursor.fetchone()
                if row is None:
                    raise GoalNotFound(goal_id)
                seq = row["last_event_seq"]
                cursor = await conn.execute(
                    "INSERT INTO goal_events (goal_id, seq, type, data, detail) VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING seq, at, type, data",
                    (goal_id, seq, type_, Jsonb(data), Jsonb(detail) if detail is not None else None))
                return _event(await cursor.fetchone())

    async def agent_web_calls(self, goal_id: str, agent: str) -> list[dict]:
        """The searches and page reads of one subagent in this goal, newest first, with their results."""
        return await self._all("""
            SELECT s.data->>'tool' AS tool, s.at, s.detail->'input' AS input,
                   (f.data->>'ok')::boolean AS ok, f.data->>'summary' AS summary
            FROM goal_events s
            LEFT JOIN goal_events f ON f.goal_id = s.goal_id AND f.type = 'tool_finished'
                 AND f.data->>'tool_use_id' = s.data->>'tool_use_id'
            WHERE s.goal_id = %s AND s.type = 'tool_started' AND s.data->>'agent' = %s
              AND s.data->>'tool' IN ('google_search', 'read_page')
            ORDER BY s.seq DESC
        """, (goal_id, agent))

    async def step_detail(self, goal_id: str, tool_use_id: str) -> dict | None:
        """The tool, input and output of one step. None when the goal has no such step."""
        rows = await self._all("""
            SELECT type, data->>'tool' AS tool, detail FROM goal_events
            WHERE goal_id = %s AND type IN ('tool_started', 'tool_finished') AND data->>'tool_use_id' = %s
            ORDER BY seq
        """, (goal_id, tool_use_id))
        started = next((row for row in rows if row["type"] == "tool_started"), None)
        if started is None:
            return None
        finished = next((row for row in rows if row["type"] == "tool_finished"), None)
        given, got = started["detail"] or {}, (finished or {}).get("detail") or {}
        return {"tool": started["tool"], "input": given.get("input"), "output": got.get("output"),
                "truncated": bool(given.get("truncated") or got.get("truncated"))}

    async def list_events(self, goal_id: str, after: int = 0) -> list[dict]:
        rows = await self._all(
            "SELECT seq, at, type, data FROM goal_events WHERE goal_id = %s AND seq > %s ORDER BY seq",
            (goal_id, after))
        return [_event(row) for row in rows]

    async def status_events(self, goal_id: str) -> list[dict]:
        """The status changes of the goal, oldest first: for its run time."""
        rows = await self._all("SELECT seq, at, type, data FROM goal_events WHERE goal_id = %s "
                               "AND type = 'status_changed' ORDER BY seq", (goal_id,))
        return [_event(row) for row in rows]

    async def first_user_message(self, goal_id: str) -> str | None:
        row = await self._one(
            "SELECT data->>'text' AS text FROM goal_events WHERE goal_id = %s AND type = 'user_message' "
            "ORDER BY seq LIMIT 1", (goal_id,))
        return row["text"] if row else None

    async def last_user_message_seq(self, goal_id: str) -> int:
        """The seq of the newest message from the user in the chat of the goal. 0 when there is none."""
        row = await self._one(
            "SELECT coalesce(max(seq), 0) AS seq FROM goal_events WHERE goal_id = %s AND type = 'user_message'",
            (goal_id,))
        return row["seq"]

    async def add_scrape(self, goal_id: str, request_id: str, kind: str, target: str, counts_to_cap: bool,
                         job_id: str | None = None, *, wants_jobs: bool = False) -> dict:
        """wants_jobs: the scrape asks for the jobs section. So when it ends, the watcher records what it read."""
        return await self._one(
            "INSERT INTO scrapes (goal_id, request_id, kind, target, counts_to_cap, job_id, wants_jobs) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id::text AS id, request_id, kind, target",
            (goal_id, request_id, kind, target, counts_to_cap, job_id, wants_jobs))

    async def has_job(self, goal_id: str, job_id: str) -> bool:
        return await self._one("SELECT id FROM jobs WHERE goal_id = %s AND id = %s", (goal_id, job_id)) is not None

    async def set_scrape_agent(self, goal_id: str, request_ids: list[str], agent_id: str) -> None:
        await self._all(
            "UPDATE scrapes SET agent_id = %s WHERE goal_id = %s AND request_id = ANY(%s) RETURNING id",
            (agent_id, goal_id, request_ids))

    async def pending_scrapes(self) -> list[dict]:
        return await self._all("""
            SELECT id::text AS id, goal_id::text AS goal_id, request_id, kind, target,
                   last_status, wait_reason, next_attempt_at, job_id::text AS job_id, wants_jobs
            FROM scrapes WHERE status = 'pending' ORDER BY submitted_at
        """)

    async def count_pending(self, goal_id: str) -> int:
        row = await self._one(
            "SELECT count(*)::int AS n FROM scrapes WHERE goal_id = %s AND status = 'pending'", (goal_id,))
        return row["n"]

    async def last_continue_fit(self, goal_id: str) -> int | None:
        """The number of fit jobs at the last "continue" notice since the user's last message, or None."""
        row = await self._one("""
            SELECT (data->>'fit')::int AS fit FROM goal_events
            WHERE goal_id = %(goal)s AND type = 'notice' AND data->>'trigger' = 'continue'
              AND seq > coalesce((SELECT max(seq) FROM goal_events
                                  WHERE goal_id = %(goal)s AND type = 'user_message'), 0)
            ORDER BY seq DESC LIMIT 1
        """, {"goal": goal_id})
        return row["fit"] if row else None

    async def finished_since_user(self, goal_id: str) -> bool:
        """True when the main agent called finish_goal after the user's last message."""
        row = await self._one("""
            SELECT 1 AS ok FROM goal_events
            WHERE goal_id = %(goal)s AND type = 'tool_finished' AND data->>'tool' = 'finish_goal'
              AND (data->>'ok')::boolean
              AND seq > coalesce((SELECT max(seq) FROM goal_events
                                  WHERE goal_id = %(goal)s AND type = 'user_message'), 0)
            LIMIT 1
        """, {"goal": goal_id})
        return row is not None

    async def count_unreported(self, goal_id: str) -> int:
        row = await self._one(
            "SELECT count(*)::int AS n FROM scrapes WHERE goal_id = %s AND status <> 'pending' AND NOT reported",
            (goal_id,))
        return row["n"]

    async def update_scrape_progress(self, scrape_id: str, last_status: str | None, wait_reason: str | None,
                                     next_attempt_at: datetime | None) -> None:
        await self._one(
            "UPDATE scrapes SET last_status = %s, wait_reason = %s, next_attempt_at = %s, last_checked_at = now() "
            "WHERE id = %s RETURNING id", (last_status, wait_reason, next_attempt_at, scrape_id))

    async def finish_scrape(self, scrape_id: str, status: str, failure_kind: str | None) -> None:
        await self._one(
            "UPDATE scrapes SET status = %s, failure_kind = %s, last_checked_at = now(), finished_at = now() "
            "WHERE id = %s RETURNING id", (status, failure_kind, scrape_id))

    async def goals_ready_to_resume(self) -> list[str]:
        """Waiting goals with finished main-thread scrapes and none pending. Subagent scrapes go by agent."""
        rows = await self._all("""
            SELECT g.id::text AS id FROM goals g
            WHERE g.status = 'waiting'
              AND NOT EXISTS (SELECT 1 FROM scrapes s WHERE s.goal_id = g.id AND s.status = 'pending'
                              AND s.agent_id IS NULL)
              AND EXISTS (SELECT 1 FROM scrapes s WHERE s.goal_id = g.id AND s.status <> 'pending'
                          AND NOT s.reported AND s.agent_id IS NULL)
            ORDER BY g.updated_at
        """)
        return [row["id"] for row in rows]

    async def take_unreported(self, goal_id: str) -> list[dict]:
        return await self._all("""
            UPDATE scrapes SET reported = true
            WHERE goal_id = %s AND status <> 'pending' AND NOT reported AND agent_id IS NULL
            RETURNING request_id, kind, target, status, failure_kind
        """, (goal_id,))

    async def agents_ready_to_notify(self) -> list[dict]:
        """Subagents whose scrapes are all finished, with some not yet reported, in goals that still work."""
        return await self._all("""
            SELECT s.goal_id::text AS goal_id, s.agent_id, max(t.name) AS name
            FROM scrapes s
            JOIN goals g ON g.id = s.goal_id
            LEFT JOIN subagent_tasks t ON t.task_id = s.agent_id
            WHERE s.agent_id IS NOT NULL AND g.status IN ('running', 'waiting')
            GROUP BY s.goal_id, s.agent_id
            HAVING bool_or(s.status <> 'pending' AND NOT s.reported) AND NOT bool_or(s.status = 'pending')
            ORDER BY min(s.submitted_at)
        """)

    async def take_agent_unreported(self, goal_id: str, agent_id: str) -> list[dict]:
        return await self._all("""
            UPDATE scrapes SET reported = true
            WHERE goal_id = %s AND agent_id = %s AND status <> 'pending' AND NOT reported
            RETURNING request_id, kind, target, status, failure_kind
        """, (goal_id, agent_id))

    async def scrapes_by_request(self, goal_id: str, request_ids: list[str]) -> list[dict]:
        return await self._all(
            "SELECT request_id, kind, target, status, failure_kind, wait_reason FROM scrapes "
            "WHERE goal_id = %s AND request_id = ANY(%s) ORDER BY submitted_at", (goal_id, request_ids))

    async def mark_reported(self, goal_id: str, request_ids: list[str]) -> None:
        await self._all(
            "UPDATE scrapes SET reported = true WHERE goal_id = %s AND request_id = ANY(%s) "
            "AND status <> 'pending' RETURNING id", (goal_id, request_ids))

    async def list_scrapes(self, goal_id: str) -> list[dict]:
        return await self._all(
            "SELECT request_id, kind, target, status, failure_kind FROM scrapes WHERE goal_id = %s "
            "ORDER BY submitted_at", (goal_id,))

    async def cancel_pending_scrapes(self, goal_id: str) -> list[dict]:
        # Marked reported, since the work stopped. The caller cancels them in career-lens.
        return await self._all("""
            UPDATE scrapes SET status = 'cancelled', reported = true, finished_at = now()
            WHERE goal_id = %s AND status = 'pending' RETURNING request_id, target, wants_jobs
        """, (goal_id,))

    async def waits(self, goal_id: str) -> list[dict]:
        """The pending scrapes of the goal, with the reason that each one waits."""
        return await self._all("""
            SELECT request_id, kind, target, last_status, wait_reason, next_attempt_at, submitted_at FROM scrapes
            WHERE goal_id = %s AND status = 'pending' ORDER BY submitted_at
        """, (goal_id,))

    async def pending_request_ids(self, goal_ids: list[str]) -> list[str]:
        rows = await self._all("SELECT DISTINCT request_id FROM scrapes WHERE goal_id = ANY(%s::uuid[]) "
                               "AND status = 'pending'", (goal_ids,))
        return [row["request_id"] for row in rows]

    async def unwanted(self, request_ids: list[str]) -> list[str]:
        """The requests that no pending scrape waits on. All goals use one career-lens key, so career-lens sees them
        as one caller: a goal cancels a request only when no other goal waits on it."""
        rows = await self._all("SELECT DISTINCT request_id FROM scrapes WHERE status = 'pending' "
                               "AND request_id = ANY(%s)", (list(request_ids),))
        wanted = {row["request_id"] for row in rows}
        return [i for i in dict.fromkeys(request_ids) if i not in wanted]

    async def expire_overdue(self, max_age: timedelta) -> list[dict]:
        """Give up on every pending scrape of a waiting goal whose oldest pending scrape is older than max_age."""
        return await self._all("""
            UPDATE scrapes s SET status = 'expired', finished_at = now()
            FROM goals g
            WHERE s.goal_id = g.id AND g.status = 'waiting' AND s.status = 'pending'
              AND g.id IN (SELECT goal_id FROM scrapes WHERE status = 'pending' AND submitted_at < now() - %s)
            RETURNING s.goal_id::text AS goal_id, s.request_id, s.target, s.wants_jobs
        """, (max_age,))
