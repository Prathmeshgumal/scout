import re

import pytest

from job_finder.guard import SubagentGuard
from job_finder.hooks import (
    CLOSED_REASON,
    MAIN_THREAD_MATCHER,
    MAIN_THREAD_REASON,
    QUEUE_MATCHER,
    SCRAPE_MATCHER,
    main_thread_hook,
    scrape_agent_hook,
    scrape_cap_hook,
    subagent_guard_hook,
)

from .fakes import roles

LI = {"linkedin_url": "https://www.linkedin.com/company/acme/"}


def decision(result):
    return (result.get("hookSpecificOutput") or {}).get("permissionDecision")


async def setup_cap(store, used, cap):
    gid = (await store.create_goal("g"))["id"]
    for n in range(used):
        await store.add_scrape(gid, f"rq-{n}", "linkedin_company", "u", True)
    await store.add_scrape(gid, "rq-search", "google_search", "q", False)  # never counts
    notices = []

    async def notify(text):
        notices.append(text)

    return gid, scrape_cap_hook(store, gid, cap, notify), notices


async def test_scrape_under_the_cap_is_allowed(store):
    _, hook, notices = await setup_cap(store, used=1, cap=2)
    assert await hook({"tool_input": LI}, "t1", {}) == {}
    assert notices == []


@pytest.mark.parametrize("tool_input", [LI, {"url": "https://www.linkedin.com/jobs/view/42/"},
                                        {"keywords": "go engineer"}])
async def test_each_linkedin_read_counts_to_the_cap(store, tool_input):
    _, hook, notices = await setup_cap(store, used=2, cap=2)
    result = await hook({"tool_input": tool_input}, "t1", {})
    assert decision(result) == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason == ("Scrape cap reached (2/2). Use get_company or get_linkedin_job for saved data, "
                      "or finish with what you have.")
    assert notices == [reason]


def test_the_cap_covers_every_tool_that_asks_for_a_fresh_linkedin_read():
    for name in ("scrape_company", "scrape_linkedin_job", "search_linkedin_jobs"):
        assert re.fullmatch(SCRAPE_MATCHER, f"mcp__career_lens__{name}")
    for name in ("get_company", "get_linkedin_job", "google_search", "read_page"):
        assert not re.fullmatch(SCRAPE_MATCHER, f"mcp__career_lens__{name}")


async def test_closed_hook_denies_every_scrape(store):
    gid = (await store.create_goal("g"))["id"]
    hook = scrape_cap_hook(store, gid, 15, lambda text: None, closed=True)
    result = await hook({"tool_input": LI}, "t1", {})
    assert decision(result) == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == CLOSED_REASON


def test_main_thread_matcher_covers_the_web_tools_and_each_subagent_server():
    for name in ("career_lens__read_page", "artifacts_fit_check__save_fit", "artifacts_job_discovery__save_jobs",
                 "artifacts_job_research__save_company", "artifacts_application_prep__save_application_note",
                 "artifacts_candidate_profile__save_profile", "artifacts_candidate_profile__get_brief",
                 "artifacts_candidate_profile__read_file"):
        assert re.fullmatch(MAIN_THREAD_MATCHER, f"mcp__{name}"), name
    for name in ("artifacts__get_pipeline", "artifacts__set_brief", "mandates__propose_mandate"):
        assert not re.fullmatch(MAIN_THREAD_MATCHER, f"mcp__{name}"), name
    assert not re.fullmatch(MAIN_THREAD_MATCHER, "Agent")


async def test_main_thread_hook_denies_the_main_agent_and_allows_subagents():
    hook = main_thread_hook()
    denied = await hook({"tool_name": "mcp__career_lens__read_page", "tool_input": {}}, "t1", None)
    assert decision(denied) == "deny"
    assert denied["hookSpecificOutput"]["permissionDecisionReason"] == MAIN_THREAD_REASON
    assert await hook({"tool_name": "mcp__career_lens__read_page", "tool_input": {}, "agent_id": "a1"},
                      "t2", None) == {}
    denied = await hook({"tool_name": "mcp__artifacts_fit_check__save_fit", "tool_input": {}}, "t3", None)
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await hook({"tool_name": "mcp__artifacts_fit_check__save_fit", "tool_input": {}, "agent_id": "a1"},
                      "t4", None) == {}


async def agents_of(store):
    async with store.pool.connection() as conn:
        cursor = await conn.execute("SELECT request_id, agent_id FROM scrapes ORDER BY request_id")
        return {row["request_id"]: row["agent_id"] for row in await cursor.fetchall()}


async def queued_goal(store, *request_ids):
    goal = await store.create_goal("g")
    for request_id in request_ids:
        await store.add_scrape(goal["id"], request_id, "linkedin_company", "u", True)
    return goal["id"], scrape_agent_hook(store, goal["id"])


async def test_scrape_agent_hook_records_the_subagent_on_queued_scrapes(store):
    _, hook = await queued_goal(store, "rq-1", "rq-2")
    reply = [{"type": "text", "text": "LinkedIn company u1: queued as rq-1.\nLinkedIn job u2: queued as rq-2.\n"
                                      "Scrapes can take minutes. Continue with other work."}]
    assert await hook({"tool_name": "mcp__career_lens__scrape_company", "tool_response": reply,
                       "agent_id": "a7f"}, "t1", None) == {}
    assert await agents_of(store) == {"rq-1": "a7f", "rq-2": "a7f"}


