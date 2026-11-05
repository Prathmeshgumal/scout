import httpx
import pytest
import respx

from job_finder.career_lens import CareerLens
from job_finder.watcher import Watcher, agent_message, resume_message

BASE = "https://wh.test"


class FakeRunner:
    def __init__(self, busy=(), running=()):
        self.busy = set(busy)
        self.running = set(running)  # (goal_id, agent_id) of subagents that still run
        self.busy_types = set()  # (goal_id, subagent type) that has a run in the goal
        self.elsewhere = set()  # goals that another process drives
        self.started = []

    async def reachable(self, goal_id):
        return goal_id not in self.elsewhere

    async def ready(self, goal_id):
        return goal_id not in self.busy and goal_id not in self.elsewhere

    def is_running(self, goal_id, agent_id):
        return (goal_id, agent_id) in self.running

    def type_busy(self, goal_id, name):
        return (goal_id, name) in self.busy_types

    def start(self, goal_id, message, trigger):
        self.busy.add(goal_id)
        self.started.append((goal_id, message, trigger))


@pytest.fixture
async def setup(store, recorder):
    wh = CareerLens(BASE, "cl_v1.ak-test.key")
    runner = FakeRunner()
    yield Watcher(store, recorder, wh, runner, interval_s=60), runner
    await wh.aclose()


async def waiting_goal(store, request_id="rq-1"):
    goal = await store.create_goal("g")
    await store.add_scrape(goal["id"], request_id, "linkedin_company", "https://www.linkedin.com/company/acme/", True)
    await store.set_status(goal["id"], "waiting")
    return goal


@respx.mock
async def test_status_change_is_recorded_once(setup, store):
    watcher, runner = setup
    goal = await waiting_goal(store)
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-1", "status": "queued", "wait_reason": "account off shift",
        "next_attempt_at": "2026-10-02T09:00:00+00:00"}))
    await watcher.poll_once()
    await watcher.poll_once()  # unchanged: no second event
    events = [e for e in await store.list_events(goal["id"]) if e["type"] == "scrape_status"]
    assert len(events) == 1 and events[0]["wait_reason"] == "account off shift"
    assert runner.started == []


@respx.mock
async def test_finished_scrape_resumes_goal_with_outcomes(setup, store):
    watcher, runner = setup
    goal = await waiting_goal(store)
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-1", "status": "succeeded"}))
    await watcher.poll_once()
    [(goal_id, message, trigger)] = runner.started
    assert (goal_id, trigger) == (goal["id"], "scrapes_finished")
    assert "rq-1 (linkedin_company, https://www.linkedin.com/company/acme/): succeeded" in message
    assert [e["type"] for e in await store.list_events(goal["id"])] == ["scrape_finished"]


@respx.mock
async def test_failed_scrape_reports_kind(setup, store):
    watcher, runner = setup
    await waiting_goal(store)
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-1", "status": "failed", "failure": {"kind": "blocked", "detail": "x"}}))
    await watcher.poll_once()
    assert "failed (blocked)" in runner.started[0][1]


async def test_waiting_goal_whose_scrapes_already_finished_is_still_resumed(setup, store):
    # Review focus 2: the run ended after its scrapes had finished; nothing is pending.
    watcher, runner = setup
    goal = await store.create_goal("g")
    scrape = await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "u", True)
    await store.finish_scrape(scrape["id"], "succeeded", None)
    await store.set_status(goal["id"], "waiting")
    await watcher.poll_once()
    assert runner.started and runner.started[0][0] == goal["id"]


@respx.mock
async def test_active_goal_is_not_resumed_twice_and_career_lens_errors_are_skipped(setup, store):
    watcher, runner = setup
    goal = await waiting_goal(store)
    runner.busy.add(goal["id"])
    respx.get(f"{BASE}/v1/requests/rq-1").mock(side_effect=httpx.ConnectError("down"))
    await watcher.poll_once()  # does not raise
    assert runner.started == []


