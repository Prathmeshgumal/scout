from job_finder.report_md import pipeline_sentence, render_markdown

AT = "2026-10-06T10:19:51+00:00"


def job(title, company, verdict=None, **extra):
    return {"id": title, "title": title, "company_name": company, "location": "Pune", "workplace_type": "Hybrid",
            "posted_at": "2 days ago", "posting_status": "open", "evidence_level": "full_description",
            "url": f"https://jobs.example/{company.lower()}", "fit": verdict,
            "fit_reasons": f"{company} reasons." if verdict else None, "fit_evidence": [], "note": None, "notes": [],
            "research": [], **extra}


NOTHING_LEFT = {"missing": [], "remarks": [], "failed_steps": [], "failed_scrapes": []}
REPORT = {
    "goal": {"id": "g-1", "title": "Find", "status": "done", "status_reason": None, "created_at": AT,
             "updated_at": AT, "spend_usd": 0.62, "max_spend_usd": 2.0},
    "title": "Backend roles in Pune",
    "request": "Find 1 backend role,\nin Pune.",
    "brief": {"criteria": "Go, Pune.", "target_count": 1},
    "pipeline": {"jobs": 2, "researched": 1, "checked": 2, "fit": 1, "partial": 0, "not_fit": 1, "unsure": 0,
                 "noted": 1, "target": 1},
    "warnings": [],
    "jobs": [
        job("Backend Engineer", "Acme", "fit", fit_evidence=["Asks for Go."],
            note={"why_match": "Go and payments.", "resume_points": "The billing work.", "gaps": "No Kafka."},
            notes=[{"agent": "job-research", "kind": "note", "text": "HQ confirmed.\nFunding fits.", "at": AT},
                   {"agent": "job-research", "kind": "gap", "text": "Not shown.", "at": AT}],
            research=[{"kind": "profile", "data": {"size": "2-10", "linkedin": "https://www.linkedin.com/company/acme",
                                                   "empty": None},
                       "sources": ["https://news.example/acme", "press release"], "created_by": "job-research",
                       "created_at": AT}]),
        job("Platform Engineer", "Beta", "not_fit"),
    ],
    "not_taken": {"missing": [{"agent": "job-research", "text": "Exact pay", "at": AT},
                              {"agent": "fit-check", "role": "Backend Engineer", "text": "Team size.", "at": AT}],
                  "remarks": [{"agent": "job-discovery", "text": "I left out Gamma: it is in Berlin.", "at": AT}],
                  "failed_steps": [{"agent": "job-discovery", "label": "Reading x", "summary": "403", "at": AT}],
                  "failed_scrapes": []},
    "timing": {"started_at": AT, "finished_at": "2026-10-06T10:23:40+00:00", "work_seconds": 274},
}


def test_the_markdown_has_each_section_in_order():
    md = render_markdown(REPORT)
    headings = [line for line in md.splitlines() if line.startswith("#")]
    assert headings == ["# Backend roles in Pune", "## Roles (1)", "### Backend Engineer, Acme",
                        "#### Application note", "#### Company profile", "## Not taken",
                        "### Roles that are not a fit", "### Notes from the agents",
                        "### Data that the agents could not find", "### Failed work"]
    assert "**Request:** Find 1 backend role, in Pune.  \n**Criteria:** Go, Pune." in md
    assert ("2 roles found for a target of 1 fit. 1 researched, 1 fit, 0 partial, 0 unsure, 1 not a fit, "
            "1 application note. Spent \\$0.62 of the \\$2.00 limit.") in md
    assert md.endswith("\n") and "\n\n\n" not in md


def test_a_role_shows_its_facts_verdict_and_note():
    md = render_markdown(REPORT)
    assert ("**Verdict:** Fit  \nPune · Hybrid · Posted 2 days ago  \n**Evidence:** Full description read  \n"
            "**Link:** [jobs.example](https://jobs.example/acme)") in md
    assert "Acme reasons.\n\n- Asks for Go." in md
    assert "**Why this role matches:** Go and payments." in md
    assert "**Resume points to put first:** The billing work." in md and "**Gaps to prepare for:** No Kafka." in md


def test_only_the_notes_of_a_role_show_in_its_note_list():
    md = render_markdown(REPORT)
    assert "- job-research: HQ confirmed.\n  Funding fits." in md  # a second line stays in its list item
    assert "Not shown." not in md


def test_the_research_of_a_role_lists_its_facts_and_sources():
    md = render_markdown(REPORT)
    assert "- **LinkedIn:** [linkedin.com](https://www.linkedin.com/company/acme)" in md
    assert "- **Sources:** [news.example](https://news.example/acme), press release" in md
    assert "**Empty:**" not in md


def test_research_from_another_goal_names_that_goal():
    row = {"kind": "funding", "data": {"round": "Seed"}, "sources": [], "from_goal": "Go roles"}
    md = render_markdown({**REPORT, "jobs": [job("Engineer", "Acme", "fit", research=[row])]})
    assert '#### Company funding\n\nFrom the goal "Go roles".\n\n- **Round:** Seed' in md


def test_a_role_with_no_verdict_says_it_is_not_checked():
    md = render_markdown({**REPORT, "jobs": [job("Engineer", "Acme")]})
    assert "**Verdict:** Not checked  \n" in md


def test_a_closed_posting_says_so():
    assert "Pune · Hybrid · Posted 2 days ago · Closed  \n" in render_markdown(
        {**REPORT, "jobs": [job("Engineer", "Acme", posting_status="closed")]})


def test_a_not_fit_role_shows_only_in_not_taken():
    md = render_markdown(REPORT)
    assert "- **Platform Engineer, Beta:** Beta reasons." in md
    assert "### Platform Engineer, Beta" not in md


