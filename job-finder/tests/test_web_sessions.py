import pytest

from job_finder.db import GoalLock
from job_finder.services import GoalLocked, GoalNotFound
from job_finder.web.sessions import GoalSessions

from .fakes import Streams, agent_call, make_services, replies, result, task_started


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def streams():
    return Streams(replies("On it."))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def sessions(app_settings, store, hub, recorder, streams, clock):
    manager = GoalSessions(make_services(app_settings, store, hub, recorder, streams), idle_s=600, clock=clock)
    yield manager
    await manager.close_all()


async def idle_goal(store, title="g"):
    goal = await store.create_goal(title)
    await store.set_status(goal["id"], "done")
    return goal["id"]


async def test_send_opens_the_session_once(sessions, store, streams):
    goal_id = await idle_goal(store)
    assert sessions.get(goal_id) is None
    await sessions.send(goal_id, "Find 5 jobs", "user")
    await sessions.send(goal_id, "Skip Germany", "follow_up")
    assert len(streams.clients) == 1
    assert streams.last.queries == ["Find 5 jobs", "Skip Germany"]
    assert sessions.get(goal_id) is not None


async def test_an_idle_session_closes_and_the_next_message_reopens_it(sessions, store, streams, clock,
                                                                    app_settings):
    folder = app_settings.claude_config_dir / "projects" / "p"
    folder.mkdir(parents=True)
    (folder / "sess-1.jsonl").write_text("")  # the SDK's saved conversation
    goal_id = await idle_goal(store)
    await sessions.send(goal_id, "Find 5 jobs", "user")
    await streams.last.flush()
    clock.now += 599
    await sessions.reap()
    assert sessions.get(goal_id) is not None
    clock.now += 2
    await sessions.reap()
    assert sessions.get(goal_id) is None and streams.last.closed
    await sessions.send(goal_id, "And 5 more", "follow_up")
    assert len(streams.clients) == 2
    assert streams.last.options.resume == "sess-1"  # the SDK resumes the same conversation
    assert streams.last.queries == ["And 5 more"]


async def test_a_refreshed_goal_opens_a_new_session_on_the_next_message(sessions, store, streams):
    goal_id = await idle_goal(store)
    await sessions.send(goal_id, "Find 5 jobs", "user")
    await streams.last.flush()
    await store.set_profile(goal_id, "Go engineer in Pune.", ready=False)
    sessions.refresh(goal_id)
    await sessions.send(goal_id, "Write notes", "follow_up")
    assert len(streams.clients) == 2 and streams.clients[0].closed
    assert "Go engineer in Pune." not in streams.last.options.system_prompt  # only two subagents read the profile
    await sessions.send(goal_id, "Thanks", "follow_up")
    assert len(streams.clients) == 2  # no change since: the same session