async def test_scrape_agent_hook_reads_every_reply_shape(store):
    _, hook = await queued_goal(store, "rq-3", "rq-4", "rq-5")
    for reply in ({"content": [{"type": "text", "text": "x: queued as rq-3."}]},
                  'Google search for "acme" is still running; queued as rq-4. Scrapes can',
                  "x: queued as rq-5."):
        await hook({"tool_response": reply, "agent_id": "b2"}, "t", None)
    assert await agents_of(store) == {"rq-3": "b2", "rq-4": "b2", "rq-5": "b2"}


async def test_scrape_agent_hook_ignores_the_main_thread_and_fresh_data(store):
    _, hook = await queued_goal(store, "rq-6")
    await hook({"tool_response": "x: queued as rq-6."}, "t", None)  # no agent_id: main thread
    await hook({"tool_response": "x: already fresh (scraped within 7 days).", "agent_id": "a7f"}, "t", None)
    assert await agents_of(store) == {"rq-6": None}


def test_queue_matcher_covers_the_tools_that_queue():
    for name in ("scrape_company", "scrape_linkedin_job", "search_linkedin_jobs", "google_search"):
        assert re.fullmatch(QUEUE_MATCHER, f"mcp__career_lens__{name}")
    assert not re.fullmatch(QUEUE_MATCHER, "mcp__career_lens__get_company")


async def test_guard_hook_allows_one_run_of_each_type(store):
    goal = await store.create_goal("g")
    hook = subagent_guard_hook(SubagentGuard(lambda: []), store, goal["id"])

    async def start(name, use_id, **extra):
        return await hook({"tool_name": "Agent", "tool_input": {"subagent_type": name}, **extra}, use_id, None)

    assert await start("job-research", "t1") == {}
    denied = await start("job-research", "t2")
    assert decision(denied) == "deny"
    assert "job-research run is already working" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    assert await start("job-discovery", "t3") == {}
    assert await start("job-research", "t4", agent_id="a1") == {}  # not the main thread


async def test_guard_hook_checks_the_type_of_a_resumed_subagent(store):
    goal = await store.create_goal("g")
    await store.start_task(goal["id"], "a7f", "job-research", "task", "t0")
    hook = subagent_guard_hook(SubagentGuard(lambda: ["job-research"]), store, goal["id"])
    assert decision(await hook({"tool_name": "SendMessage", "tool_input": {"to": "a7f"}}, "t1", None)) == "deny"
    assert await hook({"tool_name": "SendMessage", "tool_input": {"to": "unknown"}}, "t2", None) == {}


async def test_the_agent_tool_is_refused_once_the_goal_is_a_home_chat(store, pool):
    from datetime import UTC, datetime, time

    from job_finder.hooks import HOME_AGENT_REASON, home_agent_hook
    from job_finder.mandate_store import MandateStore
    gid = (await store.create_goal("g"))["id"]
    hook = home_agent_hook(store, gid)
    assert await hook({"tool_input": {}}, "t1", {}) == {}
    await MandateStore(pool).create(gid, {"title": "t", "instruction": "i", "every_unit": "day", "every_count": 1, "jobs_per_run": 1,
                                          "window_days": [1], "window_start": time(9), "timezone": "UTC",
                                          "max_spend_per_run": 1, "max_scrapes_per_run": 1,
                                          "next_run_at": datetime.now(UTC)})
    out = await hook({"tool_input": {}}, "t1", {})
    assert decision(out) == "deny" and out["hookSpecificOutput"]["permissionDecisionReason"] == HOME_AGENT_REASON


async def test_a_run_with_four_times_its_quota_of_roles_refuses_more(store, artifacts, make_run):
    from job_finder.hooks import CEILING_REASON, job_ceiling_hook
    _, run = await make_run(quota=1)
    notices = []

    async def notify(text):
        notices.append(text)

    hook = job_ceiling_hook(store, run["id"], notify)
    await artifacts.save_jobs(run["id"], roles("A", "B", "C"), "job-discovery")
    assert await hook({"tool_input": {}}, "t1", {}) == {}
    await artifacts.save_jobs(run["id"], roles("D"), "job-discovery")
    out = await hook({"tool_input": {}}, "t1", {})
    assert decision(out) == "deny" and notices == [CEILING_REASON]


async def test_a_goal_starts_only_the_profile_subagent_until_its_profile_is_resolved(store):
    from job_finder.hooks import profile_first_hook
    goal = await store.create_goal("g")
    hook = profile_first_hook(store, goal["id"])

    def agent(name):
        return {"tool_name": "Agent", "tool_input": {"subagent_type": name}}

    assert decision(await hook(agent("job-discovery"), "t1", None)) == "deny"
    assert await hook(agent("candidate-profile"), "t2", None) == {}
    await store.set_profile(goal["id"], {"target_titles": {"value": ["Backend Engineer"], "basis": "stated"}},
                            ready=False)
    assert decision(await hook(agent("job-discovery"), "t3", None)) == "deny"
    await store.set_profile(goal["id"], {"locations": {"value": ["Pune"], "basis": "stated"}}, ready=True)
    assert await hook(agent("job-discovery"), "t3", None) == {}
