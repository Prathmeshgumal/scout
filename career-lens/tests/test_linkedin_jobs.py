"""Job postings and keyword job searches: parsing, storage and reading back."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from career_lens.capture.failures import CaptureFailed
from career_lens.capture.writer import Document
from career_lens.linkedin import jobs

NOW = datetime(2026, 11, 4, 17, 30, tzinfo=UTC)
JOB_URL = "https://www.linkedin.com/jobs/view/4000000001/"
SEARCH_URL = ("https://www.linkedin.com/jobs/search/"
              "?keywords=backend+engineer&location=Bengaluru")

JOB_HTML = """
<html><head><title>(3) Backend Engineer | Acme | LinkedIn</title></head><body>
<main><div data-view-name="job-detail-page">
  <div componentkey="auto-binding-x-4000000001">
    <a href="https://www.linkedin.com/company/acme/life/">Acme</a>
    <p><span>Bengaluru, Karnataka, India</span> · <span>Reposted 3 days ago</span>
       · <span>Over 100 applicants</span></p>
    <p><span>Promoted by hirer ·</span><span>Responses managed off LinkedIn</span></p>
    <span>Remote</span><span>Full-time</span>
    <span>Apply</span><span>Save</span>
  </div>
  <div componentkey="JobDetails_AboutTheJob_4000000001">
    <h2>About the job</h2>
    <div data-testid="expandable-text-box"><strong>Overview</strong><br><br>Build
      payment services in Go.<br><br><strong>Requirements</strong><br>5 years of
      backend work.<br>Postgres.</div>
  </div>
</div></main></body></html>
"""

CLOSED_JOB_HTML = JOB_HTML.replace(
    "<span>Apply</span>", "<span>No longer accepting applications</span>")


def _document(section: str, html: str, *, url: str, metadata=None) -> Document:
    return Document(
        section=section, url_requested=url, url_final=url,
        host="www.linkedin.com", http_status=200, captured_at=NOW,
        title="T", html=html.encode(), scroll_stopped="bottom",
        metadata=metadata or {})


def test_a_job_page_gives_its_fields_and_full_description():
    job = jobs.build_job(JOB_URL, _document("job", JOB_HTML, url=JOB_URL))
    assert job == {
        "schema_version": "1.0",
        "kind": "linkedin_job",
        "job_url": JOB_URL,
        "job_id": "4000000001",
        "observed_at": NOW.isoformat(),
        "title": "Backend Engineer",
        "company_name": "Acme",
        "company_url": "https://www.linkedin.com/company/acme/",
        "location": "Bengaluru, Karnataka, India",
        "posted_at": "Reposted 3 days ago",
        "applicants": "Over 100 applicants",
        "workplace_type": "Remote",
        "employment_type": "Full-time",
        "posting_status": "open",
        "description_text": (
            "Overview\n\nBuild payment services in Go.\n\n"
            "Requirements\n5 years of backend work.\nPostgres."),
    }


def test_a_closed_job_says_it_is_closed():
    job = jobs.build_job(JOB_URL, _document("job", CLOSED_JOB_HTML, url=JOB_URL))
    assert job["posting_status"] == "closed"


def test_a_job_page_without_a_description_is_a_failed_read():
    html = JOB_HTML.replace('data-testid="expandable-text-box"', 'data-x="y"')
    with pytest.raises(CaptureFailed) as failed:
        jobs.build_job(JOB_URL, _document("job", html, url=JOB_URL))
    assert failed.value.failure_kind == "extract_failed"


def test_a_sign_in_page_is_never_a_job():
    html = ("<html><head><title>Sign Up | LinkedIn</title></head><body>"
            "<main><h1>Join LinkedIn</h1></main></body></html>")
    with pytest.raises(CaptureFailed):
        jobs.build_job(JOB_URL, _document("job", html, url=JOB_URL))


RESULT_PAGE_1 = """
<html><body><main><h1>Job listings</h1>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/101/">Platform Engineer</a></h2>
    <p>Acme</p><p>Bengaluru (Hybrid)</p><p>2 days ago</p>
  </article>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/102/">Data Engineer</a></h2>
    <p>Beta</p><p>Pune (On-site)</p><p>1 week ago</p>
  </article>
</main></body></html>
"""

RESULT_PAGE_2 = """
<html><body><main><h1>Job listings</h1>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/102/">Data Engineer</a></h2>
    <p>Beta</p><p>Pune (On-site)</p><p>1 week ago</p>
  </article>
  <article>
    <h2><a href="https://www.linkedin.com/jobs/view/103/">Go Developer</a></h2>
    <p>Gamma</p><p>Remote</p><p>3 weeks ago</p>
  </article>
