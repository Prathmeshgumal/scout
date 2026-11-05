import pytest

from job_finder.artifact_store import JOB_FILTERS, LISTING_CANNOT_FIT, MANDATE_REFUSAL, ONLY_FIT_NOTES, normalize_domain

DESCRIPTION = "We build payment systems in Go and Python. " * 10  # long enough to be a full description


@pytest.mark.parametrize("website, domain", [
    ("https://www.Acme.com/about?x=1", "acme.com"),
    ("acme.com", "acme.com"),
    ("  http://shop.acme.co.uk/ ", "shop.acme.co.uk"),
    ("acme", None),
    ("", None),
    ("http://", None),
])
def test_normalize_domain(website, domain):
    assert normalize_domain(website) == domain


async def goal(store, title="g"):
    return (await store.create_goal(title))["id"]


def role(n: int | str = 1, **extra) -> dict:
    return {"url": f"https://boards.greenhouse.io/acme/jobs/{n}", "title": f"Backend Engineer {n}",
            "company_name": "Acme", "location": "Bengaluru", **extra}


async def researched(artifacts, goal_id, job_id, evidence="full_description"):
    assert await artifacts.save_job_research(goal_id, job_id, DESCRIPTION if evidence == "full_description" else None,
                                             evidence, None) is None


async def test_a_role_is_saved_once_per_goal(store, artifacts):
    g = await goal(store)
    [first] = await artifacts.save_jobs(g, [role(1)], "job-discovery")
    [again] = await artifacts.save_jobs(g, [role(1, url="https://boards.greenhouse.io/acme/jobs/1?gh_src=x")],
                                        "job-discovery")
    assert first["new"] and not again["new"] and again["refused"] == "Already saved in this mission."
    assert again["job_id"] == first["job_id"]
    other = await goal(store)
    [elsewhere] = await artifacts.save_jobs(other, [role(1)], "job-discovery")
    assert elsewhere["new"]  # another goal, not of a mandate, can find it again


async def test_a_role_needs_url_title_and_company(store, artifacts):
    g = await goal(store)
    bad, good = await artifacts.save_jobs(g, [{"url": "https://x.io/j/1", "title": "Engineer"}, role(2)], "job-discovery")
    assert bad["error"] == "A role needs url, title and company_name." and good["new"]
    assert [job["title"] for job in await artifacts.list_jobs(g)] == ["Backend Engineer 2"]


async def test_a_linkedin_role_and_its_board_copy_show_once_with_the_better_evidence(store, artifacts):
    g = await goal(store)
    linkedin = {"url": "https://in.linkedin.com/jobs/view/backend-engineer-at-acme-4012345678?trk=x",
                "title": "Backend Engineer", "company_name": "Acme Pvt. Ltd.", "location": "Bengaluru, Karnataka"}
    [first] = await artifacts.save_jobs(g, [linkedin], "job-discovery")
    board = {"url": "https://jobs.lever.co/acme/1", "title": "Backend  engineer", "company_name": "ACME",
             "location": "Bengaluru", "snippet": "Go, Postgres, payments."}
    [twin] = await artifacts.save_jobs(g, [board], "job-discovery")
    assert twin["job_id"] == first["job_id"] and not twin["new"]
    assert "This copy is better" in twin["refused"]
    [job] = await artifacts.list_jobs(g)
    assert (job["url"], job["evidence_level"]) == ("https://jobs.lever.co/acme/1", "snippet")
    # A weaker copy does not replace it.
    [weaker] = await artifacts.save_jobs(g, [linkedin], "job-discovery")
    assert weaker["refused"] == "The same role is saved already, with evidence that is as good or better."
    assert (await artifacts.list_jobs(g))[0]["url"] == "https://jobs.lever.co/acme/1"


