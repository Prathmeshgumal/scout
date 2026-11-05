"""The numbers of the metrics page: cost, time, results, scrapes, failures, refused pages and evals.

It counts the goals that a person started: normal goals, mandate runs and mandate chats. It leaves out subagent goals
(`agent` is set), which `job-finder agent` and the evals make."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.rows import dict_row

from .evals.store import EvalStore

MAX_DAYS = 365

# The goals in the period. %(days)s and %(tz)s are parameters of each query.
GOALS = """
    SELECT id, status, model, spend_usd, created_at, mandate_id FROM goals
    WHERE agent IS NULL AND created_at >= now() - make_interval(days => %(days)s)
"""


async def _all(pool, query: str, params: dict) -> list[dict]:
    async with pool.connection() as conn:
        cursor = conn.cursor(row_factory=dict_row)
        await cursor.execute(query, params)
        return await cursor.fetchall()


async def _one(pool, query: str, params: dict) -> dict:
    rows = await _all(pool, query, params)
    return rows[0] if rows else {}


def _num(value) -> float | None:
    return None if value is None else round(float(value), 4)


async def _zone(pool, tz: str):
    """The zone for Postgres: its name when Postgres knows it, else its UTC offset now. Postgres does not know some
    names that browsers give, such as Asia/Calcutta."""
    if await _one(pool, "SELECT 1 AS known FROM pg_timezone_names WHERE name = %(tz)s", {"tz": tz}):
        return tz
    try:
        return datetime.now(ZoneInfo(tz)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"


async def metrics(pool, days: int = 30, tz: str = "UTC") -> dict:
    days = max(1, min(int(days), MAX_DAYS))
    p = {"days": days, "tz": await _zone(pool, tz)}
    totals = await _one(pool, f"""
        WITH g AS ({GOALS})
        SELECT count(*) AS goals,
               count(*) FILTER (WHERE mandate_id IS NOT NULL) AS mandate_goals,
               coalesce(sum(spend_usd), 0) AS spend_usd,
               (SELECT count(*) FROM jobs j JOIN g ON g.id = j.goal_id) AS roles,
               (SELECT count(*) FROM jobs j JOIN g ON g.id = j.goal_id WHERE j.fit = 'fit') AS fit_roles,
               count(*) FILTER (WHERE status = 'done') AS done,
               count(*) FILTER (WHERE status = 'stopped') AS stopped,
               count(*) FILTER (WHERE status IN ('failed', 'capped')) AS failed,
               count(*) FILTER (WHERE status IN ('running', 'waiting')) AS running
        FROM g
    """, p)
    spend, fit = float(totals.get("spend_usd") or 0), int(totals.get("fit_roles") or 0)

    # Busy time: the time in the status running. A goal starts as running, so its creation opens the first period.
    time = await _one(pool, f"""
        WITH g AS ({GOALS} AND status NOT IN ('running', 'waiting')),
        changes AS (
            SELECT g.id AS goal_id, g.created_at AS at, 'running' AS status, -1 AS seq FROM g
            UNION ALL
            SELECT e.goal_id, e.at, e.data->>'to', e.seq FROM goal_events e JOIN g ON g.id = e.goal_id
            WHERE e.type = 'status_changed'
        ),
        periods AS (
            SELECT goal_id, status, lead(at) OVER (PARTITION BY goal_id ORDER BY at, seq) - at AS length FROM changes
        ),
        busy AS (
            SELECT goal_id, extract(epoch FROM sum(length) FILTER (WHERE status = 'running')) AS seconds
            FROM periods GROUP BY goal_id
        )
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) AS median_s,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY seconds) AS p90_s, count(seconds) AS goals
        FROM busy WHERE seconds IS NOT NULL
    """, p)

    daily = await _all(pool, f"""
        WITH g AS ({GOALS})
        SELECT to_char(date_trunc('day', g.created_at AT TIME ZONE %(tz)s), 'YYYY-MM-DD') AS day,
               count(*) AS goals, coalesce(sum(g.spend_usd), 0)::float AS spend_usd,
               coalesce(sum((SELECT count(*) FROM jobs j WHERE j.goal_id = g.id AND j.fit = 'fit')), 0)::int AS fit_roles
        FROM g GROUP BY 1 ORDER BY 1
    """, p)

    verdicts = await _all(pool, f"""
        WITH g AS ({GOALS})
        SELECT coalesce(g.model, 'default') AS model, coalesce(j.fit, 'none') AS verdict, count(*)::int AS roles
        FROM jobs j JOIN g ON g.id = j.goal_id GROUP BY 1, 2 ORDER BY 1, 2
    """, p)
    by_model = await _all(pool, f"""
        WITH g AS ({GOALS})
        SELECT coalesce(g.model, 'default') AS model, count(*)::int AS goals,
               coalesce(sum(g.spend_usd), 0)::float AS spend_usd,
               coalesce(sum((SELECT count(*) FROM jobs j WHERE j.goal_id = g.id AND j.fit = 'fit')), 0)::int AS fit_roles
        FROM g GROUP BY 1 ORDER BY 3 DESC
    """, p)

    scrapes = await _all(pool, f"""
        WITH g AS ({GOALS})
        SELECT s.kind, count(*)::int AS total,
               count(*) FILTER (WHERE s.status = 'succeeded')::int AS succeeded,
               count(*) FILTER (WHERE s.status = 'failed')::int AS failed,
               count(*) FILTER (WHERE s.status = 'cancelled')::int AS cancelled,
               count(*) FILTER (WHERE s.status = 'pending')::int AS pending,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM s.finished_at - s.submitted_at))
                   FILTER (WHERE s.status = 'succeeded') AS median_wait_s,
               percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(epoch FROM s.finished_at - s.submitted_at))
                   FILTER (WHERE s.status = 'succeeded') AS p90_wait_s
        FROM scrapes s JOIN g ON g.id = s.goal_id GROUP BY s.kind ORDER BY 2 DESC
    """, p)
    failure_kinds = await _all(pool, f"""
        WITH g AS ({GOALS}),
        kinds AS (
            SELECT s.failure_kind AS kind, 'scrape' AS source FROM scrapes s JOIN g ON g.id = s.goal_id
            WHERE s.failure_kind IS NOT NULL
            UNION ALL
            SELECT e.data->>'failure_kind', 'tool' FROM goal_events e JOIN g ON g.id = e.goal_id
            WHERE e.type = 'tool_finished' AND e.data->>'failure_kind' IS NOT NULL
        )
        SELECT kind, source, count(*)::int AS count FROM kinds GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 20
    """, p)
    tools = await _all(pool, f"""
        WITH g AS ({GOALS})
        SELECT e.data->>'tool' AS tool, count(*)::int AS calls,
               count(*) FILTER (WHERE (e.data->>'ok')::boolean IS FALSE)::int AS failed
        FROM goal_events e JOIN g ON g.id = e.goal_id
        WHERE e.type = 'tool_finished' AND e.data->>'tool' IS NOT NULL
        GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT 20
    """, p)
    agents = await _all(pool, f"""
        WITH g AS ({GOALS}),
        started AS (SELECT e.goal_id, e.data->>'tool_use_id' AS id, e.data->>'name' AS agent, e.at FROM goal_events e
                    JOIN g ON g.id = e.goal_id WHERE e.type = 'agent_started'),
        finished AS (SELECT e.goal_id, e.data->>'tool_use_id' AS id, (e.data->>'ok')::boolean AS ok, e.at
                     FROM goal_events e JOIN g ON g.id = e.goal_id WHERE e.type = 'agent_finished')
        SELECT s.agent, count(*)::int AS runs, count(*) FILTER (WHERE f.ok IS FALSE)::int AS failed,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM f.at - s.at)) AS median_s
        FROM started s LEFT JOIN finished f ON f.goal_id = s.goal_id AND f.id = s.id
        WHERE s.agent IS NOT NULL GROUP BY 1 ORDER BY 2 DESC
    """, p)
    refused = await _one(pool, f"""
        WITH g AS ({GOALS})
        SELECT count(*) FILTER (WHERE e.data->>'tool' = 'read_page')::int AS pages,
               coalesce(sum((e.data->>'count')::int) FILTER (WHERE e.data->>'tool' = 'google_search'), 0)::int
                   AS search_results,
               count(DISTINCT e.goal_id)::int AS goals
        FROM goal_events e JOIN g ON g.id = e.goal_id WHERE e.type = 'site_blocked'
    """, p)

    runs = await EvalStore(pool).recent(100)
    latest: dict[str, dict] = {}
    for run in runs:  # newest first
        latest.setdefault(run["suite"], run)

    return {
        "days": days,
        "totals": {
            "goals": int(totals.get("goals") or 0), "mandate_goals": int(totals.get("mandate_goals") or 0),
            "spend_usd": _num(spend), "roles": int(totals.get("roles") or 0), "fit_roles": fit,
            "cost_per_fit_usd": _num(spend / fit) if fit else None,
            "done": int(totals.get("done") or 0), "stopped": int(totals.get("stopped") or 0),
            "failed": int(totals.get("failed") or 0), "running": int(totals.get("running") or 0),
            "median_busy_s": _num(time.get("median_s")), "p90_busy_s": _num(time.get("p90_s")),
        },
        "daily": daily,
        "by_model": by_model,
        "verdicts": verdicts,
        "scrapes": [{**row, "median_wait_s": _num(row["median_wait_s"]), "p90_wait_s": _num(row["p90_wait_s"])}
                    for row in scrapes],
        "failure_kinds": failure_kinds,
        "tools": tools,
        "agents": [{**row, "median_s": _num(row["median_s"])} for row in agents],
        "refused": refused or {"pages": 0, "search_results": 0, "goals": 0},
        "evals": {"latest": list(latest.values()), "runs": runs},
    }
