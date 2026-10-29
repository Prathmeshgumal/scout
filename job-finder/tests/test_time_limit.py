"""The time limit of a mandate goal: the soft limit stops new work, the hard limit stops all work."""

from datetime import UTC, datetime, timedelta

import pytest

from job_finder.career_lens import CareerLens
from job_finder.mandate_store import MandateStore
from job_finder.session import GoalSession
from job_finder.time_limit import (
    TIME_LIMIT_PROMPT,
    TIME_REASON,
    check_time_limits,
    limit_phase,
    run_seconds,
    time_limit_hook,
)

from .fakes import Cancels, Streams, agent_call, assistant, fit_roles, result, task_started


@pytest.fixture
def streams():
    return Streams()


async def worked(pool, goal_id, minutes):
    """The goal started `minutes` ago and has worked since then."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE goals SET created_at = now() - make_interval(mins => %s) WHERE id = %s",
                           (minutes, goal_id))


async def opened(app_settings, store, recorder, streams, goal_id, career_lens=None):
    session = GoalSession(app_settings, store, recorder, career_lens or CareerLens("", ""), goal_id,
                          client_factory=streams)
    await session.open(recover=False)
    return session


async def notices(store, goal_id):
    return [e["text"] for e in await store.list_events(goal_id) if e["type"] == "notice"]


# The phases

def test_the_phase_comes_from_the_run_time():
    goal = {"time_limit_s": 7200, "grace_s": 1800, "soft_limit_at": None}
    assert limit_phase(goal, 7199) is None
    assert limit_phase(goal, 7200) == "soft"
    assert limit_phase({**goal, "soft_limit_at": datetime.now(UTC)}, 7300) is None  # the soft step is done
    assert limit_phase(goal, 9000) == "hard"
    assert limit_phase({**goal, "grace_s": 0}, 7200) == "hard"
    assert limit_phase({**goal, "time_limit_s": None}, 99999) is None  # a goal that the user started


async def test_run_time_leaves_out_the_time_after_an_end(store, recorder, pool):
    goal = await store.create_goal("g")
    await worked(pool, goal["id"], 60)
    assert 3590 <= await run_seconds(store, await store.get_goal(goal["id"])) <= 3610
    await recorder.record(goal["id"], "status_changed", **{"from": "running", "to": "capped"})
    async with pool.connection() as conn:  # the end was 40 minutes ago
        await conn.execute("UPDATE goal_events SET at = now() - interval '40 minutes' WHERE goal_id = %s",
                           (goal["id"],))
    assert 1190 <= await run_seconds(store, await store.get_goal(goal["id"])) <= 1210


# The hook

async def test_after_the_soft_limit_no_new_subagent_and_no_new_web_work_starts(store, make_run):
    _, run = await make_run()
    hook = time_limit_hook(store, run["id"])

    def call(tool, agent_id=None):
        return {"tool_name": tool, "tool_input": {}, **({"agent_id": agent_id} if agent_id else {})}

    tools = [call("Agent"), call("Task"), call("mcp__career_lens__scrape_company", "a1"),
             call("mcp__career_lens__scrape_linkedin_job", "a1"), call("mcp__career_lens__google_search", "a1"),
             call("mcp__career_lens__read_page", "a1")]
    for tool in tools:
        assert await hook(tool, "t1", {}) == {}
    await store.set_soft_limit(run["id"])
    for tool in tools:
        out = await hook(tool, "t1", {})
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", tool
        assert "time limit" in out["hookSpecificOutput"]["permissionDecisionReason"]


async def test_the_hook_is_on_the_runs_of_a_mandate_only(store, make_run):
    from job_finder.options import build_hooks
    from job_finder.time_limit import TIME_MATCHER
    _, run = await make_run()
    matchers = [m.matcher for m in build_hooks(run, store, None, closed=False)["PreToolUse"]]
    assert TIME_MATCHER in matchers
    goal = await store.create_goal("g")
    assert TIME_MATCHER not in [m.matcher for m in build_hooks(goal, store, None, closed=False)["PreToolUse"]]
    assert "SendMessage" not in TIME_MATCHER  # a subagent with finished scrapes can still finish its task
    assert "get_company" not in TIME_MATCHER and "wait_for_scrapes" not in TIME_MATCHER


# The soft limit

async def test_the_soft_limit_tells_the_main_agent_once_and_lets_the_subagents_finish(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run()
    session = await opened(app_settings, store, recorder, streams, run["id"])
    await session.send("go", "mandate_run", user=False)
    streams.last.push(agent_call("call-1", "job-gen"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    await worked(pool, run["id"], 121)

    await session.reach_time_limit(hard=False)
    await session.reach_time_limit(hard=False)  # the next check does not tell it again
    goal = await store.get_goal(run["id"])
    assert goal["soft_limit_at"] is not None and goal["status"] == "running"
    assert session.tasks and streams.last.stopped == []  # job-gen goes on
    assert sum(TIME_LIMIT_PROMPT in q for q in streams.last.queries) == 1
    assert sum("Time limit reached (2h)" in text for text in await notices(store, run["id"])) == 1
    await session.close()


async def test_after_the_soft_limit_the_goal_ends_when_its_work_ends(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run(quota=2)
    session = await opened(app_settings, store, recorder, streams, run["id"])
    await worked(pool, run["id"], 121)
    await session.reach_time_limit(hard=False)
    streams.last.push(assistant("Report: 0 fit jobs."), result(cost=0.02))
    await streams.last.flush()
    goal = await store.get_goal(run["id"])
    assert (goal["status"], goal["status_reason"]) == ("capped", TIME_REASON)
    assert not any(q.startswith("0 of 2 jobs fit") for q in streams.last.queries)  # no "continue toward the target"
    await session.close()


async def test_a_goal_with_its_quota_at_the_soft_limit_is_done(app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run(quota=2)
    await fit_roles(store, run["id"], ["a", "b"])
    session = await opened(app_settings, store, recorder, streams, run["id"])
    await worked(pool, run["id"], 121)
    await session.reach_time_limit(hard=False)
    streams.last.push(assistant("Report: 2 fit jobs."), result(cost=0.02))
    await streams.last.flush()
    assert (await store.get_goal(run["id"]))["status"] == "done"
    await session.close()


async def test_a_message_after_the_soft_limit_gets_an_answer_but_no_new_work(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run()
    session = await opened(app_settings, store, recorder, streams, run["id"])
    await worked(pool, run["id"], 121)
    await session.reach_time_limit(hard=False)
    streams.last.push(result(cost=0.02))
    await streams.last.flush()
    await session.send("Which jobs did you find?", "user")
    assert streams.last.queries[-1] == "Which jobs did you find?"
    streams.last.push(assistant("None yet."), result(cost=0.02))
    await streams.last.flush()
    assert (await store.get_goal(run["id"]))["status"] == "capped"
    await session.close()


# The hard limit

async def test_the_hard_limit_stops_all_work_and_gets_a_final_answer(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run()
    await store.add_scrape(run["id"], "rq-1", "linkedin_person", "https://www.linkedin.com/in/x/", True)
    career_lens = Cancels()
    session = await opened(app_settings, store, recorder, streams, run["id"], career_lens)
    await session.send("go", "mandate_run", user=False)
    first = streams.last
    first.push(agent_call("call-1", "research"), task_started("a1", "call-1"), result(cost=0.02))
    await first.flush()
    await worked(pool, run["id"], 151)

    await session.reach_time_limit(hard=True)
    wrap = streams.last
    assert wrap is not first and first.closed
    assert wrap.options.max_budget_usd == pytest.approx(0.20) and wrap.queries[0].startswith("You've reached")
    assert career_lens.asked == ["rq-1"]
    [task] = await store.list_tasks(run["id"])
    assert task["status"] == "stopped"
    assert (await store.get_goal(run["id"]))["soft_limit_at"] is not None
    await session.reach_time_limit(hard=True)  # the next check while the final answer is written
    assert streams.last is wrap
    wrap.push(assistant("Best answer."), result(cost=0.05))
    await wrap.flush()
    goal = await store.get_goal(run["id"])
    assert (goal["status"], goal["status_reason"]) == ("capped", TIME_REASON)
    assert any(text.startswith(f"{TIME_REASON} Writing the best answer") for text in await notices(store, run["id"]))
    await session.close()


async def test_a_goal_past_its_hard_limit_reopens_for_final_answers_only(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run()
    await store.set_soft_limit(run["id"])
    await store.set_status(run["id"], "capped", TIME_REASON)
    await worked(pool, run["id"], 151)
    async with pool.connection() as conn:  # it ended at once: all of its time counts
        await conn.execute("UPDATE goals SET updated_at = now() WHERE id = %s", (run["id"],))
    session = await opened(app_settings, store, recorder, streams, run["id"])
    assert streams.clients == []
    await session.send("anything else?", "user")
    assert streams.last.options.max_budget_usd == pytest.approx(0.20)
    await session.close()


# The check each minute

class Port:
    def __init__(self):
        self.calls = []

    async def time_limit(self, goal_id, hard):
        self.calls.append((goal_id, hard))


async def test_the_check_finds_each_mandate_goal_at_a_limit(store, make_run, pool):
    m, under = await make_run(slot_hour=1)
    _, soft = await make_run(slot_hour=2, mandate=m)
    _, soft_done = await make_run(slot_hour=3, mandate=m)
    _, hard = await make_run(slot_hour=4, mandate=m)
    _, ended = await make_run(slot_hour=5, mandate=m)
    user_goal = await store.create_goal("mine")
    await worked(pool, under["id"], 60)
    for goal in (soft, soft_done):
        await worked(pool, goal["id"], 125)
    await store.set_soft_limit(soft_done["id"])
    await worked(pool, hard["id"], 155)
    await worked(pool, ended["id"], 155)
    await store.set_status(ended["id"], "done")
    await worked(pool, user_goal["id"], 600)
    port = Port()
    await check_time_limits(store, MandateStore(pool), port)
    assert sorted(port.calls) == sorted([(soft["id"], False), (hard["id"], True)])


async def test_a_failed_check_of_one_goal_does_not_stop_the_others(store, make_run, pool):
    m, first = await make_run(slot_hour=1)
    _, second = await make_run(slot_hour=2, mandate=m)
    for goal in (first, second):
        await worked(pool, goal["id"], 155)

    class Flaky(Port):
        async def time_limit(self, goal_id, hard):
            if goal_id == first["id"]:
                raise RuntimeError("cannot open")
            await super().time_limit(goal_id, hard)

    port = Flaky()
    await check_time_limits(store, MandateStore(pool), port)
    assert port.calls == [(second["id"], True)]


async def test_the_scheduler_tick_checks_the_time_limits(store, artifacts, recorder, make_run, pool):
    from job_finder.scheduler import Scheduler
    _, run = await make_run()
    await worked(pool, run["id"], 125)

    class SchedulerPort(Port):
        async def send(self, *args, **kwargs):
            pass

    port = SchedulerPort()
    await Scheduler(store, MandateStore(pool), artifacts, recorder, port,
                    now=lambda: datetime.now(UTC) - timedelta(days=400)).tick()
    assert port.calls == [(run["id"], False)]


# Continue after the time limit

async def time_capped(store, pool, run, minutes=151):
    """The goal worked for `minutes`, then stopped at its time limit just now."""
    await worked(pool, run["id"], minutes)
    await store.set_soft_limit(run["id"])
    await store.set_status(run["id"], "capped", TIME_REASON)


async def test_continue_waits_for_a_higher_time_limit_of_the_mandate(store, make_run, pool):
    from job_finder.time_limit import continue_check
    mandates = MandateStore(pool)
    m, run = await make_run()
    await time_capped(store, pool, run)
    check = await continue_check(store, mandates, await store.get_goal(run["id"]))
    assert check["ok"] is False and check["limit_s"] == 7200 and check["mandate_limit_s"] == 7200
    assert check["reason"].startswith("Raise the time limit in the mandate chat first.")
    assert "This goal worked for 2h 31m. The time limit is 2h." in check["reason"]
    await mandates.update(m["id"], {"time_limit_minutes": 180})
    check = await continue_check(store, mandates, await store.get_goal(run["id"]))
    assert (check["ok"], check["reason"], check["mandate_limit_s"]) == (True, None, 180 * 60)


async def test_continue_is_only_for_a_goal_that_stopped_at_its_time_limit(store, make_run, pool):
    from job_finder.time_limit import continue_check
    mandates = MandateStore(pool)
    _, run = await make_run()
    assert await continue_check(store, mandates, await store.get_goal(run["id"])) is None  # it works
    await store.set_status(run["id"], "capped", "Spend limit reached.")
    assert await continue_check(store, mandates, await store.get_goal(run["id"])) is None
    goal = await store.create_goal("mine")
    await store.set_status(goal["id"], "capped", TIME_REASON)
    assert await continue_check(store, mandates, await store.get_goal(goal["id"])) is None


async def test_continue_waits_while_another_goal_of_the_mandate_works(store, make_run, pool):
    from job_finder.time_limit import continue_check
    mandates = MandateStore(pool)
    m, run = await make_run(slot_hour=1)
    await time_capped(store, pool, run)
    await mandates.update(m["id"], {"time_limit_minutes": 180})
    await mandates.create_run(m, datetime(2026, 10, 9, 3, tzinfo=UTC), 2.0, 2)  # goal 2 starts
    check = await continue_check(store, mandates, await store.get_goal(run["id"]))
    assert check["ok"] is False and check["reason"] == (
        "Goal 2 of this mandate is in progress. Continue this goal when it stops.")


async def test_continue_refuses_a_goal_at_its_spend_cap(store, make_run, pool):
    from job_finder.time_limit import continue_check
    mandates = MandateStore(pool)
    m, run = await make_run()
    await time_capped(store, pool, run)
    await mandates.update(m["id"], {"time_limit_minutes": 180})
    await store.add_spend(run["id"], 1.9)
    check = await continue_check(store, mandates, await store.get_goal(run["id"]))
    assert check["ok"] is False and check["reason"] == "This goal reached its spend cap, so it cannot continue."


async def test_continue_gives_the_goal_the_new_limit_and_says_how_much_time_is_left(store, make_run, pool):
    from job_finder.time_limit import ContinueError, continue_goal
    mandates = MandateStore(pool)
    m, run = await make_run()
    await time_capped(store, pool, run, minutes=150)
    with pytest.raises(ContinueError, match="Raise the time limit"):
        await continue_goal(store, mandates, run["id"])
    await mandates.update(m["id"], {"time_limit_minutes": 180, "grace_minutes": 15})
    note = await continue_goal(store, mandates, run["id"])
    goal = await store.get_goal(run["id"])
    assert (goal["time_limit_s"], goal["grace_s"], goal["soft_limit_at"]) == (180 * 60, 15 * 60, None)
    assert note.startswith("The user gave this goal more time. Its time limit is now 3h: about 30m more.")
    assert "get_pipeline" in note


async def test_a_continued_goal_works_again(app_settings, store, recorder, streams, make_run, pool):
    from job_finder.time_limit import continue_goal
    mandates = MandateStore(pool)
    m, run = await make_run()
    await time_capped(store, pool, run)
    async with pool.connection() as conn:
        await conn.execute("UPDATE goals SET updated_at = now() WHERE id = %s", (run["id"],))
    await mandates.update(m["id"], {"time_limit_minutes": 240})
    note = await continue_goal(store, mandates, run["id"])
    session = await opened(app_settings, store, recorder, streams, run["id"])
    await session.send(note, "time_continue", user=False)
    assert streams.last.options.max_budget_usd == pytest.approx(1.8)  # the normal budget, not a final answer
    assert (await store.get_goal(run["id"]))["status"] == "running"
    await session.close()


async def test_the_hard_limit_also_ends_a_goal_at_its_spend_cap_that_waits_on_scrapes(
        app_settings, store, recorder, streams, make_run, pool):
    _, run = await make_run()
    await store.add_spend(run["id"], 1.95)
    await store.set_status(run["id"], "waiting", "Waiting on 1 scrape")
    await store.add_scrape(run["id"], "rq-1", "linkedin_person", "https://www.linkedin.com/in/x/", True)
    await worked(pool, run["id"], 151)
    career_lens = Cancels()
    session = await opened(app_settings, store, recorder, streams, run["id"], career_lens)
    assert streams.clients == []  # at its spend cap: no client until a message
    await session.reach_time_limit(hard=True)
    assert career_lens.asked == ["rq-1"] and streams.last.options.max_budget_usd == pytest.approx(0.20)
    streams.last.push(assistant("Best answer."), result(cost=0.05))
    await streams.last.flush()
    assert (await store.get_goal(run["id"]))["status"] == "capped"
    await session.close()


async def test_continue_explains_that_an_ended_mandate_cannot_change(store, make_run, pool):
    from job_finder.time_limit import continue_check
    mandates = MandateStore(pool)
    m, run = await make_run()
    await time_capped(store, pool, run)
    await mandates.update(m["id"], {"status": "ended"})
    check = await continue_check(store, mandates, await store.get_goal(run["id"]))
    assert check["ok"] is False and check["reason"] == (
        "The mandate has ended, so its time limit cannot change. This goal cannot continue.")
