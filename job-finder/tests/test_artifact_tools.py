import json

import jsonschema
import pytest
from claude_agent_sdk import _build_input_schema

from job_finder.artifact_store import LISTING_CANNOT_FIT, ONLY_FIT_NOTES
from job_finder.career_lens import CareerLens
from job_finder.tools import ToolContext
from job_finder.tools.artifacts import build_tools

DESCRIPTION = "We build payment systems in Go and Python. " * 10


@pytest.fixture
async def ctx(store, recorder):
    goal = await store.create_goal("g")
    return ToolContext(goal["id"], store, recorder, CareerLens("", ""))


def call(ctx, name, agent="job-discovery"):
    return next(t for t in build_tools(ctx, agent) if t.name == name).handler


def text(result) -> str:
    """The last block of the reply: the data, when a save tool also says what it saved."""
    return result["content"][-1]["text"]


def said(result) -> str:
    """What a save tool says that it saved: the first block of its reply."""
    return result["content"][0]["text"]


def role(n=1, **extra):
    return {"url": f"https://jobs.lever.co/acme/{n}", "title": f"Backend Engineer {n}", "company_name": "Acme",
            **extra}


async def saved_job(ctx, **extra) -> str:
    out = await call(ctx, "save_jobs")({"jobs": [role(**extra)]})
    return json.loads(text(out))[0]["job_id"]


async def test_set_brief_checks_input_and_saves(ctx, store):
    brief = {"title": "Backend roles in Bengaluru", "criteria": "Go or Python", "target_count": "5"}
    assert (await call(ctx, "set_brief", "main")({**brief, "criteria": ""}))["is_error"]
    assert (await call(ctx, "set_brief", "main")({**brief, "target_count": 0}))["is_error"]
    out = await call(ctx, "set_brief", "main")(brief)
    assert "is_error" not in out
    assert (await store.get_goal(ctx.goal_id))["brief"]["target_count"] == 5
    assert said(out) == "Brief saved: 5 fit roles. Go or Python"


async def test_set_brief_needs_a_short_title(ctx, store):
    brief = {"criteria": "Go", "target_count": 5}
    assert (await call(ctx, "set_brief", "main")(brief))["is_error"]
    out = await call(ctx, "set_brief", "main")({**brief, "title": " ".join(["word"] * 15)})
    assert out["is_error"] and "fewer than 15 words" in text(out)
    assert "is_error" not in await call(ctx, "set_brief", "main")({**brief, "title": " ".join(["word"] * 14)})


async def test_set_brief_in_a_run_keeps_the_quota(make_run, store, recorder):
    _, run = await make_run(quota=20)
    ctx = ToolContext(run["id"], store, recorder, CareerLens("", ""))
    out = await call(ctx, "set_brief", "main")({"title": "Go roles", "criteria": "Go, remote", "target_count": 5})
    assert (await store.get_goal(run["id"]))["brief"]["target_count"] == 20
    assert said(out) == "Brief saved: 20 fit roles (fixed by the mandate). Go, remote"


async def test_only_fit_check_and_application_prep_read_the_resume(ctx, store):
    empty = json.loads(text(await call(ctx, "get_brief", "job-discovery")({})))
    assert "no brief yet" in empty["brief"] and "no candidate profile" in empty["profile"]
    await call(ctx, "save_profile", "candidate-profile")({
        "target_titles": {"value": ["Backend Engineer"], "basis": "stated"},
        "locations": {"value": ["Bengaluru"], "basis": "stated"},
        "resume_text": "Six years of Go."})
    for agent, sees in (("job-discovery", False), ("job-research", False), ("fit-check", True),
                        ("application-prep", True)):
        profile = json.loads(text(await call(ctx, "get_brief", agent)({})))["profile"]
        assert ("resume_text" in profile) is sees, agent


async def test_a_profile_without_titles_or_locations_is_not_ready(ctx, store):
    out = await call(ctx, "save_profile", "candidate-profile")({"skills": {"value": ["Go"], "basis": "stated"}})
    assert "not ready" in said(out) and "target_titles" in said(out) and "locations" in said(out)
    assert (await store.get_goal(ctx.goal_id))["profile_ready"] is False


