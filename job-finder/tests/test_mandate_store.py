from datetime import UTC, datetime, time

import pytest

from job_finder.mandate_store import MandateStore

from .fakes import roles

NOW = datetime(2026, 10, 8, 3, 30, tzinfo=UTC)  # Thu 8 Oct 2026 09:00 in Asia/Kolkata
VALUES = {"title": "EU fintech jobs", "instruction": "Series B fintechs in the EU", "every_unit": "day", "every_count": 1,
          "jobs_per_run": 20, "window_days": [1, 2, 3, 4, 5], "window_start": time(9), "window_end": None,
          "timezone": "Asia/Kolkata", "max_spend_per_run": 2.0, "max_scrapes_per_run": 15,
          "max_total_spend": None, "ends_on": None, "profile": None, "profile_ready": False, "next_run_at": NOW}


@pytest.fixture
def mandates(pool) -> MandateStore:
    return MandateStore(pool)


async def new_mandate(store, mandates, **values):
    home = await store.create_goal("Find EU fintech jobs every weekday")
    return await mandates.create(home["id"], {**VALUES, **values})


async def test_create_and_read_a_mandate(store, mandates):
    m = await new_mandate(store, mandates)
    assert m["status"] == "active" and m["window_start"] == time(9) and m["max_spend_per_run"] == 2.0
    assert await mandates.get(m["id"]) == m == await mandates.by_home(m["home_goal_id"])
    assert (await store.get_goal(m["home_goal_id"]))["home_of"] == m["id"]
    assert [row["id"] for row in await mandates.list()] == [m["id"]]
    assert await mandates.by_prefix(m["id"][:8]) == [m["id"]]


async def test_update_changes_only_the_given_fields(store, mandates):
    m = await new_mandate(store, mandates)
    changed = await mandates.update(m["id"], {"jobs_per_run": 100})
    assert changed["jobs_per_run"] == 100 and changed["max_spend_per_run"] == 2.0
    with pytest.raises(ValueError, match="home_goal_id"):
        await mandates.update(m["id"], {"home_goal_id": m["id"]})


async def test_due_lists_only_active_mandates_whose_time_has_come(store, mandates):
    m = await new_mandate(store, mandates)
    assert [row["id"] for row in await mandates.due(NOW)] == [m["id"]]
    await mandates.update(m["id"], {"status": "paused"})
    assert await mandates.due(NOW) == []


async def test_one_run_for_each_slot(store, mandates):
    m = await new_mandate(store, mandates)
    run = await mandates.create_run(m, NOW, 1.5, 1)
    assert run["timezone"] == "Asia/Kolkata"  # the times of the run show in the zone of the mandate
    assert run["mandate_id"] == m["id"] and run["run_slot"] == NOW and run["status"] == "running"
    assert run["max_spend_usd"] == 1.5 and run["max_fresh_scrapes"] == 15
    assert run["brief"] == {"title": "EU fintech jobs", "criteria": "Series B fintechs in the EU",
                            "target_count": 20}
    assert run["title"] == "EU fintech jobs: mission 1"
    assert (run["time_limit_s"], run["grace_s"], run["soft_limit_at"]) == (120 * 60, 30 * 60, None)
    assert await mandates.create_run(m, NOW, 1.5, 1) is None
    assert await mandates.has_running_run(m["id"])
    await store.add_spend(run["id"], 0.75)
    assert await mandates.total_spend(m["id"]) == 0.75
    [row] = await mandates.runs(m["id"])
    assert row["id"] == run["id"] and row["number"] == 1 and row["spend_usd"] == 0.75


async def test_a_goal_that_the_user_starts_has_no_time_limit(store):
    goal = await store.create_goal("Find 5 jobs")
    assert (goal["time_limit_s"], goal["grace_s"], goal["soft_limit_at"]) == (None, None, None)


