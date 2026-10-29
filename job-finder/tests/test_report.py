from job_finder.mandates import kickoff_message
from job_finder.report import MAX_REQUEST, build_report, now_line, shown_request, warnings

from .fakes import roles

TEXT = "We build payment systems in Go and Python. " * 10  # long enough to be a full description


async def test_build_report(store, artifacts, recorder):
    goal_id = (await store.create_goal("Find 2 Go roles"))["id"]
    await store.set_brief(goal_id, {"title": "Go roles in Pune", "criteria": "Go, Pune", "target_count": 2})
    acme, beta = await artifacts.save_jobs(goal_id, roles("Acme", "Beta"), "job-discovery")
    await artifacts.save_job_research(goal_id, acme["job_id"], TEXT, "full_description", "https://jobs.lever.co/acme/1")
    await artifacts.save_company(goal_id, acme["job_id"], name="Acme", website="https://acme.com", linkedin_url=None,
                                 data={"size": "51-200"}, sources=["https://acme.com"], created_by="job-research")
    await artifacts.set_fit(goal_id, beta["job_id"], "not_fit", "The role is in Berlin.", ["Location: Berlin"])
    await artifacts.set_fit(goal_id, acme["job_id"], "fit", "Go and Pune match.", ["Go: stated"])
    assert await artifacts.save_note(goal_id, acme["job_id"], "Go match", "Payments work", "None",
                                     "application-prep") is None
    await recorder.record(goal_id, "tool_started", tool_use_id="t1", tool="read_page", label="Reading y",
                          agent="job-discovery")
    await recorder.record(goal_id, "tool_finished", tool_use_id="t1", tool="read_page", ok=False,
                          summary="Could not read y", failure_kind="not_found", agent="job-discovery")

    report = await build_report(store, artifacts, goal_id)
    assert report["title"] == "Go roles in Pune"
    assert report["request"] == "Find 2 Go roles"
    assert report["funnel"] == {"found": 2, "researched": 1, "checked": 2, "fit": 1}
    first, second = report["jobs"]
    assert (first["company_name"], first["fit"], first["evidence_level"]) == ("Acme", "fit", "full_description")
    assert first["description_text"] == TEXT and first["company"]["website"] == "https://acme.com"
    assert first["note"]["why_match"] == "Go match"
    assert [r["data"] for r in first["research"]] == [{"size": "51-200"}]
    assert (second["company_name"], second["fit"], second["extra"]) == ("Beta", "not_fit", False)
    assert report["not_taken"]["failed_steps"] == [{
        "agent": "job-discovery", "label": "Reading y", "summary": "Could not read y", "failure_kind": "not_found",
        "at": report["not_taken"]["failed_steps"][0]["at"]}]
    assert report["now"] == "Deciding the next step"


async def test_fit_roles_past_the_target_are_extra(store, artifacts):
    goal_id = (await store.create_goal("g"))["id"]
    await store.set_brief(goal_id, {"title": "t", "criteria": "c", "target_count": 1})
    rows = await artifacts.save_jobs(goal_id, roles("A", "B"), "job-discovery")
    for row in rows:
        await artifacts.save_job_research(goal_id, row["job_id"], TEXT, "full_description", None)
        await artifacts.set_fit(goal_id, row["job_id"], "fit", "ok", [])
    report = await build_report(store, artifacts, goal_id)
    assert [(j["company_name"], j["extra"]) for j in report["jobs"]] == [("A", False), ("B", True)]


async def test_unknown_goal_has_no_report(store, artifacts):
    assert await build_report(store, artifacts, "00000000-0000-0000-0000-000000000000") is None


async def test_the_report_title_is_the_goal_title_until_the_brief_has_one(store, artifacts):
    goal_id = (await store.create_goal("Find roles"))["id"]
    assert (await build_report(store, artifacts, goal_id))["title"] == "Find roles"


async def test_the_report_shows_saved_gaps_notes_and_rejected_roles(store, artifacts):
    goal_id = (await store.create_goal("g"))["id"]
    [row] = await artifacts.save_jobs(goal_id, roles("Acme"), "job-discovery")
    await artifacts.save_notes(goal_id, "job-research", [
        {"job_id": row["job_id"], "kind": "gap", "text": "The posting has no salary."},
        {"kind": "note", "text": "LinkedIn showed few roles in Pune."}])
    await artifacts.save_rejected(goal_id, [{"title": "Account Manager", "company_name": "Beta",
                                             "url": "https://jobs.lever.co/beta/2", "reason": "Not engineering."}],
                                  "job-discovery")
    out = (await build_report(store, artifacts, goal_id))["not_taken"]
    assert [(m["role"], m["text"]) for m in out["missing"]] == [("Backend Engineer, Acme", "The posting has no salary.")]
    assert [r["text"] for r in out["remarks"]] == ["LinkedIn showed few roles in Pune."]
    assert [(r["title"], r["reason"]) for r in out["rejected"]] == [("Account Manager", "Not engineering.")]
    detail = (await build_report(store, artifacts, goal_id))["jobs"][0]
    assert [n["text"] for n in detail["notes"]] == ["The posting has no salary."]


async def test_company_facts_from_another_goal_name_that_goal(store, artifacts):
    a, b = (await store.create_goal("Earlier search"))["id"], (await store.create_goal("g"))["id"]
    [old] = await artifacts.save_jobs(a, roles("Acme"), "job-discovery")
    await artifacts.save_company(a, old["job_id"], name="Acme", website="https://acme.com", linkedin_url=None,
                                 data={"size": "5"}, sources=[], created_by="job-research")
    await artifacts.save_jobs(b, roles("Acme"), "job-discovery")  # the company exists, so the role links to it
    research = (await build_report(store, artifacts, b))["jobs"][0]["research"]
    assert [(r["data"], r["from_goal"]) for r in research] == [({"size": "5"}, "Earlier search")]