def test_resume_message_lists_each_scrape():
    text = resume_message([
        {"request_id": "rq-1", "kind": "linkedin_company", "target": "u1", "status": "succeeded", "failure_kind": None},
        {"request_id": "rq-2", "kind": "linkedin_company", "target": "u2", "status": "failed", "failure_kind": "blocked"}])
    assert text.splitlines() == [
        "Scrapes finished:",
        "- rq-1 (linkedin_company, u1): succeeded",
        "- rq-2 (linkedin_company, u2): failed (blocked)",
        "Read the new data with get_company, get_job_search or get_linkedin_job (or search again) and continue working toward the goal."]


@respx.mock
async def test_unreadable_next_attempt_at_does_not_stop_the_poll(setup, store):
    watcher, runner = setup
    first = await waiting_goal(store, "rq-1")
    second = await waiting_goal(store, "rq-2")
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-1", "status": "queued", "next_attempt_at": "tomorrow-ish"}))
    respx.get(f"{BASE}/v1/requests/rq-2").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-2", "status": "succeeded"}))
    await watcher.poll_once()
    [status] = [e for e in await store.list_events(first["id"]) if e["type"] == "scrape_status"]
    assert status["next_attempt_at"] is None
    assert [s[0] for s in runner.started] == [second["id"]]


@respx.mock
async def test_scrape_waiting_past_the_limit_is_expired_and_the_goal_finishes(setup, store):
    watcher, runner = setup
    goal = await waiting_goal(store)
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE scrapes SET submitted_at = now() - interval '73 hours'")
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-1", "status": "queued", "wait_reason": "account off shift"}))
    cancel = respx.post(f"{BASE}/v1/requests/rq-1/cancel").mock(
        return_value=httpx.Response(200, json={"request_id": "rq-1", "status": "cancelled", "cancel": "cancelled"}))
    await watcher.poll_once()
    [(goal_id, message, trigger)] = runner.started
    assert (goal_id, trigger) == (goal["id"], "wait_expired")
    assert message.splitlines()[0] == "Stopped waiting for scrapes after 72 hours:"
    assert "- rq-1 (linkedin_company, https://www.linkedin.com/company/acme/): expired" in message
    finished = [e for e in await store.list_events(goal["id"]) if e["type"] == "scrape_finished"]
    assert (finished[0]["status"], finished[0]["answer"]) == ("expired", "cancelled")
    assert cancel.call_count == 1


@respx.mock
async def test_an_expired_scrape_that_another_goal_waits_on_is_not_cancelled(setup, store):
    """All goals use one career-lens key: career-lens cannot tell that the other goal still wants it."""
    watcher, runner = setup
    await waiting_goal(store)
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE scrapes SET submitted_at = now() - interval '73 hours'")
    await waiting_goal(store)  # a newer goal asked for the same profile: the same request
    respx.get(f"{BASE}/v1/requests/rq-1").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-1", "status": "queued"}))
    cancel = respx.post(f"{BASE}/v1/requests/rq-1/cancel")
    await watcher.poll_once()
    assert len(runner.started) == 1  # the old goal gave up
    assert not cancel.called


async def test_goal_outside_goal_ids_is_not_resumed(store, recorder):
    goal = await waiting_goal(store)
    await store.finish_scrape((await store.pending_scrapes())[0]["id"], "succeeded", None)
    runner = FakeRunner()
    watcher = Watcher(store, recorder, CareerLens(BASE, "k"), runner, interval_s=60, goal_ids={"other"})
    await watcher.poll_once()
    assert runner.started == []
    watcher.goal_ids = {goal["id"]}
    await watcher.poll_once()
    assert [g for g, _, _ in runner.started] == [goal["id"]]


async def agent_goal(store, *request_ids, agent_id="a7f", status="running"):
    goal = await store.create_goal("g")
    await store.start_task(goal["id"], agent_id, "research", "Research jobs", "call-1")
    for request_id in request_ids:
        await store.add_scrape(goal["id"], request_id, "linkedin_company", f"https://www.linkedin.com/company/{request_id}/",
                               True)
    await store.set_scrape_agent(goal["id"], list(request_ids), agent_id)
    await store.set_status(goal["id"], status)
    return goal