async def test_save_jobs_accepts_a_json_string_and_says_what_it_refused(ctx):
    save = call(ctx, "save_jobs")
    out = await save({"jobs": json.dumps([role(1), role(1), {"url": "x"}])})
    assert said(out) == "1 new role, 1 refused, 1 not saved"
    rows = json.loads(text(out))
    assert rows[1]["refused"] == "Already saved in this goal." and "error" in rows[2]
    assert (await save({}))["is_error"]
    assert (await save({"jobs": [{"title": "no url"}]}))["is_error"]  # nothing saved is an error


async def test_save_jobs_saves_rejected_roles(ctx, artifacts):
    out = await call(ctx, "save_jobs")({"rejected": [
        {"title": "Account Manager", "company_name": "Acme", "url": "https://acme.com/jobs/9", "reason": "Not engineering."}]})
    assert said(out) == "0 new roles, 0 refused, 0 not saved, 1 rejected"
    assert [r["reason"] for r in await artifacts.list_rejected(ctx.goal_id)] == ["Not engineering."]


async def test_agents_do_not_see_company_ids(ctx):
    job_id = await saved_job(ctx)
    listed = json.loads(text(await call(ctx, "list_jobs")({})))
    assert "company_id" not in listed[0]
    assert "company_id" not in json.loads(text(await call(ctx, "read_job")({"job_id": job_id})))


async def test_list_and_read_check_their_input(ctx):
    assert (await call(ctx, "list_jobs")({"filter": "unknown"}))["is_error"]
    assert text(await call(ctx, "list_jobs")({})) == "No roles match."
    out = await call(ctx, "read_job")({"job_id": "not-a-uuid"})
    assert out["is_error"] and "list_jobs" in text(out)


async def test_research_refuses_a_sign_in_wall(ctx):
    job_id = await saved_job(ctx)
    research = call(ctx, "save_job_research", "job-research")
    out = await research({"job_id": job_id, "evidence_level": "full_description",
                          "description_text": "Sign in to view more jobs. Join LinkedIn now."})
    assert out["is_error"] and "sign-in wall" in text(out)
    assert (await research({"job_id": job_id, "evidence_level": "best"}))["is_error"]
    out = await research({"job_id": job_id, "evidence_level": "full_description", "description_text": DESCRIPTION,
                          "source_url": "https://jobs.lever.co/acme/1"})
    assert said(out) == "Research saved: full_description"
    read = json.loads(text(await call(ctx, "read_job", "fit-check")({"job_id": job_id})))
    assert read["evidence_level"] == "full_description" and read["description_text"].startswith("We build")


async def test_save_fit_refuses_fit_for_a_listing_only_role(ctx):
    job_id = await saved_job(ctx)
    fit = call(ctx, "save_fit", "fit-check")
    assert (await fit({"job_id": job_id, "verdict": "great", "reasons": "x"}))["is_error"]
    assert (await fit({"job_id": job_id, "verdict": "fit", "reasons": ""}))["is_error"]
    out = await fit({"job_id": job_id, "verdict": "fit", "reasons": "Go matches."})
    assert out["is_error"] and text(out) == LISTING_CANNOT_FIT
    assert said(await fit({"job_id": job_id, "verdict": "unsure", "reasons": "Not read."})) == (
        "Verdict saved: Backend Engineer 1 is unsure")


async def test_an_application_note_needs_a_fit_role(ctx):
    job_id = await saved_job(ctx, snippet="Go and Postgres.")
    note = call(ctx, "save_application_note", "application-prep")
    args = {"job_id": job_id, "why_match": "Go", "resume_points": "Payments", "gaps": "Kafka"}
    assert text(await note(args)) == ONLY_FIT_NOTES
    await call(ctx, "save_fit", "fit-check")({"job_id": job_id, "verdict": "fit", "reasons": "Go matches."})
    assert (await note({**args, "gaps": ""}))["is_error"]
    assert said(await note(args)) == "Application note saved"


async def test_save_company_shares_facts_with_the_role(ctx):
    job_id = await saved_job(ctx)
    save = call(ctx, "save_company", "job-research")
    assert (await save({"job_id": job_id, "facts": "big"}))["is_error"]
    out = await save({"job_id": job_id, "website": "acme.com", "facts": {"size": "200"}, "sources": ["https://acme.com"]})
    assert said(out) == "Company facts saved"
    read = json.loads(text(await call(ctx, "read_job")({"job_id": job_id})))
    assert read["research"][0]["data"] == {"size": "200"} and read["website"] == "acme.com"


