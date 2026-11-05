import asyncio

import pytest
from claude_agent_sdk import AssistantMessage, ProcessError, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from job_finder.career_lens import CareerLens
from job_finder.session import GoalSession

from .fakes import (
    Cancels,
    Streams,
    agent_call,
    assistant,
    fit_roles,
    result,
    task_notification,
    task_started,
    task_updated,
)


@pytest.fixture
def streams():
    return Streams()


@pytest.fixture
def make_session(app_settings, store, recorder, streams):
    async def make(goal_id, **kwargs):
        session = GoalSession(app_settings, store, recorder, CareerLens("", ""), goal_id, client_factory=streams,
                              **kwargs)
        await session.open(recover=False)
        return session
    return make


def session_file(settings, session_id):
    folder = settings.claude_config_dir / "projects" / "-agent"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text("")


async def events(store, goal_id, kind):
    return [e for e in await store.list_events(goal_id) if e["type"] == kind]


async def status(store, goal_id):
    return (await store.get_goal(goal_id))["status"]


async def test_open_sets_the_budget_and_resumes_the_session(make_session, store, streams, app_settings):
    session_file(app_settings, "sess-0")
    goal = await store.create_goal("g", max_spend_usd=2.0)
    await store.add_spend(goal["id"], 0.5)
    await store.set_sdk_session(goal["id"], "sess-0", 0.0)
    session = await make_session(goal["id"])
    options = streams.last.options
    assert options.max_budget_usd == pytest.approx(1.3)  # $2 cap, $0.50 spent, $0.20 kept for the wrap-up
    assert options.resume == "sess-0" and streams.last.connected
    await session.close()


