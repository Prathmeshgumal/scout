"""Focused coverage for LinkedIn company Jobs-tab parsing."""

from __future__ import annotations

import json

from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"
JOBS_URL = f"{COMPANY_URL}jobs/"
RESULTS_URL = "https://www.linkedin.com/jobs/search-results/?f_C=123"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card">
    <h1 class="org-top-card-summary__title">Acme</h1>
  </section>
</section></main></body></html>
"""

JOBS_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-jobs-recently-posted-job-card__container">
    <a class="job-card-square__link"
       href="/jobs/search-results/?currentJobId=100">
      <div class="job-card-square__title">Preview-only role</div>
      <div class="job-card-container__company-name">Acme</div>
      <div class="job-card-container__metadata-wrapper">Remote</div>
      <time>1 day ago</time>
    </a>
  </section>
  <a href="/jobs/search-results/?f_C=123">Show all jobs</a>
</section></main></body></html>
"""

EMPTY_JOBS_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="org-jobs-container">
    <div class="org-jobs-empty-jobs-module artdeco-card">
      <figure aria-hidden="true"></figure>
      <p>There are no jobs right now.</p>
      <p>Create a job alert and we will let you know.</p>
    </div>
  </div>
</section></main></body></html>
"""

RESULT_PAGE_1 = """
<html><body><main><h1>Job listings</h1>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/101/">Platform Engineer</a></h2>
    <p>Acme</p><p>Bengaluru (Hybrid)</p>
    <p>5 school alumni work here</p><p>67 company alumni work here</p>
    <p>Be an early applicant</p><p>2 days ago</p>
  </article>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/102/">Data Analyst</a></h2>
    <p>Acme</p><p>Pune (On-site)</p><p>1 week ago</p>
  </article>
</main></body></html>
"""

RESULT_PAGE_2 = """
<html><body><main><h1>Job listings</h1>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/102/">Data Analyst</a></h2>
    <p>Acme</p><p>Pune (On-site)</p><p>1 week ago</p>
  </article>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/103/">Product Designer</a></h2>
    <p>Acme</p><p>Remote</p><p>3 weeks ago</p>
  </article>
</main></body></html>
"""

LEGACY_RESULT_PAGE = """
<html><body>
  <div data-testid="lazy-column" componentkey="SearchResultsMainContent">
    <div componentkey="job-card-component-ref-201">
      <button aria-label="Dismiss Infrastructure Engineer job"></button>
      <p>Infrastructure Engineer</p>
      <p>Acme</p><p>Chennai (Hybrid)</p><p>&#183;</p><p>Posted today</p>
    </div>
  </div>
  <section componentkey="JobDetailsMainContent">
    <h2>Your profile and resume are missing some required qualifications</h2>
    <p>Viewer-specific account information</p>
  </section>
</body></html>
"""

def test_company_jobs_prefer_paginated_results_and_deduplicate() -> None:
    result = parse_company_html(
        HOME_HTML,
        {
            "jobs": JOBS_HTML,
            "jobs-page-1": RESULT_PAGE_1,
            "jobs-page-2": RESULT_PAGE_2,
        },
        company_url=COMPANY_URL,
        tab_urls={
            "jobs": JOBS_URL,
            "jobs-page-1": RESULTS_URL,
            "jobs-page-2": f"{RESULTS_URL}&start=25",
        },
        tab_capture={
            "jobs-page-1": {"has_more": True},
            "jobs-page-2": {"has_more": False},
        },
    )

    jobs = result["company"]["jobs"]
    assert jobs == {
        "source_url": JOBS_URL,
        "captured_pages": 2,
        "reached_end": True,
        "items": [
            {
                "id": "101",
                "title": "Platform Engineer",
                "url": "https://www.linkedin.com/jobs/view/101/",
                "company_name": "Acme",
                "location": "Bengaluru",
                "workplace_type": "Hybrid",
                "posted_at": "2 days ago",
                "posting_status": None,
                "insights": ["Be an early applicant"],
                "page": 1,
                "position": 1,
            },
            {
                "id": "102",
                "title": "Data Analyst",
                "url": "https://www.linkedin.com/jobs/view/102/",
                "company_name": "Acme",
                "location": "Pune",
                "workplace_type": "On-site",
                "posted_at": "1 week ago",
                "posting_status": None,
                "insights": [],
                "page": 1,
                "position": 2,
            },
            {
                "id": "103",
                "title": "Product Designer",
                "url": "https://www.linkedin.com/jobs/view/103/",
                "company_name": "Acme",
                "location": None,
                "workplace_type": "Remote",
                "posted_at": "3 weeks ago",
                "posting_status": None,
                "insights": [],
                "page": 2,
                "position": 2,
            },
        ],
    }
    assert "Preview-only role" not in json.dumps(jobs)
    assert result["section_status"]["jobs"] == "parsed"
    assert result["sources"]["jobs"] == {
        "artifact": None,
        "url": JOBS_URL,
        "result_pages": [
            {"page": 1, "artifact": None, "url": RESULTS_URL},
            {"page": 2, "artifact": None, "url": f"{RESULTS_URL}&start=25"},
        ],
    }

def test_company_jobs_use_preview_when_result_pages_were_not_captured() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"jobs": JOBS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"jobs": JOBS_URL},
    )

    assert result["company"]["jobs"] == {
        "source_url": JOBS_URL,
        "captured_pages": 0,
        "reached_end": None,
        "items": [
            {
                "id": "100",
                "title": "Preview-only role",
                "url": "https://www.linkedin.com/jobs/view/100/",
                "company_name": "Acme",
                "location": None,
                "workplace_type": "Remote",
                "posted_at": "1 day ago",
                "posting_status": None,
                "insights": [],
                "page": None,
                "position": 1,
            }
        ],
    }

def test_company_jobs_mark_a_confirmed_empty_jobs_tab_as_complete() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"jobs": EMPTY_JOBS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"jobs": JOBS_URL},
    )

    assert result["company"]["jobs"] == {
        "source_url": JOBS_URL,
        "captured_pages": 0,
        "reached_end": True,
        "items": [],
    }

def test_company_jobs_exclude_the_selected_job_and_account_pane() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"jobs-page-1": LEGACY_RESULT_PAGE},
        company_url=COMPANY_URL,
        tab_urls={"jobs-page-1": RESULTS_URL},
        tab_capture={"jobs-page-1": {"has_more": True}},
    )

    jobs = result["company"]["jobs"]
    assert jobs["reached_end"] is False
    assert jobs["items"] == [
        {
            "id": "201",
            "title": "Infrastructure Engineer",
            "url": "https://www.linkedin.com/jobs/view/201/",
            "company_name": "Acme",
            "location": "Chennai",
            "workplace_type": "Hybrid",
            "posted_at": "today",
            "posting_status": "posted",
            "insights": [],
            "page": 1,
            "position": 1,
        }
    ]
    assert "profile and resume" not in json.dumps(jobs).lower()