async def test_a_session_with_a_running_subagent_stays_open(sessions, store, streams, clock):
    goal_id = await idle_goal(store)
    await sessions.send(goal_id, "Find 5 jobs", "user")
    streams.last.push(agent_call("call-1", "job-discovery"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    clock.now += 3600
    await sessions.reap()
    assert sessions.get(goal_id) is not None
    assert await sessions.ready(goal_id) is False
    assert await sessions.reachable(goal_id) is True  # busy, but a subagent note can still go in line
    assert sessions.is_running(goal_id, "a1") and not sessions.is_running(goal_id, "other")
    assert sessions.type_busy(goal_id, "job-discovery") and not sessions.type_busy(goal_id, "job-research")
    assert not sessions.type_busy("no-such-goal", "job-discovery")


async def test_a_goal_that_another_process_holds_is_refused(sessions, store, database_url):
    goal_id = await idle_goal(store)
    lock = await GoalLock.acquire(database_url, goal_id)
    try:
        with pytest.raises(GoalLocked):
            await sessions.send(goal_id, "hi", "follow_up")
        assert await sessions.ready(goal_id) is False  # the watcher leaves it to the other process
        assert await sessions.reachable(goal_id) is False
    finally:
        await lock.release()


async def test_ready_opens_a_closed_goal_for_the_watcher(sessions, store, streams):
    goal_id = await idle_goal(store)
    assert await sessions.ready(goal_id) is True
    assert sessions.get(goal_id) is not None
    await sessions.start(goal_id, "Scrapes finished: ...", "scrapes_finished")
    assert streams.last.queries == ["Scrapes finished: ..."]


async def test_stop_works_on_a_closed_goal(sessions, store):
    goal_id = await idle_goal(store)
    await store.set_status(goal_id, "waiting")
    await store.add_scrape(goal_id, "rq-1", "linkedin_person", "https://www.linkedin.com/in/jane/", True)
    await sessions.stop(goal_id)
    goal = await store.get_goal(goal_id)
    assert goal["status"] == "stopped"
    assert await store.count_pending(goal_id) == 0


async def test_recover_reopens_goals_that_were_running(sessions, store, streams):
    running = (await store.create_goal("was running"))["id"]  # created as running
    done = await idle_goal(store, "done")
    await sessions.recover()
    assert sessions.get(running) is not None and sessions.get(done) is None
    assert streams.last.queries[0].startswith("You were interrupted by an app restart.")


async def test_recover_tries_again_when_the_old_process_still_holds_the_goal(sessions, store, database_url):
    goal_id = (await store.create_goal("was running"))["id"]
    lock = await GoalLock.acquire(database_url, goal_id)  # the old server process has not stopped yet
    await sessions.recover()
    assert sessions.get(goal_id) is None
    await sessions.reap()
    assert sessions.get(goal_id) is None
    await lock.release()
    await sessions.reap()
    assert sessions.get(goal_id) is not None


async def test_close_all_releases_the_locks(sessions, store, database_url):
    goal_id = await idle_goal(store)
    await sessions.send(goal_id, "hi", "user")
    await sessions.close_all()
    lock = await GoalLock.acquire(database_url, goal_id)
    assert lock is not None
    await lock.release()


async def test_a_goal_that_became_a_home_chat_reopens_with_the_home_tools(sessions, store, streams, pool):
    from datetime import UTC, datetime, time

    from job_finder.mandate_store import MandateStore
    goal_id = await idle_goal(store)
    await sessions.send(goal_id, "Find 5 jobs every weekday", "user")
    await streams.last.flush()
    await MandateStore(pool).create(goal_id, {"title": "t", "instruction": "i", "every_unit": "day", "every_count": 1,
                                              "jobs_per_run": 5, "window_days": [1], "window_start": time(9),
                                              "timezone": "UTC", "max_spend_per_run": 1, "max_scrapes_per_run": 1,
                                              "next_run_at": datetime.now(UTC)})
    sessions.get(goal_id).needs_reopen = True  # what create_mandate does through on_change
    await sessions.send(goal_id, "Make it 10", "follow_up")
    assert len(streams.clients) == 2 and streams.last.options.tools == ["Agent"]
    assert list(streams.last.options.agents) == ["candidate-profile"]  # the chat of a mandate runs only this subagent


async def test_a_deleted_goal_does_not_open(sessions, store):
    goal_id = await idle_goal(store)
    await store.delete_goals([goal_id])
    with pytest.raises(GoalNotFound):
        await sessions.send(goal_id, "hi", "follow_up")
    assert await sessions.reachable(goal_id) is False  # the watcher leaves it
    assert sessions.get(goal_id) is None


async def test_the_time_limit_opens_a_closed_goal_and_gives_it_its_step(sessions, store, streams, make_run):
    _, run = await make_run()
    await store.set_status(run["id"], "waiting")
    await sessions.time_limit(run["id"], False)
    assert sessions.get(run["id"]) is not None
    assert (await store.get_goal(run["id"]))["soft_limit_at"] is not None
    assert streams.last.queries[-1].startswith("Time limit reached (2h).")


async def test_the_time_limit_skips_a_goal_that_another_process_holds(sessions, store, make_run, database_url):
    _, run = await make_run()
    lock = await GoalLock.acquire(database_url, run["id"])
    try:
        await sessions.time_limit(run["id"], True)  # the next check tries again
    finally:
        await lock.release()
    assert sessions.get(run["id"]) is None and (await store.get_goal(run["id"]))["soft_limit_at"] is None