</main></body></html>
"""


def _pages(last_has_more: bool):
    return [
        _document("jobs-page-1", RESULT_PAGE_1, url=SEARCH_URL,
                  metadata={"has_more": True}),
        _document("jobs-page-2", RESULT_PAGE_2, url=SEARCH_URL + "&start=25",
                  metadata={"has_more": last_has_more}),
    ]


def test_a_search_lists_each_job_once_across_pages():
    search = jobs.build_search(SEARCH_URL, _pages(last_has_more=False))
    assert search["keywords"] == "backend engineer"
    assert search["location"] == "Bengaluru"
    assert search["captured_pages"] == 2
    assert search["reached_end"] is True
    assert [item["id"] for item in search["items"]] == ["101", "102", "103"]
    assert search["items"][0]["workplace_type"] == "Hybrid"


def test_a_search_cut_by_the_page_limit_does_not_claim_the_end():
    search = jobs.build_search(SEARCH_URL, _pages(last_has_more=True))
    assert search["reached_end"] is False


def test_a_search_with_no_result_pages_is_a_failed_read():
    with pytest.raises(CaptureFailed) as failed:
        jobs.build_search(SEARCH_URL, [])
    assert failed.value.failure_kind == "extract_failed"


# Storage and the read API.

from sqlalchemy import text  # noqa: E402

from auth_support import operator_client  # noqa: E402
from career_lens.api.main import create_app  # noqa: E402
from career_lens.store.ids import CAPTURE, REQUEST, new_id  # noqa: E402


class MemoryStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get(self, key):
        return self.objects[key]

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None


@pytest.fixture
def client(app_url, monkeypatch):
    from career_lens.api import v1
    from career_lens.store import blobs, db

    store = MemoryStore()
    monkeypatch.setattr(blobs, "open_store", lambda: store)

    def database_override():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    application = create_app()
    application.dependency_overrides[v1.database] = database_override
    test_client = operator_client(application, app_url)
    test_client.store = store
    return test_client


def _request(app, *, kind, url, status="succeeded"):
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :url, :url, :kind, :status, :now)
    """), {"id": request_id, "url": url, "kind": kind, "status": status,
           "now": NOW})
    return request_id


def _capture(app, request_id, section, url):
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped, scroll_growing)
        VALUES (:id, :request, :section, :url, :url, 'www.linkedin.com', 200,
                :now, 'T', 'bottom', false)
    """), {"id": capture_id, "request": request_id, "section": section,
           "url": url, "now": NOW})
    return capture_id


def _stored_job(app, app_url, store):
    request_id = _request(app, kind="linkedin_job", url=JOB_URL)
    capture_id = _capture(app, request_id, "job", JOB_URL)
    app.commit()
    jobs.store_document(
        app_url, request_id=request_id, attempt_id=None,
        document=jobs.build_job(JOB_URL, _document("job", JOB_HTML, url=JOB_URL)),
        inputs={"job": capture_id}, store=store)
    return request_id


def _stored_search(app, app_url, store):
    request_id = _request(app, kind="linkedin_job_search", url=SEARCH_URL)
    inputs = {section: _capture(app, request_id, section, SEARCH_URL)
              for section in ("jobs-page-1", "jobs-page-2")}
    app.commit()
    jobs.store_document(
        app_url, request_id=request_id, attempt_id=None,
        document=jobs.build_search(SEARCH_URL, _pages(last_has_more=True)),
        inputs=inputs, store=store)
    return request_id


def test_a_stored_job_reads_back_by_any_spelling_of_its_url(client, app, app_url):
    request_id = _stored_job(app, app_url, client.store)
    response = client.get("/v1/linkedin-job", params={
        "url": "https://in.linkedin.com/jobs/view/backend-at-acme-4000000001?trk=x"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["request_id"] == request_id
    assert body["job"]["title"] == "Backend Engineer"
    assert body["job"]["description_text"].startswith("Overview")


def test_storing_a_job_twice_keeps_one_reading(client, app, app_url):
    request_id = _stored_job(app, app_url, client.store)
    capture_id = app.execute(text(
        "SELECT id FROM capture WHERE request_id=:r"), {"r": request_id}).scalar_one()
    jobs.store_document(
        app_url, request_id=request_id, attempt_id=None,
        document=jobs.build_job(JOB_URL, _document("job", JOB_HTML, url=JOB_URL)),
        inputs={"job": capture_id}, store=client.store)
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.job_observation")).scalar_one() == 1


def test_a_job_nobody_has_read_is_a_404(client):
    response = client.get("/v1/linkedin-job", params={"url": JOB_URL})
    assert response.status_code == 404


def test_a_finished_search_returns_its_jobs(client, app, app_url):
    request_id = _stored_search(app, app_url, client.store)
    response = client.get(f"/v1/linkedin-job-search/{request_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["result"]["reached_end"] is False
    assert [item["id"] for item in body["result"]["items"]] == ["101", "102", "103"]


def test_a_running_search_is_a_202(client, app):
    request_id = _request(app, kind="linkedin_job_search", url=SEARCH_URL,
                          status="running")
    app.commit()
    response = client.get(f"/v1/linkedin-job-search/{request_id}")
    assert response.status_code == 202
    assert response.json()["result"] is None


def test_a_failed_search_says_why_and_is_not_an_empty_list(client, app):
    request_id = _request(app, kind="linkedin_job_search", url=SEARCH_URL,
                          status="failed")
    app.commit()
    body = client.get(f"/v1/linkedin-job-search/{request_id}").json()
    assert body["status"] == "failed"
    assert body["result"] is None


def test_a_job_request_is_not_a_search(client, app):
    request_id = _request(app, kind="linkedin_job", url=JOB_URL)
    app.commit()
    assert client.get(f"/v1/linkedin-job-search/{request_id}").status_code == 404


def test_a_fresh_search_answers_a_new_request_for_it(client, app, app_url,
                                                     monkeypatch):
    _stored_search(app, app_url, client.store)
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    response = client.post("/v1/scrape/linkedin-job-search", json={
        "keywords": "backend engineer", "location": "Bengaluru",
        "freshness": "7d"})
    assert response.status_code == 200, response.text
    request_id = response.json()["request_id"]
    body = client.get(f"/v1/linkedin-job-search/{request_id}").json()
    assert body["status"] == "satisfied"
    assert len(body["result"]["items"]) == 3
