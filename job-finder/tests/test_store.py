from datetime import UTC, datetime, timedelta

import pytest

from job_finder.store import goal_title

from .fakes import roles


async def test_create_and_get_goal(store):
    goal = await store.create_goal("Find the VP of Sales at Acme")
    assert goal["status"] == "running"
    assert goal["sdk_session_id"] is None
    again = await store.get_goal(goal["id"])
    assert again["title"] == "Find the VP of Sales at Acme"


async def test_get_unknown_goal_is_none(store):
    assert await store.get_goal("00000000-0000-0000-0000-000000000000") is None


async def test_events_are_numbered_per_goal_and_listed_after_n(store):
    a = await store.create_goal("a")
    b = await store.create_goal("b")
    first = await store.append_event(a["id"], "user_message", {"text": "hi"})
    await store.append_event(b["id"], "user_message", {"text": "other goal"})
    second = await store.append_event(a["id"], "text", {"text": "hello"})
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["type"] == "user_message" and first["text"] == "hi"
    later = await store.list_events(a["id"], after=1)
    assert [e["seq"] for e in later] == [2]
    assert later[0]["text"] == "hello"


async def test_set_status_returns_previous(store):
    goal = await store.create_goal("g")
    assert await store.set_status(goal["id"], "waiting", "Waiting on 1 scrape") == "running"
    fresh = await store.get_goal(goal["id"])
    assert (fresh["status"], fresh["status_reason"]) == ("waiting", "Waiting on 1 scrape")


async def test_list_goals_counts_pending_scrapes(store):
    goal = await store.create_goal("g")
    await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "https://x/in/a/", True)
    [row] = await store.list_goals()
    assert row["pending_scrapes"] == 1


async def test_scrape_lifecycle_and_resume_readiness(store):
    goal = await store.create_goal("g")
    scrape = await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "https://x/in/a/", True)
    await store.set_status(goal["id"], "waiting")
    assert await store.goals_ready_to_resume() == []
    when = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    await store.update_scrape_progress(scrape["id"], "queued", "account off shift", when)
    [pending] = await store.pending_scrapes()
    assert pending["wait_reason"] == "account off shift" and pending["next_attempt_at"] == when
    await store.finish_scrape(scrape["id"], "succeeded", None)
    assert await store.count_pending(goal["id"]) == 0
    assert await store.goals_ready_to_resume() == [goal["id"]]
    reported = await store.take_unreported(goal["id"])
    assert [(r["request_id"], r["status"]) for r in reported] == [("rq-1", "succeeded")]
    assert await store.take_unreported(goal["id"]) == []  # only reported once


async def test_a_goal_keeps_each_of_its_sessions(store):
    goal = await store.create_goal("g")
    await store.set_sdk_session(goal["id"], "sess-1", 0.1)
    await store.set_sdk_session(goal["id"], "sess-1", 0.2)  # the same session: nothing to keep
    await store.set_sdk_session(goal["id"], "sess-2", 0.0)
    row = await store._one("SELECT sdk_session_id, past_session_ids FROM goals WHERE id = %s", (goal["id"],))
    assert (row["sdk_session_id"], row["past_session_ids"]) == ("sess-2", ["sess-1"])


async def test_count_unreported_finished_scrapes(store):
    goal = await store.create_goal("g")
    scrape = await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "u", True)
    assert await store.count_unreported(goal["id"]) == 0  # still pending
    await store.finish_scrape(scrape["id"], "succeeded", None)
    assert await store.count_unreported(goal["id"]) == 1
    await store.take_unreported(goal["id"])
    assert await store.count_unreported(goal["id"]) == 0


async def backdate(store, request_id, hours):
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE scrapes SET submitted_at = now() - make_interval(hours => %s) "
                           "WHERE request_id = %s", (hours, request_id))


async def test_create_goal_uses_given_caps(store):
    goal = await store.create_goal("g", max_fresh_scrapes=2, max_spend_usd=0.5)
    assert (goal["max_fresh_scrapes"], goal["max_spend_usd"], goal["spend_usd"]) == (2, 0.5, 0.0)


async def test_usage_counts_capped_scrapes_and_spend(store):
    gid = (await store.create_goal("g"))["id"]
    await store.add_scrape(gid, "rq-1", "linkedin_company", "u", True)
    await store.add_scrape(gid, "rq-2", "google_search", "q", False)
    await store.add_spend(gid, 0.6)
    assert await store.usage(gid) == {"fresh_scrapes": 1, "spend_usd": pytest.approx(0.6)}


async def test_cancel_pending_scrapes_marks_them_reported(store):
    gid = (await store.create_goal("g"))["id"]
    await store.add_scrape(gid, "rq-1", "linkedin_company", "u1", True)
    other = await store.add_scrape(gid, "rq-2", "linkedin_company", "u2", True)
    await store.finish_scrape(other["id"], "succeeded", None)
    assert [r["request_id"] for r in await store.cancel_pending_scrapes(gid)] == ["rq-1"]
    assert await store.count_pending(gid) == 0
    assert [s["request_id"] for s in await store.take_unreported(gid)] == ["rq-2"]


