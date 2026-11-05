from datetime import UTC, date, datetime, time, timedelta

import pytest

from job_finder.mandate_store import MandateStore
from job_finder.scheduler import Scheduler

from .fakes import roles

MON_0900 = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


class Port:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, goal_id, text, trigger, *, user=True, recover=True):
        if self.fail:
            raise RuntimeError("Claude is not connected")
        self.sent.append((goal_id, text, trigger, user))
        self.recover = recover


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def mandates(pool):
    return MandateStore(pool)


@pytest.fixture
def clock():
    return Clock(MON_0900)


def scheduler(store, mandates, artifacts, recorder, port, clock):
    return Scheduler(store, mandates, artifacts, recorder, port, now=clock)


async def new(store, mandates, **values):
    home = await store.create_goal("home")
    return await mandates.create(home["id"], {
        "title": "EU fintech jobs", "instruction": "fintech", "every_unit": "day", "every_count": 1,
        "jobs_per_run": 20,
        "window_days": [1, 2, 3, 4, 5], "window_start": time(9), "timezone": "UTC", "max_spend_per_run": 2.0,
        "max_scrapes_per_run": 15, "next_run_at": MON_0900, **values})


async def test_a_due_mandate_starts_one_run_and_moves_to_the_next_slot(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    [run] = await mandates.runs(m["id"])
    [(goal_id, text, trigger, user)] = port.sent
    assert goal_id == run["id"] and trigger == "mandate_run" and not user
    assert port.recover is False  # a new run is not a goal that an app restart interrupted
    assert text.startswith('This is goal 1 of the mandate "EU fintech jobs"')
    assert (await mandates.get(m["id"]))["next_run_at"] == MON_0900 + timedelta(days=1)
    await s.tick()  # not due again until tomorrow
    assert len(port.sent) == 1


async def test_a_running_run_holds_the_next_one(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    clock.now += timedelta(days=1)
    await s.tick()
    assert len(port.sent) == 1  # run 1 is still running
    [run] = await mandates.runs(m["id"])
    await store.set_status(run["id"], "waiting")  # waiting for scrapes holds it too
    await s.tick()
    assert len(port.sent) == 1
    await store.set_status(run["id"], "done")
    await s.tick()
    assert len(port.sent) == 2


async def test_after_a_long_stop_only_one_run_starts(store, mandates, artifacts, recorder, clock):
    # Review focus 2: the server was down from Monday to Thursday 10:00.
    m = await new(store, mandates)
    clock.now = datetime(2026, 10, 15, 10, 0, tzinfo=UTC)
    port = Port()
    await scheduler(store, mandates, artifacts, recorder, port, clock).tick()
    assert len(port.sent) == 1
    assert (await mandates.get(m["id"]))["next_run_at"] == datetime(2026, 10, 16, 9, 0, tzinfo=UTC)


async def test_a_run_that_cannot_start_fails(store, mandates, artifacts, recorder, clock):
    # Review focus 1
    m = await new(store, mandates)
    await scheduler(store, mandates, artifacts, recorder, Port(fail=True), clock).tick()
    [run] = await mandates.runs(m["id"])
    goal = await store.get_goal(run["id"])
    assert goal["status"] == "failed" and "Claude is not connected" in goal["status_reason"]
    assert not await mandates.has_running_run(m["id"])


async def test_the_total_spend_limit_ends_the_mandate(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, max_total_spend=3.0)
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    [run] = await mandates.runs(m["id"])
    await store.add_spend(run["id"], 2.0)
    await store.set_status(run["id"], "done")
    clock.now += timedelta(days=1)
    await s.tick()  # $1.00 is left: the second run gets a $1.00 cap
    second = (await mandates.runs(m["id"]))[1]
    assert (await store.get_goal(second["id"]))["max_spend_usd"] == 1.0
    await store.add_spend(second["id"], 0.9)
    await store.set_status(second["id"], "done")
    clock.now += timedelta(days=1)
    await s.tick()  # $0.10 is left: less than the wrap-up reserve
    assert (await mandates.get(m["id"]))["status"] == "ended" and len(port.sent) == 2


async def test_the_end_date_ends_the_mandate(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, ends_on=date(2026, 10, 11))
    await scheduler(store, mandates, artifacts, recorder, Port(), clock).tick()
    assert (await mandates.get(m["id"]))["status"] == "ended"
    notes = [e["text"] for e in await store.list_events(m["home_goal_id"]) if e["type"] == "notice"]
    assert notes == ["Mandate ended: the end date passed."]


async def test_a_settled_run_sends_one_notice_to_the_home_chat(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    [run] = await mandates.runs(m["id"])
    await artifacts.save_jobs(run["id"], roles("A"), "job-discovery")
    await store.add_spend(run["id"], 1.84)
    await store.set_status(run["id"], "done")
    await s.tick()
    await s.tick()
    texts = [e["text"] for e in await store.list_events(m["home_goal_id"])
             if e["type"] == "notice" and e.get("trigger") == "mandate_run_finished"]
    assert texts == [f"Goal 1, Mon 12 Oct 2026 09:00: 0 fit of 1 found, $1.84. Status: done. Goal id: {run['id']}."]


async def test_a_run_that_waits_on_scrapes_keeps_the_next_run_from_starting(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, every_unit="hour", every_count=1)
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    [run] = await mandates.runs(m["id"])
    await store.set_status(run["id"], "waiting", "Waiting on scrapes")
    clock.now = MON_0900 + timedelta(hours=1)
    await s.tick()
    assert len(await mandates.runs(m["id"])) == 1 and len(port.sent) == 1


async def test_a_slot_that_waits_for_a_run_in_progress_is_told_once_in_the_home_chat(
        store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, every_unit="hour", every_count=1)
    s = scheduler(store, mandates, artifacts, recorder, Port(), clock)
    await s.tick()
    clock.now = MON_0900 + timedelta(hours=1)
    await s.tick()
    await s.tick()
    rows = await store.list_events(m["home_goal_id"])
    waits = [e for e in rows if e["type"] == "notice" and e.get("trigger") == "mandate_run_waiting"]
    assert len(waits) == 1
    assert "Goal 1 is still in progress" in waits[0]["text"]


async def test_a_late_hourly_goal_starts_once_for_the_newest_missed_time(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, every_unit="hour", every_count=1, window_end=time(18))
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()  # goal 1 at 09:00
    [run] = await mandates.runs(m["id"])
    await store.set_status(run["id"], "done")
    clock.now = MON_0900 + timedelta(hours=2, minutes=40)  # the server was off: 10:00 and 11:00 were missed
    await s.tick()
    runs = await mandates.runs(m["id"])
    assert [r["run_slot"] for r in runs] == [MON_0900, MON_0900 + timedelta(hours=2)] and len(port.sent) == 2
    assert (await mandates.get(m["id"]))["next_run_at"] == MON_0900 + timedelta(hours=3)


async def test_no_late_goal_after_no_goals_after(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, every_unit="hour", every_count=1, window_end=time(18))
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s.tick()
    [run] = await mandates.runs(m["id"])
    clock.now = MON_0900 + timedelta(hours=9, minutes=30)  # 18:30: goal 1 held the goals of the day
    await store.set_status(run["id"], "done")
    await s.tick()
    assert len(port.sent) == 1
    assert (await mandates.get(m["id"]))["next_run_at"] == MON_0900 + timedelta(days=1)


async def test_a_change_after_the_scheduler_read_the_mandate_wins(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    [seen] = await mandates.due(clock.now)
    await mandates.update(m["id"], {"status": "paused"})  # the user pauses while the tick works
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s._start(seen, clock.now)
    assert port.sent == [] and await mandates.runs(m["id"]) == []
    assert (await mandates.get(m["id"]))["status"] == "paused"


async def test_a_settings_change_after_the_read_is_used_by_the_next_tick(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    [seen] = await mandates.due(clock.now)
    await mandates.update(m["id"], {"jobs_per_run": 5})
    port = Port()
    s = scheduler(store, mandates, artifacts, recorder, port, clock)
    await s._start(seen, clock.now)
    assert port.sent == []
    await s.tick()
    [run] = await mandates.runs(m["id"])
    assert (await store.get_goal(run["id"]))["brief"]["target_count"] == 5


async def test_a_mandate_that_a_delete_holds_starts_no_goal(store, mandates, artifacts, recorder, clock, database_url):
    from job_finder.db import GoalLock

    m = await new(store, mandates)
    port = Port()
    lock = await GoalLock.acquire(database_url, f"mandate:{m['id']}")  # a delete works on the mandate
    try:
        await scheduler(store, mandates, artifacts, recorder, port, clock).tick()
    finally:
        await lock.release()
    assert port.sent == [] and await mandates.runs(m["id"]) == []
    await scheduler(store, mandates, artifacts, recorder, port, clock).tick()  # the next tick starts it
    assert len(port.sent) == 1


async def test_the_numbers_go_on_after_a_deleted_goal(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    s = scheduler(store, mandates, artifacts, recorder, Port(), clock)
    await s.tick()
    [first] = await mandates.runs(m["id"])
    await store.set_status(first["id"], "done")
    await store.delete_goals([first["id"]])
    clock.now += timedelta(days=1)
    await s.tick()
    [second] = await mandates.runs(m["id"])
    assert second["number"] == 2 and second["title"] == "EU fintech jobs: goal 2"


async def fail_days(s, clock, days):
    """One tick each day: it announces the goal before, then starts a goal that fails."""
    for _ in range(days):
        await s.tick()
        clock.now += timedelta(days=1)


def pause_notices(events):
    return [e["text"] for e in events if e["type"] == "notice" and e.get("trigger") == "mandate_paused"]


async def test_failed_goals_in_a_row_pause_the_mandate(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    port = Port(fail=True)
    s = Scheduler(store, mandates, artifacts, recorder, port, now=clock, max_failures=3)
    await fail_days(s, clock, 3)
    assert (await mandates.get(m["id"]))["status"] == "active"  # the third failure is not announced yet
    await s.tick()
    assert (await mandates.get(m["id"]))["status"] == "paused" and len(await mandates.runs(m["id"])) == 3
    [text] = pause_notices(await store.list_events(m["home_goal_id"]))
    assert text.startswith("Mandate paused: the last 3 goals failed.") and "Claude is not connected" in text
    assert "ask here in this chat" in text


async def test_a_goal_that_does_not_fail_ends_the_row(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    port = Port(fail=True)
    s = Scheduler(store, mandates, artifacts, recorder, port, now=clock, max_failures=3)
    await fail_days(s, clock, 2)
    port.fail = False
    await fail_days(s, clock, 1)
    [*_, ok] = await mandates.runs(m["id"])
    await store.set_status(ok["id"], "done")
    port.fail = True
    await fail_days(s, clock, 3)
    assert (await mandates.get(m["id"]))["status"] == "active"  # 2 failed, 1 done, 2 failed and 1 not yet announced


async def test_after_a_resume_the_mandate_pauses_again_only_after_as_many_new_failures(
        store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates, window_days=[1, 2, 3, 4, 5, 6, 7])
    s = Scheduler(store, mandates, artifacts, recorder, Port(fail=True), now=clock, max_failures=2)
    await fail_days(s, clock, 3)
    assert (await mandates.get(m["id"]))["status"] == "paused"
    await mandates.update(m["id"], {"status": "active", "next_run_at": clock.now})
    await fail_days(s, clock, 2)
    assert (await mandates.get(m["id"]))["status"] == "active"  # 3 failed in a row: not a multiple of 2
    await fail_days(s, clock, 1)
    assert (await mandates.get(m["id"]))["status"] == "paused"  # 4 failed in a row
    assert len(pause_notices(await store.list_events(m["home_goal_id"]))) == 2


async def test_zero_failures_never_pauses_the_mandate(store, mandates, artifacts, recorder, clock):
    m = await new(store, mandates)
    s = Scheduler(store, mandates, artifacts, recorder, Port(fail=True), now=clock, max_failures=0)
    await fail_days(s, clock, 5)
    assert (await mandates.get(m["id"]))["status"] == "active"