async def test_save_notes_checks_the_items_and_the_role(ctx, artifacts):
    job_id = await saved_job(ctx)
    save = call(ctx, "save_notes", "fit-check")
    assert (await save({"items": []}))["is_error"]
    assert (await save({"items": [{"kind": "idea", "text": "x"}]}))["is_error"]
    other_goal = (await ctx.store.create_goal("h"))["id"]
    [other] = await artifacts.save_jobs(other_goal, [role(2)], "job-discovery")
    assert (await save({"items": [{"kind": "gap", "text": "Pay", "job_id": other["job_id"]}]}))["is_error"]
    out = await save({"items": json.dumps([{"kind": "gap", "text": "Pay not listed.", "job_id": job_id},
                                           {"kind": "note", "text": "I read 'remote' as remote in India."}])})
    assert said(out) == "Saved 2 items for the report"
    read = json.loads(text(await call(ctx, "read_job", "job-research")({"job_id": job_id})))
    assert read["report_notes"] == [{"agent": "fit-check", "kind": "gap", "text": "Pay not listed."}]


async def test_save_notes_takes_a_null_job_id_as_the_goal(ctx, artifacts):
    save = next(t for t in build_tools(ctx, "job-discovery") if t.name == "save_notes")
    args = {"items": [{"kind": "note", "text": "Few roles in Pune.", "job_id": None}]}
    jsonschema.validate(instance=args, schema=_build_input_schema(save))  # the SDK's own check
    assert said(await save.handler(args)) == "Saved 1 item for the report"


async def test_search_history_shows_discovery_searches_pages_rejections_and_reads(ctx, recorder, artifacts):
    g = ctx.goal_id
    await recorder.record(g, "tool_started", {"input": {"query": "site:jobs.lever.co \"Backend\""}},
                          agent="job-discovery", tool="google_search", tool_use_id="t1")
    await recorder.record(g, "tool_finished", None, agent="job-discovery", tool="google_search", tool_use_id="t1",
                          ok=True)
    await recorder.record(g, "tool_started", {"input": {"url": "https://acme.com/careers", "prompt": "roles"}},
                          agent="job-discovery", tool="read_page", tool_use_id="t2")
    await recorder.record(g, "tool_finished", None, agent="job-discovery", tool="read_page", tool_use_id="t2",
                          ok=False, summary="Could not read https://acme.com/careers: the page capture failed (blocked).")
    await recorder.record(g, "tool_started", {"input": {"query": "not discovery"}},
                          agent="job-research", tool="google_search", tool_use_id="t3")
    await artifacts.save_rejected(g, [{"title": "Account Manager", "company_name": "Acme", "reason": "Not engineering."}],
                                  "job-discovery")
    await artifacts.record_job_read(g, "https://www.linkedin.com/company/acme/", "Acme", "partial", 75, 3)
    out = json.loads(text(await call(ctx, "get_search_history")({})))
    assert [s["query"] for s in out["searches"]] == ['site:jobs.lever.co "Backend"']
    assert out["pages"][0]["ok"] is False and "blocked" in out["pages"][0]["failure"]
    assert out["rejected"] == [{"title": "Account Manager", "company_name": "Acme", "reason": "Not engineering."}]
    assert out["company_job_reads"][0]["state"] == "partial"


async def test_finish_goal_needs_a_reason_and_waits_for_pending_scrapes(ctx, store):
    finish = call(ctx, "finish_goal", "main")
    assert (await finish({"reason": " "}))["is_error"]
    await store.add_scrape(ctx.goal_id, "rq-1", "linkedin_company", "https://www.linkedin.com/company/acme/", True)
    out = await finish({"reason": "Target reached."})
    assert out["is_error"] and said(out) == "1 scrape still pending. End your turn. You get a note when they finish."
    [scrape] = await store.pending_scrapes()
    await store.finish_scrape(scrape["id"], "succeeded", None)
    assert said(await finish({"reason": "Target reached."})) == "Goal finished: Target reached."


async def test_other_goals_are_read_only_summaries(ctx, store, artifacts):
    other = (await store.create_goal("Go roles"))["id"]
    await artifacts.save_jobs(other, [role(1)], "job-discovery")
    goals = json.loads(text(await call(ctx, "list_goals", "main")({})))
    assert [(g["title"], g["jobs"]) for g in goals] == [("Go roles", 1)]
    jobs = json.loads(text(await call(ctx, "read_goal_jobs", "main")({"goal_id": other})))
    assert jobs[0]["title"] == "Backend Engineer 1"