async def test_expire_overdue_only_expires_waiting_goals_with_an_old_scrape(store):
    old = (await store.create_goal("old"))["id"]
    await store.add_scrape(old, "rq-old", "linkedin_company", "u", True)
    await store.add_scrape(old, "rq-new", "linkedin_company", "u", True)
    await backdate(store, "rq-old", 73)
    await store.set_status(old, "waiting")
    fresh = (await store.create_goal("fresh"))["id"]
    await store.add_scrape(fresh, "rq-fresh", "linkedin_company", "u", True)
    await store.set_status(fresh, "waiting")
    running = (await store.create_goal("running"))["id"]  # a follow-up is running: leave it alone
    await store.add_scrape(running, "rq-run", "linkedin_company", "u", True)
    await backdate(store, "rq-run", 73)
    expired = await store.expire_overdue(timedelta(hours=72))
    assert sorted((r["goal_id"], r["request_id"]) for r in expired) == [(old, "rq-new"), (old, "rq-old")]
    assert await store.count_pending(fresh) == 1 and await store.count_pending(running) == 1
    assert {s["status"] for s in await store.list_scrapes(old)} == {"expired"}


async def test_first_user_message(store, recorder):
    gid = (await store.create_goal("g"))["id"]
    assert await store.first_user_message(gid) is None
    await recorder.record(gid, "user_message", text="first")
    await recorder.record(gid, "user_message", text="second")
    assert await store.first_user_message(gid) == "first"


async def test_add_spend_ignores_negative_amounts(store):
    goal = await store.create_goal("g")
    assert await store.add_spend(goal["id"], 0.25) == pytest.approx(0.25)
    assert await store.add_spend(goal["id"], -0.1) == pytest.approx(0.25)
    assert (await store.get_goal(goal["id"]))["spend_usd"] == pytest.approx(0.25)


async def test_task_lifecycle(store):
    goal = await store.create_goal("g")
    await store.start_task(goal["id"], "a1", "job-gen", "Find 5", "toolu_agent")
    await store.start_task(goal["id"], "a2", "research", "Research", "toolu_r")
    assert [t["task_id"] for t in await store.interrupt_tasks(goal["id"])] == ["a1", "a2"]
    # a resume: the SendMessage call has no name and its own id; the first values stay
    await store.start_task(goal["id"], "a1", "", "", "toolu_send")
    [a1] = await store.list_tasks(goal["id"], "running")
    assert (a1["name"], a1["tool_use_id"]) == ("job-gen", "toolu_agent")
    assert await store.finish_task("a1", "completed", "6 jobs") is True
    assert await store.finish_task("a1", "stopped", None) is False  # the first terminal status wins
    assert await store.finish_task("a1", "completed", "6 jobs, 2 skipped") is False  # a later summary is kept
    assert [t["summary"] for t in await store.list_tasks(goal["id"], "completed")] == ["6 jobs, 2 skipped"]
    assert await store.finish_task("a2", "failed", "boom") is True  # interrupted rows can still finish
    statuses = {t["task_id"]: t["status"] for t in await store.list_tasks(goal["id"])}
    assert statuses == {"a1": "completed", "a2": "failed"}


async def test_a_resumed_task_keeps_its_first_name_and_runs_again(store):
    goal = await store.create_goal("g")
    await store.start_task(goal["id"], "a1", "job-research", "Research 5 roles", "call-1")
    assert [t["task_id"] for t in await store.interrupt_tasks(goal["id"])] == ["a1"]
    await store.start_task(goal["id"], "a1", "", "", "send-1")  # a resume through SendMessage
    [task] = await store.list_tasks(goal["id"], "running")
    assert task["name"] == "job-research"
    assert await store.task_name(goal["id"], "a1") == "job-research"


async def scrape(store, goal_id, request_id, agent_id=None, status=None):
    row = await store.add_scrape(goal_id, request_id, "linkedin_company", f"u-{request_id}", True)
    if agent_id:
        await store.set_scrape_agent(goal_id, [request_id], agent_id)
    if status:
        await store.finish_scrape(row["id"], status, None)
    return row


async def test_agent_is_ready_only_when_all_its_scrapes_finished(store):
    goal = await store.create_goal("g")
    await store.set_status(goal["id"], "running")
    await store.start_task(goal["id"], "b2", "research", "Research jobs", "call-1")
    await scrape(store, goal["id"], "rq-1", "a1", "succeeded")
    await scrape(store, goal["id"], "rq-2", "a1")  # still pending
    await scrape(store, goal["id"], "rq-3", "b2", "failed")
    assert await store.agents_ready_to_notify() == [{"goal_id": goal["id"], "agent_id": "b2", "name": "research"}]


async def test_take_agent_unreported_marks_only_that_agents_rows(store):
    goal = await store.create_goal("g")
    await scrape(store, goal["id"], "rq-1", "a1", "succeeded")
    await scrape(store, goal["id"], "rq-2", "b2", "succeeded")
    taken = await store.take_agent_unreported(goal["id"], "a1")
    assert [row["request_id"] for row in taken] == ["rq-1"]
    assert await store.take_agent_unreported(goal["id"], "a1") == []
    assert await store.count_unreported(goal["id"]) == 1