async def test_a_run_refuses_roles_from_earlier_runs_of_its_mandate(artifacts, make_run, store):
    mandate, first = await make_run(3)
    _, second = await make_run(4, mandate)
    await artifacts.save_jobs(first["id"], [role(1)], "job-discovery")
    twin = role(9, url="https://www.linkedin.com/jobs/view/77", title="Backend Engineer 1")
    rows = await artifacts.save_jobs(second["id"], [role(1), twin, role(2)], "job-discovery")
    assert [row.get("refused") for row in rows] == [MANDATE_REFUSAL, MANDATE_REFUSAL, None]
    assert rows[2]["new"]
    assert [r["reason"] for r in await artifacts.list_rejected(second["id"])] == [MANDATE_REFUSAL, MANDATE_REFUSAL]
    other_mandate, other_run = await make_run(5)
    assert other_mandate != mandate
    assert (await artifacts.save_jobs(other_run["id"], [role(1)], "job-discovery"))[0]["new"]
    normal = await store.create_goal("normal mission")
    assert (await artifacts.save_jobs(normal["id"], [role(1)], "job-discovery"))[0]["new"]


async def test_a_listing_only_role_cannot_be_fit(store, artifacts):
    g = await goal(store)
    [row] = await artifacts.save_jobs(g, [role(1)], "job-discovery")
    assert (await artifacts.list_jobs(g))[0]["evidence_level"] == "listing_only"
    assert await artifacts.set_fit(g, row["job_id"], "fit", "matches", []) == {"error": LISTING_CANNOT_FIT}
    assert await artifacts.set_fit(g, row["job_id"], "partial", "not read", []) == {"title": "Backend Engineer 1"}
    await researched(artifacts, g, row["job_id"], "snippet")
    assert await artifacts.set_fit(g, row["job_id"], "fit", "Go and Python", []) == {"title": "Backend Engineer 1"}


async def test_research_never_lowers_the_evidence(store, artifacts):
    g = await goal(store)
    [row] = await artifacts.save_jobs(g, [role(1, snippet="Go")], "job-discovery")
    await researched(artifacts, g, row["job_id"])
    await researched(artifacts, g, row["job_id"], "snippet")
    job = await artifacts.get_job(g, row["job_id"])
    assert (job["evidence_level"], job["description_text"]) == ("full_description", DESCRIPTION)


async def test_a_sign_in_wall_or_short_text_is_not_a_full_description(store, artifacts):
    g = await goal(store)
    [row] = await artifacts.save_jobs(g, [role(1)], "job-discovery")
    wall = await artifacts.save_job_research(g, row["job_id"], "Sign in to view this job. Join LinkedIn",
                                             "full_description", None)
    assert "sign-in wall" in wall
    short = await artifacts.save_job_research(g, row["job_id"], "Go, Python.", "full_description", None)
    assert "too short" in short
    assert (await artifacts.get_job(g, row["job_id"]))["evidence_level"] == "listing_only"


async def test_an_application_note_is_only_for_a_fit_role(store, artifacts):
    g = await goal(store)
    [row] = await artifacts.save_jobs(g, [role(1)], "job-discovery")
    assert await artifacts.save_note(g, row["job_id"], "w", "r", "x", "application-prep") == ONLY_FIT_NOTES
    await researched(artifacts, g, row["job_id"])
    await artifacts.set_fit(g, row["job_id"], "fit", "Go", [])
    assert await artifacts.save_note(g, row["job_id"], "w", "r", "x", "application-prep") is None
    assert await artifacts.save_note(g, row["job_id"], "w2", "r2", "x2", "application-prep") is None
    note = (await artifacts.get_job(g, row["job_id"]))["note"]
    assert (note["why_match"], note["resume_points"], note["gaps"]) == ("w2", "r2", "x2")