def test_a_gap_names_its_role():
    md = render_markdown(REPORT)
    assert "- job-research: Exact pay" in md and "- fit-check: Backend Engineer: Team size." in md


def test_warnings_come_before_the_roles():
    md = render_markdown({**REPORT, "warnings": [{"kind": "partial", "text": "Only part of the list was read."}]})
    assert "## Warnings\n\n- Only part of the list was read.\n\n## Roles (1)" in md


def test_agent_text_cannot_add_formatting():
    report = {**REPORT, "title": "Roles that pay $50k-$60k, *remote* or [hybrid]_x"}
    note = {"agent": "job-research", "kind": "note", "text": "Size <10 | `n/a`", "at": AT}
    report["jobs"] = [job("Engineer", "Acme", notes=[note])]
    md = render_markdown(report)
    assert md.startswith("# Roles that pay \\$50k-\\$60k, \\*remote\\* or \\[hybrid\\]\\_x\n")
    assert "- job-research: Size \\<10 \\| \\`n/a\\`" in md
    assert "[jobs.example](https://jobs.example/acme)" in md  # links stay links


def test_an_empty_goal_says_so():
    report = {**REPORT, "brief": None, "jobs": [], "not_taken": NOTHING_LEFT,
              "pipeline": {"jobs": 0, "researched": 0, "checked": 0, "fit": 0, "partial": 0, "not_fit": 0, "unsure": 0,
                           "noted": 0, "target": None}}
    md = render_markdown(report)
    assert "No roles yet." in md and "Nothing was left out." in md


def test_an_unavailable_source_is_listed_apart_from_failed_work():
    scrape = {"request_id": "rq", "kind": "linkedin_job", "target": "https://linkedin.com/jobs/view/1",
              "status": "failed"}
    not_taken = {"missing": [], "remarks": [], "failed_steps": [
        {"agent": "job-research", "label": "Reading parked.example", "summary": "Could not read",
         "failure_kind": "not_found", "at": AT},
        {"agent": "job-research", "label": "Reading broken.example", "summary": "Could not read",
         "failure_kind": "browser_error", "at": AT},
    ], "failed_scrapes": [{**scrape, "failure_kind": "not_found"},
                          {**scrape, "request_id": "rq-2", "failure_kind": "blocked"},
                          {**scrape, "request_id": "rq-3", "status": "expired", "failure_kind": None}]}
    md = render_markdown({**REPORT, "jobs": [], "not_taken": not_taken})
    failed = md[md.index("### Failed work"):md.index("### Source unavailable")]
    gone = md[md.index("### Source unavailable"):]
    assert "Reading broken.example (Could not read)" in failed and "(failed, blocked)" in failed
    assert "(expired)" in failed and "parked" not in failed and "not\\_found" not in failed
    assert "- job-research: Reading parked.example (not\\_found)" in gone
    assert "- Scrape of [linkedin.com](https://linkedin.com/jobs/view/1) (not\\_found)" in gone
    assert "broken" not in gone and "blocked" not in gone


def test_pipeline_sentence():
    pipeline = {"jobs": 3, "researched": 3, "checked": 0, "fit": 0, "partial": 0, "not_fit": 0, "unsure": 0, "noted": 0,
                "target": 1}
    assert pipeline_sentence(pipeline) == ("3 roles found for a target of 1 fit. 3 researched, none checked for fit, "
                                           "no application notes.")


def test_keys_name_linkedin_and_urls_correctly():
    row = {"kind": "profile", "data": {"linkedin_url": "https://linkedin.com/company/acme"}, "sources": []}
    md = render_markdown({**REPORT, "jobs": [job("Engineer", "Acme", "fit", research=[row])]})
    assert "- **LinkedIn URL:**" in md


def test_roles_that_were_not_taken_are_listed_with_the_reason():
    not_taken = {**NOTHING_LEFT, "rejected": [
        {"agent": "job-discovery", "title": "Go Developer", "company_name": "Myca", "reason": "Based in Berlin."},
        {"agent": "job-discovery", "title": "Rust Developer", "company_name": None, "reason": "Too junior."}]}
    md = render_markdown({**REPORT, "jobs": [], "not_taken": not_taken})
    assert "### Roles that were not taken" in md
    assert "- **Go Developer** (Myca): Based in Berlin.\n- **Rust Developer**: Too junior." in md


def test_a_fit_role_past_the_target_says_so():
    md = render_markdown({**REPORT, "jobs": [job("One", "Acme", "fit", extra=False), job("Two", "Gamma", "fit", extra=True)]})
    assert "**Verdict:** Fit  \n" in md
    assert "**Verdict:** Fit, more than the request  \n" in md


def test_the_times_show_in_the_time_zone_of_the_user():
    md = render_markdown(REPORT, "Asia/Kolkata")
    assert "Finished 2026-10-06 15:53 (Asia/Kolkata), after 4m 34s of run time." in md
    assert "2026-10-06 10:23 (UTC)" in render_markdown(REPORT)  # no zone: UTC
    working = {**REPORT, "timing": {**REPORT["timing"], "finished_at": None, "work_seconds": 3725}}
    assert "Started 2026-10-06 15:49 (Asia/Kolkata). It still works: 1h 02m of run time so far." in render_markdown(working, "Asia/Kolkata")


def test_the_run_time_of_a_mandate_goal_names_its_time_limit():
    timed = {**REPORT, "timing": {**REPORT["timing"], "work_seconds": 7500, "limit_s": 7200}}
    assert "after 2h 05m of run time. The time limit is 2h 00m." in render_markdown(timed, "Asia/Kolkata")
    assert "The time limit" not in render_markdown(REPORT, "Asia/Kolkata")