def test_now_line_names_the_working_subagents_and_the_waits():
    pipeline = {"needs_research": 3, "needs_fit": 1}
    assert now_line("running", ["job-research", "fit-check"], pipeline, []) == (
        "Reading job pages for 3 roles. Checking the fit of 1 role")
    assert now_line("waiting", [], pipeline, [{"wait_reason": "accounts_off_shift"}]) == (
        "Waiting: linkedIn accounts are off shift")
    assert now_line("waiting", [], pipeline, [{"wait_reason": None}] * 2) == "Waiting for 2 LinkedIn scrapes"
    assert now_line("done", ["job-research"], pipeline, []) == ""


def test_warnings_tell_a_partial_list_and_a_failed_search_from_no_roles():
    reads = [{"state": "partial", "company_name": "Acme", "company_url": "u", "items": 75, "captured_pages": 3},
             {"state": "not_captured", "company_name": None,
              "company_url": "https://www.linkedin.com/company/big-co/", "items": 0, "captured_pages": None},
             {"state": "complete", "company_name": "Z", "company_url": "z", "items": 0, "captured_pages": 1}]
    events = [{"type": "tool_finished", "tool": "google_search", "ok": False}]
    texts = [w["text"] for w in warnings(reads, events)]
    assert texts == ["Only part of Acme's jobs: 75 roles from 3 pages. It is not the full list.",
                     "Could not read Big Co's jobs. This does not mean that it has none.",
                     "Google search failed 1 time. This does not mean that no roles exist."]


async def test_the_request_of_a_mandate_goal_is_its_instruction_when_it_started(store, artifacts, recorder, make_run, pool):
    # A mandate starts its goals with a notice, not a user message. The title of the goal is cut short.
    mandate, run = await make_run()
    instruction = "Find traditional companies that hire for AI.\nRecent open jobs are the main signal. " * 3
    history = {"runs": 0, "jobs": 0, "fit": 0, "not_fit": 0, "unsure": 0, "reasons": []}
    kickoff = kickoff_message({**mandate, "instruction": instruction}, 1, run["run_slot"], history)
    await recorder.record(run["id"], "notice", trigger="mandate_run", text=kickoff)
    await recorder.record(run["id"], "user_message", trigger="user", text="Why so few jobs?")
    await store.set_brief(run["id"], {"title": "x", "criteria": "the agent's own words", "target_count": 2})
    out = await build_report(store, artifacts, run["id"])
    assert out["request"] == " ".join(instruction.split())


def test_the_timing_counts_only_the_time_that_the_goal_works():
    from datetime import UTC, datetime

    from job_finder.report import goal_timing

    def at(h, m, s=0):
        return datetime(2026, 10, 21, h, m, s, tzinfo=UTC)

    def change(when, to):
        return {"type": "status_changed", "at": when.isoformat(), "to": to}

    events = [change(at(10, 41, 24), "stopped"), {"type": "text", "at": at(10, 50).isoformat()},
              change(at(12, 0), "running"), change(at(12, 1), "waiting"), change(at(12, 3), "done")]
    assert goal_timing(at(10, 36, 50), events, at(13, 0)) == {
        "started_at": at(10, 36, 50).isoformat(), "finished_at": at(12, 3).isoformat(), "work_seconds": 274 + 180}
    # While it works: no finish time, and the time until now.
    assert goal_timing(at(10, 36, 50), events[:3], at(12, 0, 30)) == {
        "started_at": at(10, 36, 50).isoformat(), "finished_at": None, "work_seconds": 274 + 30}
    # An ended goal with no event for its end, e.g. an old goal: it ended when its row last changed.
    assert goal_timing(at(10, 36, 50), [], at(13, 0), ended_at=at(10, 40)) == {
        "started_at": at(10, 36, 50).isoformat(), "finished_at": at(10, 40).isoformat(), "work_seconds": 190}


async def test_the_timing_of_a_mandate_goal_gives_its_time_limit(store, artifacts, make_run):
    _, run = await make_run()
    assert (await build_report(store, artifacts, run["id"]))["timing"]["limit_s"] == 7200
    goal = await store.create_goal("mine")
    assert (await build_report(store, artifacts, goal["id"]))["timing"]["limit_s"] is None


def test_a_failed_linkedin_search_is_a_warning_and_not_no_roles():
    scrapes = [{"kind": "linkedin_job_search", "status": "failed"}, {"kind": "linkedin_job_search", "status": "expired"},
               {"kind": "linkedin_job_search", "status": "succeeded"}, {"kind": "linkedin_job", "status": "failed"}]
    assert warnings([], [], scrapes) == [{"kind": "linkedin_search_failed", "text": (
        "LinkedIn job search failed 2 times. This does not mean that no roles exist.")}]
    assert warnings([], [], scrapes[2:]) == []


def test_the_shown_request_never_has_the_pasted_resume():
    resume = "Private Person\nprivate@example.com\n" + "Built payment systems. " * 40
    shown = shown_request(f"Find Go roles in Pune.\n\n{resume}", {"resume_text": resume.strip()})
    assert shown == "Find Go roles in Pune. [resume]"


def test_the_shown_request_is_short_when_the_profile_is_not_saved_yet():
    shown = shown_request("word " * 400, None)
    assert len(shown) <= MAX_REQUEST and shown.endswith("…")
