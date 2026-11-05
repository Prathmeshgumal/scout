import pytest

from job_finder.db import GoalLock
from job_finder.services import GoalLocked, SessionPort

from .fakes import Streams, make_services, replies


async def test_lock_is_exclusive_until_released(database_url, store):
    goal = await store.create_goal("g")
    lock = await GoalLock.acquire(database_url, goal["id"])
    assert lock is not None
    assert await GoalLock.acquire(database_url, goal["id"]) is None
    await lock.release()
    again = await GoalLock.acquire(database_url, goal["id"])
    assert again is not None
    await again.release()


@pytest.fixture
def streams():
    return Streams(replies("On it."))


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


async def test_second_drive_is_refused_and_the_lock_frees_after_an_error(services, store):
    goal = await store.create_goal("g")
    await store.set_status(goal["id"], "done")
    async with services.drive(goal["id"]):
        with pytest.raises(GoalLocked, match="open in another terminal or in the web app"):
            async with services.drive(goal["id"]):
                pass
    with pytest.raises(RuntimeError):
        async with services.drive(goal["id"]):
            raise RuntimeError("boom")
    async with services.drive(goal["id"]):
        pass


async def test_drive_tells_a_running_goal_that_it_was_interrupted(services, store, streams):
    goal = await store.create_goal("g")  # status "running": its process stopped while it worked
    async with services.drive(goal["id"]) as session:
        await streams.last.flush()
        assert (await session.settled())["status"] == "done"
    assert streams.last.queries[0].startswith("You were interrupted by an app restart.")


async def test_drive_without_recovery_sends_nothing(services, store, streams):
    goal = await store.create_goal("g")
    async with services.drive(goal["id"], recover=False):
        pass
    assert streams.last.queries == [] and streams.last.closed


async def test_the_watcher_port_gives_the_session_a_message(services, store, streams):
    goal = await store.create_goal("g")
    async with services.drive(goal["id"], recover=False) as session:
        port = SessionPort(session)
        assert await port.ready(goal["id"]) is True
        await port.start(goal["id"], "Scrapes finished: ...", "scrapes_finished")
        assert streams.last.queries == ["Scrapes finished: ..."]


async def test_the_watcher_port_knows_which_subagents_run(services, store, streams):
    goal = await store.create_goal("g")
    async with services.drive(goal["id"], recover=False) as session:
        port = SessionPort(session)
        assert port.is_running(goal["id"], "a7f") is False
        session.tasks["a7f"] = {"task_id": "a7f", "name": "research"}
        assert port.is_running(goal["id"], "a7f") is True
        assert port.type_busy(goal["id"], "research") is True
        assert port.type_busy(goal["id"], "fit-check") is False