async def test_a_new_time_limit_reaches_only_a_run_in_progress_before_its_limit(store, mandates):
    m = await new_mandate(store, mandates, time_limit_minutes=60, grace_minutes=5)
    done = await mandates.create_run(m, NOW, 2.0, 1)
    await store.set_status(done["id"], "capped")
    late = await mandates.create_run(m, datetime(2026, 10, 9, 3, 30, tzinfo=UTC), 2.0, 2)
    await store.set_soft_limit(late["id"])  # it works in its extra time: Continue gives it the new limit later
    live = await mandates.create_run(m, datetime(2026, 10, 10, 3, 30, tzinfo=UTC), 2.0, 3)
    await mandates.set_run_time_limit(m["id"], 180, 10)
    assert ((await store.get_goal(live["id"]))["time_limit_s"], (await store.get_goal(live["id"]))["grace_s"]) == (
        180 * 60, 10 * 60)
    assert (await store.get_goal(late["id"]))["time_limit_s"] == 60 * 60
    assert (await store.get_goal(done["id"]))["time_limit_s"] == 60 * 60


async def test_quota_change_reaches_the_run_in_progress_only(store, mandates):
    m = await new_mandate(store, mandates)
    done = await mandates.create_run(m, NOW, 2.0, 1)
    await store.set_status(done["id"], "done")
    live = await mandates.create_run(m, datetime(2026, 10, 9, 3, 30, tzinfo=UTC), 2.0, 2)
    await mandates.set_run_target(m["id"], 50)
    assert (await store.get_goal(live["id"]))["brief"]["target_count"] == 50
    assert (await store.get_goal(done["id"]))["brief"]["target_count"] == 20


async def test_settled_runs_are_announced_once(store, mandates):
    m = await new_mandate(store, mandates)
    run = await mandates.create_run(m, NOW, 2.0, 1)
    assert await mandates.unannounced_runs() == []
    await store.set_status(run["id"], "done")
    [row] = await mandates.unannounced_runs()
    assert row["id"] == run["id"] and row["home_goal_id"] == m["home_goal_id"] and row["number"] == 1
    await mandates.mark_announced(run["id"])
    assert await mandates.unannounced_runs() == []


async def test_history_sums_the_earlier_runs(store, mandates, artifacts):
    m = await new_mandate(store, mandates)
    run = await mandates.create_run(m, NOW, 2.0, 1)
    rows = await artifacts.save_jobs(run["id"], roles(*"ABCDE"), "job-discovery")
    await artifacts.save_job_research(run["id"], rows[0]["job_id"], None, "snippet", None)
    await artifacts.set_fit(run["id"], rows[0]["job_id"], "fit", "Good.", [])
    for n, row in enumerate(rows[1:], start=1):
        await artifacts.set_fit(run["id"], row["job_id"], "not_fit", f"Reason {n}.", [])
    await artifacts.save_notes(run["id"], "job-discovery", [{"kind": "note", "text": "I used Pune searches.", "job_id": None}])
    # Only the 3 newest reasons. The notes of earlier goals are not in the kick-off message.
    assert await mandates.history(m["id"]) == {"runs": 1, "jobs": 5, "fit": 1, "not_fit": 4, "unsure": 0,
                                               "reasons": ["Reason 4.", "Reason 3.", "Reason 2."]}


async def test_a_deleted_goal_keeps_its_number_and_its_spend(store, mandates, pool):
    m = await new_mandate(store, mandates)
    first = await mandates.create_run(m, NOW, 2.0, 1)
    second = await mandates.create_run(await mandates.get(m["id"]), datetime(2026, 10, 9, 3, 30, tzinfo=UTC), 2.0, 2)
    await store.add_spend(first["id"], 1.25)
    await store.add_spend(second["id"], 0.5)
    assert (await mandates.get(m["id"]))["goals_started"] == 2
    await store.delete_goals([first["id"]])
    # The spend of the deleted goal still counts to the total limit, and the numbers do not move or repeat.
    assert await mandates.total_spend(m["id"]) == 1.75
    [row] = await mandates.runs(m["id"])
    assert row["id"] == second["id"] and row["number"] == 2
    assert (await mandates.get(m["id"]))["goals_started"] == 2


async def test_a_goal_of_a_mandate_takes_the_model_of_the_mandate_chat(store, mandates):
    m = await new_mandate(store, mandates)
    await store.set_model(m["home_goal_id"], "claude-opus-5-5", "xhigh")
    run = await mandates.create_run(m, NOW, 2.0, 1)
    assert (run["model"], run["effort"]) == ("claude-opus-5-5", "xhigh")