def mock_status(request_id, status, **extra):
    code = 200 if status in ("succeeded", "failed") else 202
    respx.get(f"{BASE}/v1/requests/{request_id}").mock(return_value=httpx.Response(code, json={
        "request_id": request_id, "status": status, **extra}))


@respx.mock
async def test_subagent_gets_one_note_when_all_its_scrapes_finish(setup, store):
    watcher, runner = setup
    goal = await agent_goal(store, "rq-1", "rq-2")
    runner.busy.add(goal["id"])  # the main agent is busy: the note is still sent, and the SDK queues it
    mock_status("rq-1", "succeeded")
    mock_status("rq-2", "queued")
    await watcher.poll_once()
    assert runner.started == []  # rq-2 is still pending
    mock_status("rq-2", "failed", failure={"kind": "blocked"})
    await watcher.poll_once()
    [(goal_id, message, trigger)] = runner.started
    assert (goal_id, trigger) == (goal["id"], "scrapes_finished")
    assert message.splitlines()[0] == "Scrapes finished for research (agentId a7f):"
    assert "- rq-1 (linkedin_company, https://www.linkedin.com/company/rq-1/): succeeded" in message
    assert "- rq-2 (linkedin_company, https://www.linkedin.com/company/rq-2/): failed (blocked)" in message
    await watcher.poll_once()
    assert len(runner.started) == 1  # reported once


@respx.mock
async def test_running_subagent_gets_no_note(setup, store):
    watcher, runner = setup
    goal = await agent_goal(store, "rq-1")
    runner.running.add((goal["id"], "a7f"))
    mock_status("rq-1", "succeeded")
    await watcher.poll_once()
    assert runner.started == []
    runner.running.clear()  # it finished without reading the scrape
    await watcher.poll_once()
    assert len(runner.started) == 1


@respx.mock
async def test_subagent_note_waits_while_another_run_of_its_type_works(setup, store):
    watcher, runner = setup
    goal = await agent_goal(store, "rq-1")
    runner.busy_types.add((goal["id"], "research"))  # a new research run works: it cannot be resumed yet
    mock_status("rq-1", "succeeded")
    await watcher.poll_once()
    assert runner.started == []
    runner.busy_types.clear()
    await watcher.poll_once()
    assert len(runner.started) == 1


@respx.mock
async def test_subagent_note_waits_while_another_process_drives_the_goal(setup, store):
    watcher, runner = setup
    goal = await agent_goal(store, "rq-1")
    runner.elsewhere.add(goal["id"])
    mock_status("rq-1", "succeeded")
    await watcher.poll_once()
    assert runner.started == []  # the scrape stays unreported for the process that has the goal
    runner.elsewhere.clear()
    await watcher.poll_once()
    assert len(runner.started) == 1


@respx.mock
async def test_expired_scrapes_of_a_subagent_go_out_in_its_note(setup, store):
    watcher, runner = setup
    await agent_goal(store, "rq-1", status="waiting")
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE scrapes SET submitted_at = now() - interval '73 hours'")
    mock_status("rq-1", "queued", wait_reason="account off shift")
    respx.post(f"{BASE}/v1/requests/rq-1/cancel").mock(return_value=httpx.Response(404, json={}))
    await watcher.poll_once()
    [(goal_id, message, trigger)] = runner.started  # one note, no separate "Stopped waiting" resume
    assert trigger == "scrapes_finished"
    assert "- rq-1 (linkedin_company, https://www.linkedin.com/company/rq-1/): expired" in message


def test_agent_message_tells_the_main_agent_how_to_resume():
    text = agent_message(None, "a7f", [
        {"request_id": "rq-1", "kind": "linkedin_company", "target": "u1", "status": "succeeded", "failure_kind": None}])
    assert text.splitlines() == [
        "Scrapes finished for a subagent (agentId a7f):",
        "- rq-1 (linkedin_company, u1): succeeded",
        "Resume it with SendMessage (to: a7f), and tell it that these scrapes finished. If SendMessage is "
        "refused, give the same roles to a new task of that subagent."]
