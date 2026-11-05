import asyncio

import pytest

from job_finder.db import GoalLock, mandate_lock_key
from job_finder.deletion import Deleter, MandateChat
from job_finder.mandate_store import MandateStore
from job_finder.services import GoalLocked
from job_finder.store import GoalNotFound
from job_finder.web.sessions import GoalSessions

from .fakes import Cancels, Streams, agent_call, make_services, replies, result, task_started

SESSION = "0b6f7c1e-6c55-4f43-9a51-2f3d5c8e9a10"  # the SDK names a session with a UUID


@pytest.fixture
def streams():
    return Streams(replies("On it."))


@pytest.fixture
async def sessions(app_settings, store, hub, recorder, streams):
    manager = GoalSessions(make_services(app_settings, store, hub, recorder, streams), idle_s=600)
    yield manager
    await manager.close_all()


@pytest.fixture
def deleter(app_settings, store, pool, recorder, sessions):
    return Deleter(app_settings, store, MandateStore(pool), recorder, sessions, wait_s=0.3)


def transcript(settings, session_id):
    """The files of an SDK session: its conversation and the folder of its subagents."""
    folder = settings.claude_config_dir / "projects" / "p"
    (folder / session_id / "subagents").mkdir(parents=True)
    (folder / f"{session_id}.jsonl").write_text("{}")
    (folder / session_id / "subagents" / "agent-a1.jsonl").write_text("{}")
    return folder