async def test_pipeline_and_filters(store, artifacts):
    g = await goal(store)
    await store.set_brief(g, {"criteria": "backend", "target_count": 3})
    a, b, c, d = await artifacts.save_jobs(g, [role(n) for n in "abcd"], "job-discovery")
    for row in (a, b, d):
        await researched(artifacts, g, row["job_id"])
    await artifacts.set_fit(g, a["job_id"], "fit", "matches", [])
    await artifacts.set_fit(g, b["job_id"], "not_fit", "on-site only", [])
    await artifacts.set_fit(g, d["job_id"], "partial", "pay not listed", [])
    pipeline = await artifacts.pipeline(g)
    assert {k: pipeline[k] for k in ("jobs", "researched", "checked", "fit", "partial", "not_fit", "noted",
                                     "needs_research", "needs_fit", "needs_note", "target")} == {
        "jobs": 4, "researched": 3, "checked": 3, "fit": 1, "partial": 1, "not_fit": 1, "noted": 0,
        "needs_research": 1, "needs_fit": 0, "needs_note": 1, "target": 3}
    titles = {name: [r["title"][-1] for r in await artifacts.list_jobs(g, name)] for name in JOB_FILTERS}
    assert titles == {"all": list("abcd"), "needs_research": ["c"], "research_again": [], "needs_fit": [],
                      "fit": ["a"], "partial": ["d"], "needs_note": ["a"]}
    await artifacts.save_note(g, a["job_id"], "w", "r", "x", "application-prep")
    assert (await artifacts.pipeline(g))["noted"] == 1
    assert await artifacts.list_jobs(g, "needs_note") == []


async def test_an_unsure_role_goes_back_to_research_one_time(store, artifacts):
    g = await goal(store)
    [row] = await artifacts.save_jobs(g, [role(1)], "job-discovery")

    async def ids(name):
        return [r["id"] for r in await artifacts.list_jobs(g, name)]

    await researched(artifacts, g, row["job_id"], "snippet")
    assert await ids("needs_fit") == [row["job_id"]]
    await artifacts.set_fit(g, row["job_id"], "unsure", "pay unknown", [])
    assert (await ids("research_again"), await ids("needs_fit")) == ([row["job_id"]], [])
    await researched(artifacts, g, row["job_id"])
    assert (await ids("research_again"), await ids("needs_fit")) == ([], [row["job_id"]])
    await artifacts.set_fit(g, row["job_id"], "unsure", "still unknown", [])
    for name in ("research_again", "needs_fit", "needs_research"):
        assert await ids(name) == []


async def test_a_role_waits_for_its_scrapes_before_fit_check(store, artifacts):
    g = await goal(store)
    a, b = await artifacts.save_jobs(g, [role("a"), role("b")], "job-discovery")
    for row in (a, b):
        await researched(artifacts, g, row["job_id"])
    scrape = await store.add_scrape(g, "rq-1", "linkedin_company", "u", True, job_id=a["job_id"])
    await store.add_scrape(g, "rq-2", "google_search", "q", False)  # a search for no role never blocks

    async def needs_fit():
        return [r["title"][-1] for r in await artifacts.list_jobs(g, "needs_fit")]

    assert await needs_fit() == ["b"]
    assert (await artifacts.pipeline(g))["waiting_scrapes"] == 1
    await store.finish_scrape(scrape["id"], "succeeded", None)
    assert await needs_fit() == ["b"]  # the data came, but research has not read it yet
    await researched(artifacts, g, a["job_id"])
    assert await needs_fit() == ["a", "b"]


async def test_a_scrape_blocks_its_role_for_10_minutes_at_most(store, artifacts):
    g = await goal(store)
    [a] = await artifacts.save_jobs(g, [role("a")], "job-discovery")
    await researched(artifacts, g, a["job_id"])
    pending = await store.add_scrape(g, "rq-1", "linkedin_company", "u", True, job_id=a["job_id"])
    await store._one("UPDATE scrapes SET submitted_at = now() - interval '11 minutes' WHERE id = %s RETURNING id",
                     (pending["id"],))
    assert len(await artifacts.list_jobs(g, "needs_fit")) == 1