async def test_a_message_while_busy_goes_to_the_client_at_once(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("Find 5 jobs", "user")
    await session.send("skip Germany", "follow_up")
    assert streams.last.queries == ["Find 5 jobs", "skip Germany"]
    assert [e["text"] for e in await events(store, goal["id"], "user_message")] == ["Find 5 jobs", "skip Germany"]
    await session.close()


async def test_spend_adds_the_difference_of_cumulative_costs(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    streams.last.push(assistant("one"), result(cost=0.10))
    await session.send("more", "follow_up")
    streams.last.push(assistant("two"), result(cost=0.25))
    await streams.last.flush()
    assert (await store.get_goal(goal["id"]))["spend_usd"] == pytest.approx(0.25)
    costs = [e["cost_usd"] for e in await events(store, goal["id"], "response_finished")]
    assert costs == [pytest.approx(0.10), pytest.approx(0.15)]
    await session.close()


async def test_goal_runs_while_a_subagent_works_and_settles_after_its_notification(make_session, store, streams):
    # Review focus 1: the second response starts with no send, from the task notification.
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("Find jobs", "user")
    client = streams.last
    client.push(agent_call("call-1", "job-discovery", "Find 5"), task_started("a1", "call-1", "Find 5"),
                assistant("On it."), result(cost=0.05))
    await client.flush()
    assert not session.main_busy and await status(store, goal["id"]) == "running"
    [row] = await store.list_tasks(goal["id"], "running")
    assert (row["name"], row["tool_use_id"]) == ("job-discovery", "call-1")
    client.push(task_updated("a1", "completed"), task_notification("a1", summary="6 roles saved"),
                assistant("job-discovery found 6 jobs."), result(cost=0.09))
    await client.flush()
    assert await status(store, goal["id"]) == "done" and client.queries == ["Find jobs"]
    [row] = await store.list_tasks(goal["id"])
    assert (row["status"], row["summary"]) == ("completed", "6 roles saved")
    [finished] = await events(store, goal["id"], "agent_finished")
    assert finished["summary"] == "6 roles saved"
    assert (await session.settled())["status"] == "done"
    await session.close()


async def test_idle_goal_with_pending_scrapes_is_waiting(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "u", True)
    await session.send("go", "user")
    streams.last.push(result(cost=0.01))
    await streams.last.flush()
    fresh = await store.get_goal(goal["id"])
    assert (fresh["status"], fresh["status_reason"]) == ("waiting", "Waiting on 1 scrape")
    await session.close()


async def test_interrupt_only_while_the_main_agent_is_busy(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    assert await session.interrupt() is False
    await session.send("go", "user")
    assert await session.interrupt() is True and streams.last.interrupts == 1
    streams.last.push(result("error_during_execution", is_error=True, cost=0.01))
    await streams.last.flush()
    assert "Stopped the reply." in [
        e["text"] for e in await events(store, goal["id"], "notice")]
    await session.close()


async def test_stop_ends_all_work(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    streams.last.push(agent_call("call-1", "job-discovery"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    await session.stop()
    assert streams.last.stopped == ["a1"]
    assert (await store.list_tasks(goal["id"]))[0]["status"] == "stopped"
    fresh = await store.get_goal(goal["id"])
    assert (fresh["status"], fresh["status_reason"]) == ("stopped", "Stopped by the user")
    await session.close()


async def test_stop_cancels_in_career_lens_what_no_other_goal_waits_on(
        app_settings, store, recorder, streams):
    goal, other = await store.create_goal("g"), await store.create_goal("other")
    for goal_id, request_id in ((goal["id"], "rq-own"), (goal["id"], "rq-shared"), (other["id"], "rq-shared")):
        await store.add_scrape(goal_id, request_id, "linkedin_company",
                               f"https://www.linkedin.com/company/{request_id}/", True)
    career_lens = Cancels()
    session = GoalSession(app_settings, store, recorder, career_lens, goal["id"], client_factory=streams)
    await session.open(recover=False)
    await session.send("go", "user")
    streams.last.push(agent_call("call-1", "job-research"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    session.waiting.add("rq-page")  # a read_page of the subagent waits on this page

    async def stop_task(task_id):  # the stopped tool call forgets its request
        session.waiting.clear()
    streams.last.stop_task = stop_task
    await session.stop()

    assert sorted(career_lens.asked) == ["rq-own", "rq-page"]
    finished = {e["request_id"]: e for e in await store.list_events(goal["id"]) if e["type"] == "scrape_finished"}
    assert (finished["rq-own"]["status"], finished["rq-own"]["answer"]) == ("cancelled", "cancelled")
    assert finished["rq-shared"]["answer"] is None  # the other goal still waits on it
    await session.close()


async def test_close_interrupts_running_tasks_and_disconnects(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    streams.last.push(agent_call("call-1", "job-discovery"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    await session.close()
    assert streams.last.closed and streams.last.stopped == []  # not stopped: it can be resumed
    assert (await store.list_tasks(goal["id"]))[0]["status"] == "interrupted"



async def test_a_resumed_session_counts_spend_from_its_earlier_total(make_session, store, streams, app_settings):
    # Review focus 2: the SDK reports a resumed session's cost from its earlier total on.
    session_file(app_settings, "sess-0")
    goal = await store.create_goal("g")
    await store.set_sdk_session(goal["id"], "sess-0", 0.10)
    await store.add_spend(goal["id"], 0.10)
    session = await make_session(goal["id"])
    await session.send("more", "follow_up")
    streams.last.push(assistant("ok"), result(cost=0.15))  # 0.10 earlier + 0.05 now
    await streams.last.flush()
    fresh = await store.get_goal(goal["id"])
    assert fresh["spend_usd"] == pytest.approx(0.15) and fresh["sdk_cost_usd"] == pytest.approx(0.15)
    await session.close()


async def test_a_new_session_counts_spend_from_zero(make_session, store, streams):
    goal = await store.create_goal("g")
    await store.set_sdk_session(goal["id"], None, 0.40)  # left from a session whose file is gone
    session = await make_session(goal["id"])
    await session.send("go", "user")
    streams.last.push(assistant("ok"), result(cost=0.05))
    await streams.last.flush()
    assert (await store.get_goal(goal["id"]))["spend_usd"] == pytest.approx(0.05)
    await session.close()


# Recovery, limits and errors

async def opened(app_settings, store, recorder, streams, goal_id, **kwargs):
    session = GoalSession(app_settings, store, recorder, CareerLens("", ""), goal_id, client_factory=streams,
                          **kwargs)
    await session.open(recover=True)
    return session


async def test_reopen_lists_interrupted_subagents_once(app_settings, store, recorder, streams):
    # Review focus 4
    session_file(app_settings, "sess-1")
    goal = await store.create_goal("g")
    await store.set_sdk_session(goal["id"], "sess-1", 0.0)
    await store.start_task(goal["id"], "a63", "job-discovery", "Find 10 Go roles", "call-1")  # left running by a crash
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    [note] = streams.last.queries
    assert note.startswith("You were interrupted by an app restart.")
    assert 'job-discovery (agentId a63, task "Find 10 Go roles")' in note and "SendMessage" in note
    assert (await store.list_tasks(goal["id"]))[0]["status"] == "stopped"
    # the main agent resumes it: the task runs again, under its first name
    streams.last.push(task_started("a63", "call-send"), assistant("working", parent="call-1"))
    await streams.last.flush()
    assert [(t["name"], t["status"]) for t in await store.list_tasks(goal["id"])] == [("job-discovery", "running")]
    await session.close()
    second = await opened(app_settings, store, recorder, streams, goal["id"])
    assert "job-discovery (agentId a63" in streams.last.queries[0]
    await second.close()


async def test_no_note_for_an_idle_goal(app_settings, store, recorder, streams):
    goal = await store.create_goal("g")
    await store.set_status(goal["id"], "done")
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    assert streams.last.queries == []
    await session.close()


async def test_lost_session_file_starts_fresh_with_a_recap(app_settings, store, recorder, streams):
    goal = await store.create_goal("g")
    await store.set_sdk_session(goal["id"], "gone", 0.0)
    await store.set_status(goal["id"], "done")
    await recorder.record(goal["id"], "user_message", text="Find 5 jobs")
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    assert streams.last.options.resume is None and streams.last.queries == []
    await session.send("10 more", "follow_up")
    assert streams.last.queries[0].startswith("Your earlier conversation for this goal was lost.")
    assert streams.last.queries[0].endswith("10 more")
    await session.close()


async def test_spend_cap_stops_subagents_and_gets_a_wrap_up_answer(app_settings, store, recorder, streams):
    # Review focus 2: the wrap-up client resumes the same SDK session, so its cost goes on from 0.80.
    goal = await store.create_goal("g", max_spend_usd=1.0)
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    await session.send("go", "user")
    first = streams.last
    first.push(agent_call("call-1", "job-discovery"), task_started("a1", "call-1"),
               result("error_max_budget_usd", is_error=True, cost=0.80))
    await first.flush()
    wrap = streams.last
    assert wrap is not first and first.closed
    assert wrap.options.max_budget_usd == pytest.approx(0.20) and wrap.options.max_turns == 3
    assert wrap.queries[0].startswith("You've reached this goal's limits.")
    assert (await store.list_tasks(goal["id"]))[0]["status"] == "stopped"
    wrap.push(assistant("Best answer."), result(cost=0.85))
    await wrap.flush()
    fresh = await store.get_goal(goal["id"])
    assert fresh["status"] == "capped" and fresh["spend_usd"] == pytest.approx(0.85)
    await session.send("anything else?", "follow_up")  # a capped goal answers with one more wrap-up
    assert streams.last is not wrap and streams.last.queries[0].endswith("The user's latest message: anything else?")
    await session.close()


async def test_spend_cap_cancels_in_career_lens_what_no_other_goal_waits_on(app_settings, store, recorder, streams):
    goal, other = await store.create_goal("g", max_spend_usd=1.0), await store.create_goal("other")
    for goal_id, request_id in ((goal["id"], "rq-own"), (goal["id"], "rq-shared"), (other["id"], "rq-shared")):
        await store.add_scrape(goal_id, request_id, "linkedin_company",
                               f"https://www.linkedin.com/company/{request_id}/", True)
    career_lens = Cancels()
    session = GoalSession(app_settings, store, recorder, career_lens, goal["id"], client_factory=streams)
    await session.open(recover=False)
    await session.send("go", "user")
    session.waiting.add("rq-page")  # a read_page of the subagent waits on this page
    streams.last.push(agent_call("call-1", "job-research"), task_started("a1", "call-1"),
                      result("error_max_budget_usd", is_error=True, cost=0.80))
    await streams.last.flush()

    assert sorted(career_lens.asked) == ["rq-own", "rq-page"]
    finished = {e["request_id"]: e for e in await store.list_events(goal["id"]) if e["type"] == "scrape_finished"}
    assert (finished["rq-own"]["status"], finished["rq-own"]["answer"]) == ("cancelled", "cancelled")
    assert finished["rq-shared"]["answer"] is None  # the other goal still waits on it
    assert {s["status"] for s in await store.list_scrapes(goal["id"])} == {"cancelled"}
    assert {s["status"] for s in await store.list_scrapes(other["id"])} == {"pending"}
    await session.close()


async def test_capped_goal_reopens_without_a_client_until_a_message(app_settings, store, recorder, streams):
    goal = await store.create_goal("g", max_spend_usd=1.0)
    await store.add_spend(goal["id"], 0.9)
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    assert streams.clients == []
    await session.send("summary please", "follow_up")
    assert streams.last.options.max_budget_usd == pytest.approx(0.20)
    await session.close()


async def test_turn_limit_for_one_response_is_a_notice(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    streams.last.push(result("error_max_turns", is_error=True, cost=0.1))
    await streams.last.flush()
    assert "Turn limit for one response reached." in [e["text"] for e in await events(store, goal["id"], "notice")]
    assert await status(store, goal["id"]) == "done"
    await session.close()


async def test_a_crash_gets_one_reconnect_then_fails(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    first = streams.last
    first.push(agent_call("call-1", "job-research"), task_started("r1", "call-1", "Research jobs"),
               RuntimeError("process exited"))
    await asyncio.sleep(0.05)
    second = streams.last
    assert second is not first
    assert second.queries[0].startswith("Your work stopped because of a temporary error.")
    assert "job-research (agentId r1" in second.queries[0]
    second.push(RuntimeError("process exited again"))
    await asyncio.sleep(0.05)
    fresh = await store.get_goal(goal["id"])
    assert (fresh["status"], fresh["status_reason"]) == ("failed", "process exited again")
    await session.close()


async def test_a_stop_signal_reconnects_with_no_notice(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    first = streams.last
    first.push(ProcessError("Command failed", exit_code=143))  # e.g. Ctrl+C on the server
    await asyncio.sleep(0.05)
    assert streams.last is not first
    assert not [e for e in await events(store, goal["id"], "notice") if e["text"].startswith("Temporary problem")]
    await session.close()


async def test_an_auth_error_fails_with_no_retry(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    error = AssistantMessage(model="m", content=[TextBlock(text="Not logged in")], error="authentication_failed")
    streams.last.push(error, result(cost=0.0))
    await streams.last.flush()
    fresh = await store.get_goal(goal["id"])
    assert fresh["status"] == "failed" and "authentication failed" in fresh["status_reason"]
    assert len(streams.clients) == 1
    await session.close()


async def test_a_scrapes_note_during_the_wrap_up_does_not_start_another_wrap_up(app_settings, store, recorder,
                                                                                streams):
    goal = await store.create_goal("g", max_spend_usd=1.0)
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    await session.send("go", "user")
    streams.last.push(result("error_max_budget_usd", is_error=True, cost=0.80))
    await streams.last.flush()
    wrap = streams.last
    await session.send("Scrapes finished for job-research (agentId a7f):", "scrapes_finished", user=False)
    assert streams.last is wrap and len(wrap.queries) == 1 and not wrap.closed
    await session.close()


def test_auth_and_rate_limit_errors_name_the_claude_login():
    from job_finder.session import assistant_error
    assert assistant_error("authentication_failed", "api_key") == (
        "Claude authentication failed: check ANTHROPIC_API_KEY.", True)
    text, persistent = assistant_error("authentication_failed", "oauth_token")
    assert persistent and "subscription token may have expired" in text and "job-finder login" in text
    text, persistent = assistant_error("rate_limit", "oauth_token")
    assert not persistent and "usage limit" in text and "plan" in text
    assert assistant_error("rate_limit", "api_key") == ("Claude API error (rate_limit).", False)
    assert assistant_error("billing_error", "oauth_token")[1] is True


async def until(check, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if await check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def no_sleep(seconds):
    return None


async def test_the_main_agent_continues_until_a_round_adds_no_fit_job(make_session, store, streams):
    goal = await store.create_goal("g")
    await store.set_brief(goal["id"], {"criteria": "fintech", "target_count": 3, "profile": ""})
    await fit_roles(store, goal["id"], ["a"])
    session = await make_session(goal["id"], sleep=no_sleep)
    client = streams.last
    await session.send("Find 3 jobs", "user")
    client.push(result())
    await until(lambda: asyncio.sleep(0, len(client.queries) == 2))
    assert client.queries[1].startswith("1 of 3 roles fit so far (1 found, 1 researched")
    [note] = [e for e in await events(store, goal["id"], "notice") if e["trigger"] == "continue"]
    assert note["fit"] == 1 and await status(store, goal["id"]) == "running"
    await fit_roles(store, goal["id"], ["b"])  # this round found one more fit job: continue again
    client.push(result())
    await until(lambda: asyncio.sleep(0, len(client.queries) == 3))
    assert client.queries[2].startswith("2 of 3 roles fit so far")
    client.push(result())  # this round found no new fit job: stop
    await client.flush()
    await until(lambda: asyncio.sleep(0, session._continuing is None))
    assert len(client.queries) == 3 and await status(store, goal["id"]) == "done"
    await session.close()


async def test_no_continue_when_the_target_is_met_or_there_is_no_target(make_session, store, streams):
    met = await store.create_goal("met")
    await store.set_brief(met["id"], {"criteria": "fintech", "target_count": 1, "profile": ""})
    await fit_roles(store, met["id"], ["a"])
    no_brief = await store.create_goal("research a company")
    for goal_id in (met["id"], no_brief["id"]):
        session = await make_session(goal_id, sleep=no_sleep)
        await session.send("Go", "user")
        streams.last.push(result())
        await streams.last.flush()
        assert streams.last.queries == ["Go"] and await status(store, goal_id) == "done"
        await session.close()


async def test_no_continue_in_the_home_chat_of_a_mandate(make_session, store, streams):
    from datetime import UTC, datetime, time

    from job_finder.mandate_store import MandateStore
    home = await store.create_goal("each weekday, 20 fit roles")
    await store.set_brief(home["id"], {"criteria": "fintech", "target_count": 20, "profile": ""})
    await MandateStore(store.pool).create(home["id"], {
        "title": "t", "instruction": "fintech", "every_unit": "day", "every_count": 1, "jobs_per_run": 20, "window_days": [1, 2, 3, 4, 5],
        "window_start": time(9), "timezone": "UTC", "max_spend_per_run": 2, "max_scrapes_per_run": 15,
        "next_run_at": datetime.now(UTC)})
    session = await make_session(home["id"], sleep=no_sleep)
    await session.send("Yes, start it.", "user")
    streams.last.push(result())
    await streams.last.flush()
    assert streams.last.queries == ["Yes, start it."] and await status(store, home["id"]) == "done"
    await session.close()


async def test_no_continue_when_other_work_starts_during_the_delay(make_session, store, streams):
    goal = await store.create_goal("g")
    await store.set_brief(goal["id"], {"criteria": "fintech", "target_count": 2, "profile": ""})
    gate = asyncio.Event()

    async def wait(seconds):
        await gate.wait()

    session = await make_session(goal["id"], sleep=wait)
    client = streams.last
    await session.send("Find 2 jobs", "user")
    client.push(result())
    await client.flush()
    assert session._continuing is not None and await status(store, goal["id"]) == "running"
    client.push(assistant("A subagent finished; I'll check its report."))  # a response that a task notification started
    await client.flush()
    gate.set()
    await until(lambda: asyncio.sleep(0, session._continuing is None))
    assert client.queries == ["Find 2 jobs"]  # nothing sent while the main agent works
    await session.close()


async def test_the_guard_follows_the_running_tasks(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    assert session.guard.try_start("job-research", "call-1")
    streams.last.push(agent_call("call-1", "job-research"), task_started("a1", "call-1"))
    await streams.last.flush()
    assert session.guard.busy("job-research") and not session.guard.busy("job-discovery")
    streams.last.push(task_notification("a1", tool_use_id="call-1"))
    await streams.last.flush()
    assert not session.guard.busy("job-research")
    assert session.guard.try_start("fit-check", "call-2")
    streams.last.push(UserMessage(content=[ToolResultBlock(tool_use_id="call-2", content="failed", is_error=True)]),
                      result(cost=0.02))
    await streams.last.flush()
    assert not session.guard.busy("fit-check")  # a failed call does not keep the type busy
    await session.close()


def finish_call(call_id, reason):
    return [AssistantMessage(model="m", content=[ToolUseBlock(id=call_id, name="mcp__artifacts__finish_goal",
                                                              input={"reason": reason})]),
            UserMessage(content=[ToolResultBlock(tool_use_id=call_id, content=f"Goal finished: {reason}")])]


async def test_no_continue_after_the_main_agent_finishes_the_goal(make_session, store, streams):
    goal = await store.create_goal("g")
    await store.set_brief(goal["id"], {"criteria": "fintech", "target_count": 3, "profile": ""})
    await fit_roles(store, goal["id"], ["a"])
    session = await make_session(goal["id"], sleep=no_sleep)
    client = streams.last
    await session.send("Find 3 jobs", "user")
    client.push(*finish_call("f1", "Few companies match."), result())
    await client.flush()
    await until(lambda: asyncio.sleep(0, session._continuing is None))
    assert client.queries == ["Find 3 jobs"] and await status(store, goal["id"]) == "done"
    await session.send("Try other regions", "user")  # a new message from the user: the rule applies again
    client.push(result())
    await until(lambda: asyncio.sleep(0, len(client.queries) == 3))
    assert client.queries[2].startswith("1 of 3 roles fit so far")
    await session.close()


async def test_a_finished_subagent_keeps_the_goal_running_until_the_main_agent_answers(make_session, store, streams):
    goal = await store.create_goal("g")
    gate = asyncio.Event()

    async def wait(seconds):
        await gate.wait()

    session = await make_session(goal["id"], sleep=wait)
    await session.send("Find jobs", "user")
    client = streams.last
    client.push(agent_call("call-1", "job-discovery", "Find 5"), task_started("a1", "call-1", "Find 5"), result(cost=0.05))
    await client.flush()
    # the notification wakes the main agent, but its answer comes a moment later: no "done" between them
    client.push(task_updated("a1", "completed"), task_notification("a1", summary="6 roles saved"))
    await client.flush()
    assert await status(store, goal["id"]) == "running"
    client.push(assistant("job-discovery found 6 jobs."), result(cost=0.09))
    await client.flush()
    assert await status(store, goal["id"]) == "done"
    assert [e["to"] for e in await events(store, goal["id"], "status_changed")].count("done") == 1
    # with no answer, the goal settles when the wait ends
    client.push(agent_call("call-2", "job-research", "Research"), task_started("a2", "call-2", "Research"), result())
    await client.flush()
    client.push(task_notification("a2", summary="5 researched"))
    await client.flush()
    assert await status(store, goal["id"]) == "running"
    gate.set()
    await until(lambda: asyncio.sleep(0, session._waking is None))
    assert await status(store, goal["id"]) == "done"
    await session.close()


async def test_after_a_stop_the_main_agent_does_not_answer_the_stopped_subagent(make_session, store, streams):
    goal = await store.create_goal("g")
    session = await make_session(goal["id"])
    await session.send("go", "user")
    client = streams.last
    client.push(agent_call("call-1", "job-discovery"), task_started("a1", "call-1"), result(cost=0.02))
    await client.flush()
    await session.stop()
    # the stopped subagent's notification starts a main response by itself: it is stopped, and not shown
    client.push(task_notification("a1", status="killed"), assistant("The user stopped job-gen, so I finish."),
                result(cost=0.05))
    await client.flush()
    assert client.interrupts == 1 and not session.main_busy
    assert not [e for e in await events(store, goal["id"], "text") if "I finish" in e["text"]]
    assert not [e for e in await events(store, goal["id"], "notice") if "Stopped the reply" in e["text"]]
    fresh = await store.get_goal(goal["id"])
    assert (fresh["status"], fresh["spend_usd"]) == ("stopped", pytest.approx(0.05))
    # a retry after it works as usual
    await session.send("Continue.", "retry", user=False)
    client.push(assistant("Continuing."), result(cost=0.07))
    await client.flush()
    assert [e["text"] for e in await events(store, goal["id"], "text")][-1] == "Continuing."
    await session.close()


async def test_a_home_chat_does_not_resume_a_session_with_an_other_prompt(make_session, store, streams, app_settings):
    # A resumed SDK session keeps its first system prompt. The session of the goal that became a home chat has the
    # main agent's prompt, so the home chat starts a new session with the home prompt.
    from datetime import UTC, datetime, time

    from job_finder.mandate_store import MandateStore
    from job_finder.options import home_prompt, main_prompt, prompt_sha
    session_file(app_settings, "sess-0")
    home = await store.create_goal("each weekday, 20 fit roles")
    await store.set_sdk_session(home["id"], "sess-0", 0.0)
    await store.set_session_prompt(home["id"], prompt_sha(main_prompt(home["id"])))
    await MandateStore(store.pool).create(home["id"], {
        "title": "t", "instruction": "fintech", "every_unit": "day", "every_count": 1, "jobs_per_run": 20,
        "window_days": [1], "window_start": time(9), "timezone": "UTC", "max_spend_per_run": 2,
        "max_scrapes_per_run": 15, "next_run_at": datetime.now(UTC)})
    session = await make_session(home["id"])
    assert streams.last.options.resume is None
    assert streams.last.options.system_prompt == home_prompt(home["id"])
    assert (await store.get_goal(home["id"]))["sdk_session_id"] is None
    await session.close()

    # A session that started with the home prompt resumes.
    session_file(app_settings, "sess-1")
    await store.set_sdk_session(home["id"], "sess-1", 0.0)
    await store.set_session_prompt(home["id"], prompt_sha(home_prompt(home["id"])))
    session = await make_session(home["id"])
    assert streams.last.options.resume == "sess-1"
    await session.close()


async def test_spend_from_outside_the_main_agent_also_reaches_the_cap(app_settings, store, recorder, streams):
    # read_page adds the cost of its small model to the goal. The SDK does not see it, so the app checks the goal.
    goal = await store.create_goal("g", max_spend_usd=1.0)
    session = await opened(app_settings, store, recorder, streams, goal["id"])
    await session.send("go", "user")
    first = streams.last
    await store.add_spend(goal["id"], 0.35)  # the page reader during this response
    first.push(assistant("Working."), result(cost=0.50))
    await first.flush()
    wrap = streams.last
    assert wrap is not first and first.closed
    assert wrap.queries[0].startswith("You've reached this goal's limits.")