async def test_delete_a_working_goal(deleter, sessions, store, streams, hub, app_settings):
    goal_id = (await store.create_goal("Find 5 jobs"))["id"]
    await sessions.send(goal_id, "Find 5 jobs", "user")
    streams.last.push(agent_call("call-1", "job-gen"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    await store.set_sdk_session(goal_id, SESSION, 0.1)
    await store.add_scrape(goal_id, "rq-1", "linkedin_person", "https://www.linkedin.com/in/jane/", True)
    folder = transcript(app_settings, SESSION)
    (folder / "other.jsonl").write_text("{}")
    queue = hub.subscribe(goal_id)

    assert await deleter.delete_goal(goal_id) is True

    assert streams.last.closed and sessions.get(goal_id) is None  # the agents stopped first
    assert await store.get_goal(goal_id) is None
    assert sorted(p.name for p in folder.iterdir()) == ["other.jsonl"]
    events = []
    while not queue.empty():
        events.append(queue.get_nowait()["type"])
    # The chat shows no stop: no "Stopped by the user", no cancelled scrapes. An open tab only leaves the chat.
    assert events == ["goal_deleted"]
    assert await deleter.delete_goal(goal_id) is False


async def test_a_goal_that_another_process_holds_is_not_deleted(deleter, store, database_url):
    goal_id = (await store.create_goal("g"))["id"]
    lock = await GoalLock.acquire(database_url, goal_id)
    try:
        with pytest.raises(GoalLocked):
            await deleter.delete_goal(goal_id)
    finally:
        await lock.release()
    assert await store.get_goal(goal_id) is not None


async def test_the_chat_of_a_mandate_is_deleted_with_the_mandate_only(deleter, make_run):
    mandate, _ = await make_run()
    with pytest.raises(MandateChat):
        await deleter.delete_goal(mandate["home_goal_id"])


async def test_delete_a_mandate_with_its_chat_and_its_goals(deleter, store, make_run, pool):
    mandate, first = await make_run(slot_hour=3)
    _, second = await make_run(slot_hour=4, mandate=mandate)
    other = await store.create_goal("not of the schedule")
    assert await deleter.mandate_preview(mandate["id"]) == {
        "goals": 2, "jobs": 0, "notes": 0, "working": True, "status": "active",
        "next_run_at": mandate["next_run_at"]}

    assert await deleter.delete_mandate(mandate["id"]) is True

    assert await MandateStore(pool).get(mandate["id"]) is None
    for goal_id in (mandate["home_goal_id"], first["id"], second["id"]):
        assert await store.get_goal(goal_id) is None
    assert await store.get_goal(other["id"]) is not None
    assert await deleter.delete_mandate(mandate["id"]) is False


async def test_delete_one_goal_of_a_mandate(deleter, store, make_run, pool):
    mandate, run = await make_run()
    await store.add_spend(run["id"], 1.5)
    assert await deleter.delete_goal(run["id"]) is True
    ms = MandateStore(pool)
    assert await ms.get(mandate["id"]) is not None and await ms.runs(mandate["id"]) == []
    assert await ms.total_spend(mandate["id"]) == 1.5
    notes = [e["text"] for e in await store.list_events(mandate["home_goal_id"]) if e["type"] == "notice"]
    assert notes == ["Mission 1 was deleted by the user. Its spend still counts to the total spend limit."]


async def test_a_delete_waits_for_the_scheduler(deleter, store, make_run, database_url):
    mandate, run = await make_run()
    lock = await GoalLock.acquire(database_url, mandate_lock_key(mandate["id"]))  # the scheduler starts a goal
    try:
        with pytest.raises(GoalLocked):
            await deleter.delete_mandate(mandate["id"])  # it waits 0.3 s in this test, then gives up
    finally:
        await lock.release()
    assert await store.get_goal(run["id"]) is not None


# Races: the delete and the other work start at the same time, in one event loop.

async def test_two_deletes_of_one_goal_at_the_same_time(deleter, store):
    goal_id = (await store.create_goal("g"))["id"]
    results = await asyncio.gather(deleter.delete_goal(goal_id), deleter.delete_goal(goal_id), return_exceptions=True)
    assert sorted(results, key=str) == [False, True]  # the second finds nothing: no error
    assert await store.get_goal(goal_id) is None


async def test_a_message_and_a_delete_at_the_same_time(deleter, sessions, store, streams):
    goal_id = (await store.create_goal("g"))["id"]
    await store.set_status(goal_id, "done")
    send, gone = await asyncio.gather(sessions.send(goal_id, "Find 5 more", "follow_up"), deleter.delete_goal(goal_id),
                                      return_exceptions=True)
    assert gone is True and (send is None or isinstance(send, GoalNotFound))
    assert await store.get_goal(goal_id) is None and sessions.get(goal_id) is None
    assert all(client.closed for client in streams.clients)  # no session stays open on a deleted goal
    assert await sessions.reachable(goal_id) is False  # the watcher's next poll leaves it


async def test_a_scheduler_tick_and_a_mandate_delete_at_the_same_time(deleter, sessions, store, pool, recorder,
                                                                      artifacts, make_run):
    from datetime import UTC, datetime

    from job_finder.scheduler import Scheduler

    mandate, run = await make_run()
    await store.set_status(run["id"], "done")  # the mandate is due, and no goal works: the tick starts one
    ms = MandateStore(pool)
    await ms.update(mandate["id"], {"next_run_at": datetime(2020, 1, 1, tzinfo=UTC)})
    tick = Scheduler(store, ms, artifacts, recorder, sessions).tick()
    await asyncio.gather(tick, deleter.delete_mandate(mandate["id"]))
    async with pool.connection() as conn:
        left = await (await conn.execute("SELECT count(*) AS n FROM goals")).fetchone()
    assert left["n"] == 0 and await ms.get(mandate["id"]) is None  # no new goal of the mandate stays behind
    assert sessions.count == 0


async def test_delete_a_goal_that_waits_on_scrapes(deleter, store, app_settings):
    goal_id = (await store.create_goal("g"))["id"]
    await store.set_status(goal_id, "waiting")
    await store.add_scrape(goal_id, "rq-1", "linkedin_person", "https://www.linkedin.com/in/jane/", True)
    assert await deleter.delete_goal(goal_id) is True
    assert await store.pending_scrapes() == []  # the watcher asks CareerLens about it no more


async def test_a_delete_cancels_in_career_lens_what_no_other_goal_waits_on(
        app_settings, store, pool, recorder, sessions, make_run):
    career_lens = Cancels()
    deleter = Deleter(app_settings, store, MandateStore(pool), recorder, sessions, career_lens=career_lens, wait_s=0.3)
    goal_id, other_id = (await store.create_goal("g"))["id"], (await store.create_goal("other"))["id"]
    mandate, run = await make_run(slot_hour=3)
    for owner, request_id in ((goal_id, "rq-own"), (goal_id, "rq-shared"), (other_id, "rq-shared"),
                              (run["id"], "rq-run")):
        await store.add_scrape(owner, request_id, "linkedin_person", f"https://www.linkedin.com/in/{request_id}/",
                               True)
    await store.set_status(goal_id, "waiting")

    await deleter.delete_goal(goal_id)
    assert career_lens.asked == ["rq-own"]  # the other goal still waits on rq-shared
    await deleter.delete_mandate(mandate["id"])
    assert career_lens.asked == ["rq-own", "rq-run"]


async def test_two_deletes_of_one_goal_of_a_mandate_write_one_notice(deleter, store, make_run):
    mandate, run = await make_run()
    await asyncio.gather(deleter.delete_goal(run["id"]), deleter.delete_goal(run["id"]))
    notes = [e for e in await store.list_events(mandate["home_goal_id"]) if e.get("trigger") == "mandate_run_deleted"]
    assert len(notes) == 1


async def test_a_refused_delete_stops_nothing(deleter, sessions, store, streams, make_run, database_url):
    mandate, working = await make_run(slot_hour=3)
    _, held = await make_run(slot_hour=4, mandate=mandate)
    await sessions.send(working["id"], "Find jobs", "mandate_run", user=False)  # it works in this process
    lock = await GoalLock.acquire(database_url, held["id"])  # a terminal has the other goal open
    try:
        with pytest.raises(GoalLocked):
            await deleter.delete_mandate(mandate["id"])
    finally:
        await lock.release()
    assert sessions.get(working["id"]) is not None and not streams.last.closed  # it still works
    assert await store.get_goal(working["id"]) is not None