async def test_company_facts_are_shared_and_agents_see_only_fresh_ones(store, artifacts):
    a, b = await goal(store), await goal(store)
    [first] = await artifacts.save_jobs(a, [role(1)], "job-discovery")
    company = await artifacts.save_company(a, first["job_id"], name=None, website="https://www.acme.com",
                                           linkedin_url=None, data={"size": "50"}, sources=[], created_by="job-research")
    [second] = await artifacts.save_jobs(b, [role(2, company_name="ACME Pvt Ltd")], "job-discovery")
    job = await artifacts.get_job(b, second["job_id"])
    assert job["company_id"] == company and job["research"][0]["data"] == {"size": "50"}
    async with store.pool.connection() as conn:
        await conn.execute("UPDATE company_research SET created_at = now() - interval '31 days'")
    assert (await artifacts.get_job(b, second["job_id"], fresh=True))["research"] == []
    assert len((await artifacts.get_job(b, second["job_id"]))["research"]) == 1  # the report shows all


async def test_the_main_agent_reads_other_goals(store, artifacts):
    a, b = await goal(store, "Go roles"), await goal(store)
    [row] = await artifacts.save_jobs(a, [role(1)], "job-discovery")
    await artifacts.set_fit(a, row["job_id"], "not_fit", "On-site only.", [])
    assert [(g["id"], g["title"], g["jobs"], g["not_fit"]) for g in await artifacts.list_goals(b, 10)] == [
        (a, "Go roles", 1, 1)]
    [job] = await artifacts.goal_jobs(a)
    assert (job["title"], job["fit"], job["fit_reasons"]) == ("Backend Engineer 1", "not_fit", "On-site only.")


async def test_copy_jobs_copies_once(store, artifacts):
    a, b = await goal(store), await goal(store)
    await artifacts.save_jobs(a, [role(1)], "job-discovery")
    assert await artifacts.copy_jobs(a, b, "job-research") == 1
    assert await artifacts.copy_jobs(a, b, "job-research") == 0
    assert [job["title"] for job in await artifacts.list_jobs(b)] == ["Backend Engineer 1"]


async def test_goal_agent_brief_and_prefix(store):
    goal_row = await store.create_goal("dev", agent="job-research")
    assert goal_row["agent"] == "job-research" and goal_row["brief"] is None
    await store.set_brief(goal_row["id"], {"criteria": "x", "target_count": 2})
    assert (await store.get_goal(goal_row["id"]))["brief"] == {"criteria": "x", "target_count": 2}
    assert await store.goals_by_prefix(goal_row["id"][:6]) == [goal_row["id"]]
    assert await store.goals_by_prefix("ffffffff-ffff") == []


async def test_save_notes_replaces_the_earlier_items_of_the_agent_for_the_same_role(store, artifacts):
    g = await goal(store)
    a, b = await artifacts.save_jobs(g, [role("a"), role("b")], "job-discovery")
    await artifacts.save_notes(g, "job-research", [
        {"kind": "gap", "text": "Pay not found.", "job_id": a["job_id"]},
        {"kind": "gap", "text": "No careers page.", "job_id": b["job_id"]},
        {"kind": "note", "text": "Used search snippets only.", "job_id": None}])
    await artifacts.save_notes(g, "fit-check", [{"kind": "gap", "text": "Visa unknown.", "job_id": a["job_id"]}])
    assert await artifacts.save_notes(g, "job-research", [
        {"kind": "note", "text": "Pay found.", "job_id": a["job_id"]}]) == 1
    rows = await artifacts.list_notes(g)
    assert [(r["agent"], r["text"]) for r in rows] == [
        ("job-research", "No careers page."), ("job-research", "Used search snippets only."),
        ("fit-check", "Visa unknown."), ("job-research", "Pay found.")]


async def test_a_failed_or_partial_company_read_is_kept_per_goal(store, artifacts):
    g = await goal(store)
    url = "https://www.linkedin.com/company/acme/"
    await artifacts.record_job_read(g, url, None, "not_captured")
    await artifacts.record_job_read(g, url, "Acme", "partial", 75, 3)
    [read] = await artifacts.list_job_reads(g)
    assert (read["company_name"], read["state"], read["items"], read["captured_pages"]) == ("Acme", "partial", 75, 3)
