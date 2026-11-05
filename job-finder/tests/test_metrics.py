import pytest

from job_finder.evals.store import EvalRun, EvalStore
from job_finder.metrics import metrics


async def sql(pool, query, params=()):
    async with pool.connection() as conn:
        await conn.execute(query, params)


async def event(store, pool, goal_id, type_, minutes, **data):
    """An event at `minutes` after the creation of the goal."""
    row = await store.append_event(goal_id, type_, data, None)
    await sql(pool, "UPDATE goal_events SET at = (SELECT created_at FROM goals WHERE id = %s) + make_interval(mins => %s) "
                    "WHERE goal_id = %s AND seq = %s", (goal_id, minutes, goal_id, row["seq"]))


async def finished_goal(store, pool, artifacts, *, model, spend, fits, busy_minutes):
    goal = await store.create_goal("g", model=model)
    await store.add_spend(goal["id"], spend)
    jobs = await artifacts.save_jobs(goal["id"], [{"url": f"https://jobs.example.com/{goal['id']}/{i}", "title": "Dev",
                                                   "company_name": f"Co {i}"} for i in range(fits + 1)], "job-discovery")
    for row in jobs[:fits]:
        await artifacts.save_job_research(goal["id"], row["job_id"], None, "snippet", None)
        await artifacts.set_fit(goal["id"], row["job_id"], "fit", "Because.", [])
    await event(store, pool, goal["id"], "status_changed", busy_minutes, **{"from": "running", "to": "done"})
    await store.set_status(goal["id"], "done")
    return goal["id"]


async def test_metrics_counts_cost_time_results_and_failures(store, pool, artifacts):
    a = await finished_goal(store, pool, artifacts, model="claude-sonnet-5-5", spend=1.0, fits=2, busy_minutes=10)
    b = await finished_goal(store, pool, artifacts, model="claude-opus-5-5", spend=2.0, fits=1, busy_minutes=30)
    # A goal that waited 20 minutes for scrapes in the middle: only its running time counts.
    await event(store, pool, b, "status_changed", 35, **{"from": "done", "to": "waiting"})
    await event(store, pool, b, "status_changed", 55, **{"from": "waiting", "to": "running"})
    await event(store, pool, b, "status_changed", 60, **{"from": "running", "to": "done"})
    # A subagent goal of the evals does not count.
    dev = await store.create_goal("Eval: fit-check", agent="fit-check")
    await store.add_spend(dev["id"], 5.0)

    await sql(pool, "INSERT INTO scrapes (goal_id, request_id, kind, target, counts_to_cap, status, submitted_at, finished_at, "
                    "failure_kind) VALUES (%s, 'r1', 'google_search', 'q', false, 'succeeded', now() - interval '10 min', now(), "
                    "NULL), (%s, 'r2', 'google_search', 'q', false, 'failed', now(), now(), 'blocked')", (a, a))
    await event(store, pool, a, "tool_finished", 1, tool="read_page", ok=False, failure_kind="not_found")
    await event(store, pool, a, "tool_finished", 2, tool="read_page", ok=True)
    await event(store, pool, a, "site_blocked", 3, tool="read_page", url="http://127.0.0.1/", reason="x")
    await event(store, pool, a, "site_blocked", 4, tool="google_search", query="q", count=3)
    await event(store, pool, a, "agent_started", 1, name="fit-check", tool_use_id="t1", agent="main", task="go")
    await event(store, pool, a, "agent_finished", 5, name="fit-check", tool_use_id="t1", ok=True, agent="main")
    await EvalStore(pool).save(EvalRun("fit-check", "m", "sha1", 32, 30, 30 / 32))
    await EvalStore(pool).save(EvalRun("fit-check", "m", "sha2", 32, 31, 31 / 32))

    m = await metrics(pool, days=30, tz="Asia/Kolkata")
    t = m["totals"]
    assert t["goals"] == 2 and t["spend_usd"] == 3.0 and t["fit_roles"] == 3 and t["roles"] == 5
    assert t["cost_per_fit_usd"] == 1.0 and t["done"] == 2
    assert t["median_busy_s"] == pytest.approx(22.5 * 60)  # the median of 10 minutes and 30 + 5 minutes
    assert {row["model"]: row["fit_roles"] for row in m["by_model"]} == {"claude-sonnet-5-5": 2, "claude-opus-5-5": 1}
    assert sum(row["roles"] for row in m["verdicts"] if row["verdict"] == "fit") == 3
    [google] = m["scrapes"]
    assert google["total"] == 2 and google["failed"] == 1 and google["median_wait_s"] == pytest.approx(600, abs=5)
    assert {(row["kind"], row["source"]) for row in m["failure_kinds"]} == {("blocked", "scrape"), ("not_found", "tool")}
    assert m["tools"] == [{"tool": "read_page", "calls": 2, "failed": 1}]
    assert m["agents"] == [{"agent": "fit-check", "runs": 1, "failed": 0, "median_s": 240.0}]
    assert m["refused"] == {"pages": 1, "search_results": 3, "goals": 1}
    assert [run["prompt_sha"] for run in m["evals"]["latest"]] == ["sha2"] and len(m["evals"]["runs"]) == 2
    assert sum(day["goals"] for day in m["daily"]) == 2


async def test_metrics_with_no_goals(pool):
    m = await metrics(pool, days=7)
    assert m["totals"]["goals"] == 0 and m["totals"]["cost_per_fit_usd"] is None and m["totals"]["median_busy_s"] is None
    assert m["daily"] == [] and m["evals"] == {"latest": [], "runs": []}


async def test_old_goals_are_out_of_the_period(store, pool):
    goal = await store.create_goal("old")
    await sql(pool, "UPDATE goals SET created_at = now() - interval '40 days' WHERE id = %s", (goal["id"],))
    assert (await metrics(pool, days=30))["totals"]["goals"] == 0
    assert (await metrics(pool, days=9999))["days"] == 365


async def test_the_metrics_api_needs_a_sign_in_and_reads_the_period(app_settings, store, hub, recorder):
    import httpx

    from job_finder.web.app import create_app
    from job_finder.web.sessions import GoalSessions

    from .conftest import signed_in_client
    from .fakes import Streams, make_services

    services = make_services(app_settings, store, hub, recorder, Streams())
    app = create_app(app_settings, services, GoalSessions(services, idle_s=600), model_loader=lambda: [])
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as anonymous:
        assert (await anonymous.get("/api/metrics")).status_code == 401
    await store.create_goal("g")
    async with signed_in_client(app) as client:
        body = (await client.get("/api/metrics?days=7&tz=Not/AZone")).json()
    assert body["days"] == 7 and body["totals"]["goals"] == 1


async def test_a_zone_name_that_postgres_does_not_know_uses_its_offset(store, pool):
    await store.create_goal("g")
    for tz in ("Asia/Calcutta", "Asia/Kolkata", "Not/AZone"):
        m = await metrics(pool, days=7, tz=tz)
        assert sum(day["goals"] for day in m["daily"]) == 1, tz