async def test_stopped_or_capped_goals_get_no_agent_notes(store):
    goal = await store.create_goal("g")
    await scrape(store, goal["id"], "rq-1", "a1", "succeeded")
    for status in ("capped", "failed", "stopped", "done"):
        await store.set_status(goal["id"], status)
        assert await store.agents_ready_to_notify() == []
    await store.set_status(goal["id"], "waiting")
    assert len(await store.agents_ready_to_notify()) == 1


async def test_goal_resume_ignores_scrapes_that_a_subagent_asked_for(store):
    goal = await store.create_goal("g")
    await scrape(store, goal["id"], "rq-1", "a1", "succeeded")
    await scrape(store, goal["id"], "rq-2", "a1")
    await store.set_status(goal["id"], "waiting")
    assert await store.goals_ready_to_resume() == []
    await scrape(store, goal["id"], "rq-3", None, "succeeded")
    assert await store.goals_ready_to_resume() == [goal["id"]]
    assert [row["request_id"] for row in await store.take_unreported(goal["id"])] == ["rq-3"]


def test_goal_title_cuts_a_long_text_at_a_word_and_marks_the_cut():
    text = ("Find 1 technology startups based in San fransisco, USA, with fewer than 10 employees. "
            "For each startup, verify the CEO.")
    assert goal_title(text) == "Find 1 technology startups based in San fransisco, USA, with fewer than 10…"
    assert len(goal_title(text)) <= 80
    assert goal_title("Find 3 B2B SaaS jobs.") == "Find 3 B2B SaaS jobs."
    assert goal_title("  Research\n the   jobs.  ") == "Research the jobs."
    assert goal_title("x" * 100) == "x" * 79 + "…"  # one long word is cut inside the word


async def count(pool, table):
    async with pool.connection() as conn:
        return (await (await conn.execute(f"SELECT count(*) AS n FROM {table}")).fetchone())["n"]


async def test_delete_goals_removes_all_their_data_and_keeps_what_other_goals_use(store, artifacts, pool):
    gone, kept = await store.create_goal("gone"), await store.create_goal("kept")
    own, shared = await artifacts.save_jobs(gone["id"], roles("Own", "Shared"), "job-discovery")
    for row in (own, shared):
        await artifacts.save_company(gone["id"], row["job_id"], name=None, website=None, linkedin_url=None,
                                     data={"x": 1}, sources=[], created_by="job-research")
    [kept_role] = await artifacts.save_jobs(kept["id"], roles("Shared"), "job-discovery")
    await artifacts.save_job_research(gone["id"], own["job_id"], None, "snippet", None)
    await artifacts.set_fit(gone["id"], own["job_id"], "fit", "Go", [])
    await artifacts.save_note(gone["id"], own["job_id"], "w", "r", "g", "application-prep")
    await store.append_event(gone["id"], "user_message", {"text": "hi"})
    await store.set_sdk_session(gone["id"], "sess-old", 0.1)
    await store.set_sdk_session(gone["id"], "sess-now", 0.1)
    await store.start_task(gone["id"], "a1", "job-research", "", None)
    assert await store.delete_preview([gone["id"]]) == {"goals": 1, "jobs": 2, "notes": 1, "working": True}

    assert sorted(await store.delete_goals([gone["id"]])) == ["sess-now", "sess-old"]

    assert await store.get_goal(gone["id"]) is None and await store.list_events(gone["id"]) == []
    for table, n in {"jobs": 1, "application_notes": 0, "companies": 1, "subagent_tasks": 0}.items():
        assert await count(pool, table) == n, table
    # The other goal keeps the company that it has a role at, and the facts: its report uses them.
    job = await artifacts.get_job(kept["id"], kept_role["job_id"])
    assert job["company"] == "Shared" and len(job["research"]) == 1
    assert await store.delete_goals([gone["id"]]) == []  # a second delete finds nothing


async def test_a_goal_keeps_its_model_and_effort(store):
    goal = await store.create_goal("g", model="claude-opus-5-5", effort="high")
    assert (goal["model"], goal["effort"]) == ("claude-opus-5-5", "high")
    assert (await store.create_goal("old"))["model"] is None  # None: the .env default
    await store.set_model(goal["id"], "claude-haiku-4-5", "low")
    assert {k: (await store.get_goal(goal["id"]))[k] for k in ("model", "effort")} == {"model": "claude-haiku-4-5",
                                                                                       "effort": "low"}


async def test_a_goal_keeps_the_time_zone_of_its_user(store):
    goal = await store.create_goal("g", timezone="Asia/Kolkata")
    assert goal["timezone"] == "Asia/Kolkata"
    assert (await store.create_goal("old"))["timezone"] is None
    await store.set_timezone(goal["id"], "Europe/Berlin")
    after = await store.get_goal(goal["id"])
    assert after["timezone"] == "Europe/Berlin" and after["updated_at"] == goal["updated_at"]  # the list order stays
