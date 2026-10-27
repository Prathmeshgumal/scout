"""The HTTP surface.

Against a real database, because every route is SQL and the interesting
failures are SQL failures. The queue is stubbed: what is being tested is the
API's half of the contract, and the worker has its own tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.api.main import create_app
from auth_support import customer_client, operator_client
from career_lens.api.requests import Submission
from career_lens.store.ids import EXTRACTION, REQUEST, new_id

NOW = datetime.now(UTC)


def _application(app_url):
    from career_lens.api import v1
    from career_lens.store import db

    def database_override():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    application = create_app()
    application.dependency_overrides[v1.database] = database_override
    return application


@pytest.fixture
def client(app_url):
    return operator_client(_application(app_url), app_url)


@pytest.fixture
def consumer(app_url):
    """Each call is a client with a new API key: another consumer."""
    application = _application(app_url)
    return lambda: customer_client(application, app_url)


def test_healthz_answers(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_posting_a_profile_url_returns_202_and_an_id(client, monkeypatch):
    from career_lens.api import requests as submission

    monkeypatch.setattr(submission, "submit",
                        lambda url, **kwargs: Submission("rq-test", True))
    response = client.post("/v1/scrape/linkedin-individual",
                           json={"url": "https://www.linkedin.com/in/jane/"})
    assert response.status_code == 202
    assert response.json()["request_id"] == "rq-test"
    assert response.json()["status"] == "queued"


def test_post_google_search_returns_query_target(client, monkeypatch):
    from career_lens.api import requests as submission

    monkeypatch.setattr(
        submission, "submit_google",
        lambda *args, **kwargs: (Submission("rq-google", True), "Enterprise AI", 5))
    response = client.post("/v1/scrape/google-search", json={
        "query": " Enterprise\u00a0AI ", "top_n": 5,
    })

    assert response.status_code == 202
    assert {key: response.json()[key] for key in ("request_id", "status", "target")} == {
        "request_id": "rq-google",
        "status": "queued",
        "target": {
            "kind": "google_search", "query": "Enterprise AI", "top_n": 5,
        },
    }


def test_google_submission_rejects_unknown_keys(client):
    response = client.post("/v1/scrape/google-search", json={
        "query": "AI", "country": "US",
    })

    assert response.status_code == 400
    assert "country" in response.json()["detail"]


def test_google_submission_rejects_a_url_as_a_query(client):
    response = client.post("/v1/scrape/google-search", json={
        "query": "https://example.com/article",
    })

    assert response.status_code == 400
    assert "Generic URL" in response.json()["detail"]


@pytest.mark.parametrize("body", [
    {"query": ""},
    {"query": "AI", "top_n": True},
    {"query": "AI", "top_n": 6},
    {"query": "AI", "top_n": 21},
])
def test_google_submission_rejects_invalid_query_options(client, body):
    """Six is refused as firmly as twenty-one: a search opens at most five
    destinations, and the ceiling is the API's to hold, not the caller's."""
    response = client.post("/v1/scrape/google-search", json=body)
    assert response.status_code == 400


def test_url_submission_returns_consumer_target(client, monkeypatch):
    from career_lens.api import requests as submission

    monkeypatch.setattr(submission, "submit",
                        lambda url, **kwargs: Submission("rq-url", True))
    response = client.post("/v1/scrape/url", json={
        "url": "https://example.com/article",
    })

    assert response.status_code == 202
    assert {key: response.json()[key] for key in ("request_id", "status", "target")} == {
        "request_id": "rq-url",
        "status": "queued",
        "target": {"kind": "generic", "url": "https://example.com/article"},
    }


def test_request_discovery_chance_is_rejected_as_worker_configuration(client):
    response = client.post("/v1/scrape/linkedin-individual", json={
        "url": "https://www.linkedin.com/in/jane/",
        "linkedin_google_search_chance": 0.75,
    })

    assert response.status_code == 400
    assert "worker" in response.json()["detail"].lower()


def test_posting_a_company_scrape_passes_selected_sections(client, monkeypatch):
    from career_lens.api import requests as submission

    received = {}

    def submit(url, **kwargs):
        received.update(kwargs)
        return Submission("rq-company-options", True)

    monkeypatch.setattr(submission, "submit", submit)
    response = client.post("/v1/scrape/linkedin-company", json={
        "url": "https://www.linkedin.com/company/acme/",
        "linkedin_sections": ["home", "posts"],
    })

    assert response.status_code == 202
    assert received["linkedin_sections"] == ["home", "posts"]


@pytest.mark.parametrize("body", [
    {"tempo": "slow"},
])
def test_invalid_capture_options_are_rejected(client, body):
    response = client.post("/v1/scrape/linkedin-individual", json={
        "url": "https://www.linkedin.com/in/jane/",
        **body,
    })

    assert response.status_code == 400


def test_submission_persists_a_nonblank_label(app, app_url, monkeypatch):
    from career_lens.api import requests as submission

    request_id, created, *_ = submission.submit(
        "https://www.linkedin.com/in/jane/", label="  Hiring research  ",
        database=app_url,
    )

    assert created is True
    label = app.execute(text(
        "SELECT label FROM scrape_request WHERE id = :r"),
        {"r": request_id}).scalar_one()
    assert label == "Hiring research"


def test_company_submission_persists_selected_sections(
    app, app_url, monkeypatch,
):
    from career_lens.api import requests as submission

    request_id, created, *_ = submission.submit(
        "https://www.linkedin.com/company/acme/",
        linkedin_sections=["posts", "home", "posts"],
        database=app_url,
    )

    assert created is True
    sections = app.execute(text("""
        SELECT linkedin_sections
        FROM scrape_request WHERE id = :r
    """), {"r": request_id}).scalar_one()
    assert sections == ["home", "posts"]


def test_a_profile_url_posted_to_the_company_endpoint_is_a_400(client):
    response = client.post("/v1/scrape/linkedin-company",
                           json={"url": "https://www.linkedin.com/in/jane/"})
    assert response.status_code == 400
    assert "linkedin_person" in response.json()["detail"]


def test_an_unusable_url_is_a_400_naming_the_problem(client):
    response = client.post("/v1/scrape/url", json={"url": "ftp://host/file"})
    assert response.status_code == 400
    assert "ftp" in response.json()["detail"]


def test_an_unknown_scrape_endpoint_is_a_404(client):
    response = client.post("/v1/scrape/telepathy",
                           json={"url": "https://example.com/"})
    assert response.status_code == 404


def test_an_unknown_request_is_a_404(client):
    assert client.get("/v1/requests/rq-nothing").status_code == 404


def test_a_person_nobody_has_scraped_is_a_404(client):
    response = client.get(
        "/v1/linkedin-individual?url=https://www.linkedin.com/in/nobody/")
    assert response.status_code == 404


def test_a_company_nobody_has_scraped_is_a_404(client):
    response = client.get(
        "/v1/linkedin-company?url=https://www.linkedin.com/company/nobody/")
    assert response.status_code == 404


def test_a_company_response_keeps_header_data_and_section_statuses(client, app):
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/company/acme/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_company', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_company_json', '1.0', :t, 'partial',
                'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.company_observation
            (extraction_id, slug, company_url, name, verified, tagline,
             industry, location, followers, employee_count, external_action,
             observed_at)
        VALUES (:e, 'acme', :u, 'Acme', true, 'Build useful things',
                'Software Development', 'London', '2M', '5K-10K',
                CAST(:action AS jsonb), :t)
        RETURNING id
    """), {"e": eid, "u": url, "t": NOW,
            "action": '{"label":"Visit website","url":"https://acme.test"}'}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.company_section_status
            (observation_id, section, status, source_url, reached_end, data,
             observed_at)
            VALUES
                (:o, 'home', 'parsed', :u, null,
                 CAST('{"featured":[],"newsletters":[]}' AS jsonb), :t),
                (:o, 'events', 'parsed', :events, null,
                 CAST(:events_data AS jsonb), :t),
                (:o, 'services', 'parsed', :services, null,
                 CAST(:services_data AS jsonb), :t),
                (:o, 'posts', 'parsed', :posts, false,
                 CAST(:posts_data AS jsonb), :t),
                (:o, 'products', 'not_available', null, null, null, :t),
                (:o, 'life', 'parse_failed', :life, null, null, :t)
    """), {
        "o": observation,
        "u": url,
        "t": NOW,
        "events": f"{url}events/",
        "events_data": '{"upcoming":[],"past":[]}',
        "services": f"{url}services/",
        "services_data": (
            '{"overview":null,"availability":"Remote",'
            '"pricing":"Contact for pricing","services":[]}'
        ),
        "posts": f"{url}posts/",
        "posts_data": '{"source_url":"posts","reached_end":false,"items":[]}',
        "life": f"{url}life/",
    })

    body = client.get(f"/v1/linkedin-company?url={url}").json()

    assert body["entity"] == {
        "kind": "linkedin_company", "handle": "acme", "url": url,
    }
    assert body["data"]["profile"]["followers"] == "2M"
    assert body["data"]["profile"]["employee_count"] == "5K-10K"
    assert body["data"]["profile"]["external_action"]["label"] == "Visit website"
    assert body["data"]["events"] == {
        "data": {"upcoming": [], "past": []},
    }
    assert body["meta"]["events"]["status"] == "parsed"
    assert body["meta"]["events"]["source_url"] == f"{url}events/"
    assert "reached_end" not in body["meta"]["events"]
    assert body["data"]["services"]["data"]["availability"] == "Remote"
    assert body["data"]["posts"] == {
        "data": {"items": []},
    }
    assert body["meta"]["posts"]["status"] == "parsed"
    assert body["meta"]["posts"]["source_url"] == f"{url}posts/"
    assert body["meta"]["posts"]["reached_end"] is False
    assert body["meta"]["products"]["status"] == "not_available"
    assert body["meta"]["life"]["status"] == "parse_failed"
    assert body["meta"]["about"]["status"] == "not_captured"


def test_a_section_reports_its_status_beside_its_entries(client, app):
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/in/jane/", "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.1', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, 'jane', 'https://www.linkedin.com/in/jane/', 'Jane', :observed_at)
        RETURNING id
    """), {"e": eid, "observed_at": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'publications', 'parsed', :t),
               (:o, 'patents', 'not_captured', :t)
    """), {"o": observation, "t": NOW})

    body = client.get(
        "/v1/linkedin-individual?url=https://www.linkedin.com/in/jane/").json()
    assert body["data"]["publications"] == {"entries": []}
    assert body["data"]["patents"] == {"entries": []}
    assert body["meta"]["publications"]["status"] == "parsed"
    assert body["meta"]["patents"]["status"] == "not_captured"


@pytest.mark.parametrize(("section", "field", "table_name"), (
    ("volunteering", "volunteering", "person_volunteering"),
    ("test-scores", "test_scores", "person_test_score"),
    ("services", "services", "person_service"),
    ("connected-apps", "connected_apps", "person_connected_app"),
))
def test_new_profile_sections_are_returned_losslessly(
        client, app, section: str, field: str, table_name: str):
    import json

    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = f"https://www.linkedin.com/in/{field}/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '2.1', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, :handle, :u, 'Example', :t) RETURNING id
    """), {"e": eid, "handle": field, "u": url, "t": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, :section, 'parsed_inline', :t)
    """), {"o": observation, "section": section, "t": NOW})
    entry = {"name": "Example entry", "details": ["Detail"], "extra": 7}
    app.execute(text(f"""
        INSERT INTO linkedin.{table_name}
            (observation_id, ordinal, name, data)
        VALUES (:o, 0, 'Example entry', CAST(:data AS jsonb))
    """), {"o": observation, "data": json.dumps(entry)})

    body = client.get(f"/v1/linkedin-individual?url={url}").json()

    assert body["data"][field] == {"entries": [entry]}
    assert body["meta"][field]["status"] == "parsed_inline"


def test_a_drifted_section_does_not_hide_the_rest_of_the_profile(client, app):
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/in/drifted/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'failed', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker, browser_profile, started_at,
             ended_at, cause, cause_detail)
        VALUES (:r, 1, 'test-worker', 'test', :t, :t, 'parser_drift',
                'activity-posts: feed activity cards - expected at least one card, found 0')
    """), {"r": rid, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.5', :t, 'suspect',
                'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, 'drifted', :u, 'Drifted Person', :observed_at) RETURNING id
    """), {"e": eid, "u": url, "observed_at": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'activity-posts', 'drift', :t)
    """), {"o": observation, "t": NOW})

    # A drifted section is reported as drifted; it does not put the twenty
    # readable sections beside it behind a 409. There is no earlier reading to
    # walk back to here, so the section is simply empty either way -- what
    # differs is that `meta` names the drift under allow_stale=false.
    # There is no earlier good reading to walk back to, so both modes report
    # the drift rather than an absence -- "we looked and could not read it"
    # is not "we never looked".
    lenient = client.get(f"/v1/linkedin-individual?url={url}")
    assert lenient.status_code == 200
    assert lenient.json()["data"]["profile"]["name"] == "Drifted Person"
    assert lenient.json()["meta"]["activity"]["posts"]["status"] == "drift"

    strict = client.get(f"/v1/linkedin-individual?url={url}&allow_stale=false")
    assert strict.status_code == 200
    assert strict.json()["meta"]["activity"]["posts"]["status"] == "drift"
    assert strict.json()["data"]["activity"]["posts"]["entries"] == []


def test_person_api_preserves_rich_parser_sections(client, app):
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/in/ada/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.1', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, 'ada', :u, 'Ada', :observed_at) RETURNING id
    """), {"e": eid, "u": url, "observed_at": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'education', 'parsed_inline', :t),
               (:o, 'skills', 'parsed_inline', :t),
               (:o, 'recommendations', 'parsed', :t),
               (:o, 'recommendations-given', 'not_captured', :t),
               (:o, 'activity-reactions', 'parsed', :t)
    """), {"o": observation, "t": NOW})
    app.execute(text("""
        INSERT INTO linkedin.person_education
            (observation_id, ordinal, school, date_range, started_on,
             ended_on, is_current)
        VALUES (:o, 0, 'Example University', '2020 - Present', '2020',
                NULL, true)
    """), {"o": observation})
    app.execute(text("""
        INSERT INTO linkedin.person_skill
            (observation_id, ordinal, name, details)
        VALUES (:o, 0, 'Mathematics',
                '[{"text":"99+ endorsements","url":"https://example/99"}]')
    """), {"o": observation})
    app.execute(text("""
        INSERT INTO linkedin.person_recommendation
            (observation_id, direction, ordinal, person_name, person_headline,
             recommended_at, relationship, text)
        VALUES (:o, 'received', 0, 'Charles', 'Inventor', 'April 10, 1843',
                'Charles was Ada''s client', 'Excellent work')
    """), {"o": observation})
    app.execute(text("""
        INSERT INTO linkedin.person_activity
            (observation_id, kind, ordinal, author_name, text, engagement)
        VALUES (:o, 'reaction', 0, 'Ada', 'Reacted to a note',
                CAST(:engagement AS jsonb))
    """), {"o": observation,
            "engagement": (
                '{"reactions":{"count":4,"types":["like"]},'
                '"comments":0,"reposts":0}'
            )})

    response = client.get(f"/v1/linkedin-individual?url={url}").json()
    body = response["data"]

    assert response["meta"]["skills"]["status"] == "parsed_inline"
    assert body["education"]["entries"][0]["is_current"] is True
    assert body["skills"] == {
        "entries": [{
            "name": "Mathematics", "details": [{
                "text": "99+ endorsements", "url": "https://example/99"}]}]}
    assert body["recommendations"]["received"]["entries"][0] == {
        "person": {"name": "Charles", "headline": "Inventor", "company": None,
                   "connection_degree": None, "profile_url": None},
        "date": "April 10, 1843", "relationship": "Charles was Ada's client",
        "text": "Excellent work"}
    assert body["activity"]["reactions"]["entries"][0]["engagement"] == {
        "reactions": {"count": 4, "types": ["like"]},
        "comments": 0,
        "reposts": 0,
    }


def test_person_api_returns_lossless_schema_1_4_sections(client, app):
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/in/schema-14/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.4', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, pronouns,
             current_company, top_skills, causes, observed_at)
        VALUES (:e, 'schema-14', :u, 'Ada', 'She/Her', 'Engine Works',
                '["Systems Design"]', '["Education"]', :observed_at) RETURNING id
    """), {"e": eid, "u": url, "observed_at": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'certifications', 'parsed', :t),
               (:o, 'interests', 'parsed', :t),
               (:o, 'causes', 'parsed_inline', :t),
               (:o, 'activity-posts', 'parsed', :t)
    """), {"o": observation, "t": NOW})
    certification = {
        "name": "Cloud Architecture", "credential_id": "ABC-123",
        "issuer": {"name": "Udemy"}, "skills": ["Systems Design"],
    }
    interest = {
        "name": "Systems", "cadence": "Published weekly",
        "publisher": {"name": "Engine Works"},
    }
    activity = {
        "position": 1, "relation": "reposted",
        "post": {"author": {"name": "Ada"},
                 "commentary": {"text": "A milestone"}},
    }
    import json
    app.execute(text("""
        INSERT INTO linkedin.person_certification
            (observation_id, ordinal, name, data)
        VALUES (:o, 0, 'Cloud Architecture', CAST(:data AS jsonb))
    """), {"o": observation, "data": json.dumps(certification)})
    app.execute(text("""
        INSERT INTO linkedin.person_interest
            (observation_id, ordinal, category, name, data)
        VALUES (:o, 0, 'newsletters', 'Systems', CAST(:data AS jsonb))
    """), {"o": observation, "data": json.dumps(interest)})
    app.execute(text("""
        INSERT INTO linkedin.person_activity_summary
            (observation_id, kind, source_url, loaded, captured, complete)
        VALUES (:o, 'post', :u, 3, 1, false)
    """), {"o": observation, "u": f"{url}recent-activity/all/"})
    app.execute(text("""
        INSERT INTO linkedin.person_activity
            (observation_id, kind, ordinal, data)
        VALUES (:o, 'post', 0, CAST(:data AS jsonb))
    """), {"o": observation, "data": json.dumps(activity)})

    response = client.get(f"/v1/linkedin-individual?url={url}").json()
    body = response["data"]
    assert response["meta"]["causes"]["status"] == "parsed_inline"
    assert response["meta"]["interests"]["newsletters"]["status"] == "parsed"
    assert "source_url" not in response["meta"]["interests"]["newsletters"]
    assert response["meta"]["activity"]["posts"]["status"] == "parsed"
    assert response["meta"]["activity"]["posts"]["source_url"] == f"{url}recent-activity/all/"

    assert body["profile"]["pronouns"] == "She/Her"
    assert body["profile"]["current_company"] == "Engine Works"
    assert body["profile"]["top_skills"] == ["Systems Design"]
    # A section like any other now: the status says whether anyone looked,
    # which a bare list could not, so an unlisted cause is distinguishable
    # from an unread card.
    assert body["causes"] == {"entries": ["Education"]}
    assert body["certifications"]["entries"] == [certification]
    assert body["interests"] == {
        "companies": None, "groups": None, "schools": None, "top_voices": None,
        "newsletters": {"entries": [interest]},
    }
    assert body["activity"]["posts"] == {
        "loaded": 3, "captured": 1, "complete": False,
        "entries": [activity],
    }


def test_no_v1_route_exposes_a_captured_page():
    application = create_app()
    v1_paths = [path for route in application.routes
                if (path := getattr(route, "path", None)) is not None
                and path.startswith("/v1")]
    for path in v1_paths:
        assert "capture" not in path and "markdown" not in path, path


def _attempt(connection, request_id, **overrides):
    values = {"r": request_id, "n": 1, "w": "host:9", "p": "research",
              "started": NOW, "ended": None, "cause": None, "detail": None}
    values.update(overrides)
    connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker, browser_profile, started_at,
             ended_at, cause, cause_detail)
        VALUES (:r, :n, :w, :p, :started, :ended, :cause, :detail)
    """), values)


def test_the_public_request_view_withholds_the_worker(client, app):
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'running', :t)
    """), {"r": rid, "u": "https://example.com/y", "t": NOW})
    _attempt(app, rid)

    response = client.get(f"/v1/requests/{rid}")
    assert response.status_code == 202
    assert response.json()["status"] == "running"
    assert "attempts" not in response.json()
    assert "browser_profile" not in response.text


def _real_submit(monkeypatch, app_url):
    """Let submit() run for real, against this test's database.

    The `client` fixture overrides the read routes' dependency, but submit()
    opens its own transaction through store.config.database_url(), which reads
    the environment on every call. Pointing that at the clone is enough; no
    internals need patching.
    """
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)


def test_google_submission_normalises_and_deduplicates_by_top_n(
        app, app_url, monkeypatch):
    from career_lens.api import requests as submission

    _real_submit(monkeypatch, app_url)
    (first, created, *_), display, top_n = submission.submit_google(
        "  Straße\u00a0 Research ", top_n=4, database=app_url)
    (duplicate, duplicated, *_), duplicate_display, _ = submission.submit_google(
        "STRASSE RESEARCH", top_n=4, priority="high", database=app_url)
    (other_top_n, other_created, *_), _, _ = submission.submit_google(
        "strasse research", top_n=5, database=app_url)

    assert (created, display, top_n) == (True, "Straße Research", 4)
    assert (duplicate, duplicated, duplicate_display) == (
        first, False, "STRASSE RESEARCH")
    assert other_created is True and other_top_n != first
    row = app.execute(text("""
        SELECT query_requested, query_normalised, google_top_n,
               url_requested, url_normalised, priority
        FROM scrape_request WHERE id=:request
    """), {"request": first}).one()
    assert row._mapping == {
        "query_requested": "Straße Research",
        "query_normalised": "strasse research",
        "google_top_n": 4,
        "url_requested": None,
        "url_normalised": None,
        "priority": "high",
    }


def test_completed_google_search_can_be_submitted_again(app, app_url, monkeypatch):
    from career_lens.api import requests as submission

    _real_submit(monkeypatch, app_url)
    (first, _, *_), _, _ = submission.submit_google("AI", database=app_url)
    app.execute(text("""
        UPDATE scrape_request SET status='succeeded' WHERE id=:request
    """), {"request": first})

    (second, created, *_), _, _ = submission.submit_google("AI", database=app_url)
    assert created is True and second != first


def test_a_request_records_the_ceiling_it_was_given(client, app, app_url,
                                                    monkeypatch):
    _real_submit(monkeypatch, app_url)

    response = client.post("/v1/scrape/url", json={
        "url": "https://example.com/ceiling", "max_attempts": 5})

    assert response.status_code == 202
    assert app.execute(text(
        "SELECT max_attempts FROM scrape_request WHERE id=:r"),
        {"r": response.json()["request_id"]}).scalar_one() == 5


def test_a_ceiling_below_one_is_refused(client, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)

    response = client.post("/v1/scrape/url", json={
        "url": "https://example.com/floor", "max_attempts": 0})

    assert response.status_code == 400
    assert "max_attempts" in response.json()["detail"]


def test_a_ceiling_that_is_not_a_whole_number_is_refused(client, app_url,
                                                         monkeypatch):
    _real_submit(monkeypatch, app_url)

    for bad in ("three", 2.5, True, []):
        response = client.post("/v1/scrape/url", json={
            "url": "https://example.com/kind", "max_attempts": bad})
        assert response.status_code == 400, bad


def test_a_url_in_backoff_cannot_be_submitted_twice(client, app, app_url,
                                                    monkeypatch):
    """Why a retrying request stays `queued` rather than sitting at `failed`:
    the outstanding-URL index covers only queued, assigned and running, so a
    request parked at `failed` between tries would leave the URL free for a
    duplicate to race the retry."""
    _real_submit(monkeypatch, app_url)
    url = "https://example.com/only-once"
    first = client.post("/v1/scrape/url", json={"url": url}).json()["request_id"]
    app.execute(text("""
        UPDATE scrape_request
        SET next_attempt_at = now() + interval '5 minutes',
            dispatch_wait_reason = 'retry_backoff'
        WHERE id = :r
    """), {"r": first})
    app.commit()

    second = client.post("/v1/scrape/url", json={"url": url}).json()

    assert second["request_id"] == first
    assert second["status"] == "queued"
    assert app.execute(text(
        "SELECT count(*) FROM scrape_request WHERE url_normalised=:u"),
        {"u": url}).scalar_one() == 1


def test_retrying_a_request_that_does_not_exist_is_a_404(client, app_url,
                                                         monkeypatch):
    _real_submit(monkeypatch, app_url)

    assert client.post("/v1/requests/rq-nothing/retry").status_code == 404


def test_cancelling_a_queued_request_stops_it_immediately(
        client, app, app_url, monkeypatch):
    """Nothing holds the work, so the API is what closes it."""
    _real_submit(monkeypatch, app_url)
    request_id = client.post("/v1/scrape/url", json={
        "url": "https://example.com/never-mind"}).json()["request_id"]

    response = client.post(f"/v1/requests/{request_id}/cancel")

    row = app.execute(text("""
        SELECT status, cancel_requested_at, next_attempt_at
        FROM scrape_request WHERE id=:r
    """), {"r": request_id}).one()
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert row.status == "cancelled"
    assert row.cancel_requested_at is not None
    assert row.next_attempt_at is None


def test_a_second_cancel_keeps_the_first_timestamp(
        client, app, app_url, monkeypatch):
    """So the console can show how long a stop has been pending."""
    _real_submit(monkeypatch, app_url)
    request_id = client.post("/v1/scrape/url", json={
        "url": "https://example.com/twice"}).json()["request_id"]
    app.execute(text(
        "UPDATE scrape_request SET status='running' WHERE id=:r"),
        {"r": request_id})
    app.commit()
    client.post(f"/v1/requests/{request_id}/cancel")
    first = app.execute(text(
        "SELECT cancel_requested_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one()
    app.commit()

    response = client.post(f"/v1/requests/{request_id}/cancel")

    assert response.status_code == 202
    assert app.execute(text(
        "SELECT cancel_requested_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == first


def test_cancelling_a_finished_request_is_refused(
        client, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    for status in ("succeeded", "failed", "cancelled"):
        request_id = client.post("/v1/scrape/url", json={
            "url": f"https://example.com/done-{status}"}).json()["request_id"]
        app.execute(text(
            "UPDATE scrape_request SET status=:s WHERE id=:r"),
            {"r": request_id, "s": status})
        app.commit()

        response = client.post(f"/v1/requests/{request_id}/cancel")

        assert response.status_code == 409, status


def test_cancelling_a_request_that_does_not_exist_is_a_404(
        client, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)

    assert client.post("/v1/requests/rq-nothing/cancel").status_code == 404


def test_a_cancelled_url_can_be_submitted_again_at_once(
        client, app, app_url, monkeypatch):
    """The outstanding-URL index names the live statuses explicitly, so
    `cancelled` frees the URL. This is the case the feature exists for: an
    accidental submission the operator wants to redo."""
    _real_submit(monkeypatch, app_url)
    url = "https://example.com/mistake"
    first = client.post("/v1/scrape/url", json={"url": url}).json()["request_id"]
    client.post(f"/v1/requests/{first}/cancel")

    second = client.post("/v1/scrape/url", json={"url": url}).json()

    assert second["request_id"] != first
    assert second["status"] == "queued"


def _interests(app, request_id):
    """Each interest in a request as (has a key, withdrawn)."""
    return sorted(app.execute(text("""
        SELECT api_key_id IS NOT NULL, withdrawn_at IS NOT NULL
        FROM request_interest WHERE request_id=:r
    """), {"r": request_id}).all())


def test_a_consumer_cancel_stops_a_request_that_only_it_wants(
        consumer, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    key = consumer()
    request_id = key.post("/v1/scrape/url", json={
        "url": "https://example.com/only-mine"}).json()["request_id"]

    response = key.post(f"/v1/requests/{request_id}/cancel")

    assert response.status_code == 200
    assert response.json()["cancel"] == "cancelled"
    assert response.json()["status"] == "cancelled"
    assert _interests(app, request_id) == [(True, True)]


def test_a_consumer_cancel_leaves_work_that_another_key_still_wants(
        consumer, app, app_url, monkeypatch):
    """One request serves both keys. The first to cancel only leaves it."""
    _real_submit(monkeypatch, app_url)
    first, second = consumer(), consumer()
    url = "https://www.linkedin.com/in/shared-profile/"
    request_id = first.post("/v1/scrape/linkedin-individual",
                            json={"url": url}).json()["request_id"]
    assert second.post("/v1/scrape/linkedin-individual",
                       json={"url": url}).json()["request_id"] == request_id

    response = first.post(f"/v1/requests/{request_id}/cancel")

    assert response.status_code == 200
    assert response.json()["cancel"] == "withdrawn"
    assert response.json()["status"] == "queued"
    assert _interests(app, request_id) == [(True, False), (True, True)]
    response = second.post(f"/v1/requests/{request_id}/cancel")
    assert response.json()["cancel"] == "cancelled"


def test_a_submission_without_a_key_records_one_interest(app, app_url, monkeypatch):
    """Schedules and the CLI call submit() with no key."""
    from career_lens.api import requests as submission

    _real_submit(monkeypatch, app_url)
    first = submission.submit("https://example.com/scheduled")
    again = submission.submit("https://example.com/scheduled")

    assert again.request_id == first.request_id
    assert _interests(app, first.request_id) == [(False, False)]


def test_a_submission_during_a_cancel_waits_and_gets_new_work(
        app, app_url, monkeypatch):
    """The cancel holds the request FOR UPDATE and submit() reads it FOR KEY
    SHARE. So the submission waits, sees the request closed, and queues new
    work, rather than joining a request that nobody wants any more."""
    import threading

    from career_lens.api import requests as submission
    from career_lens.store import db

    _real_submit(monkeypatch, app_url)
    url = "https://example.com/raced"
    first = submission.submit(url)
    answers = {}
    with db.transaction(app_url) as cancel:
        cancel.execute(text(
            "SELECT 1 FROM scrape_request WHERE id=:r FOR UPDATE"),
            {"r": first.request_id})
        waiting = threading.Thread(
            target=lambda: answers.update(again=submission.submit(url)))
        waiting.start()
        waiting.join(timeout=1)
        assert waiting.is_alive()
        cancel.execute(text(
            "UPDATE scrape_request SET status='cancelled' WHERE id=:r"),
            {"r": first.request_id})
    waiting.join(timeout=10)

    assert answers["again"].request_id != first.request_id
    assert answers["again"].created


def test_a_consumer_cancel_of_running_work_asks_its_worker_to_stop(
        consumer, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    key = consumer()
    request_id = key.post("/v1/scrape/url", json={
        "url": "https://example.com/mine-in-flight"}).json()["request_id"]
    app.execute(text(
        "UPDATE scrape_request SET status='running' WHERE id=:r"),
        {"r": request_id})
    app.commit()

    response = key.post(f"/v1/requests/{request_id}/cancel")
    again = key.post(f"/v1/requests/{request_id}/cancel")

    row = app.execute(text(
        "SELECT status, cancel_requested_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert response.status_code == again.status_code == 202
    assert response.json()["cancel"] == again.json()["cancel"] == "stopping"
    assert row.status == "running"
    assert row.cancel_requested_at is not None


def test_a_key_cannot_cancel_a_request_that_it_did_not_ask_for(
        consumer, app_url, monkeypatch):
    """A 404 as for a request that does not exist, so a key learns nothing
    about the work of other callers."""
    _real_submit(monkeypatch, app_url)
    owner, stranger = consumer(), consumer()
    request_id = owner.post("/v1/scrape/url", json={
        "url": "https://example.com/not-yours"}).json()["request_id"]

    assert stranger.post(f"/v1/requests/{request_id}/cancel").status_code == 404
    assert stranger.post("/v1/requests/rq-nothing/cancel").status_code == 404
    assert owner.get(f"/v1/requests/{request_id}").json()["status"] == "queued"


def test_a_key_that_asks_again_after_it_withdrew_wants_the_work_again(
        consumer, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    first, second = consumer(), consumer()
    url = "https://example.com/changed-my-mind"
    request_id = first.post("/v1/scrape/url", json={"url": url}).json()["request_id"]
    second.post("/v1/scrape/url", json={"url": url})
    first.post(f"/v1/requests/{request_id}/cancel")

    first.post("/v1/scrape/url", json={"url": url})
    response = second.post(f"/v1/requests/{request_id}/cancel")

    assert response.json()["cancel"] == "withdrawn"
    assert response.json()["status"] == "queued"


def test_a_consumer_cannot_cancel_a_finished_request(
        consumer, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    key = consumer()
    request_id = key.post("/v1/scrape/url", json={
        "url": "https://example.com/already-done"}).json()["request_id"]
    app.execute(text(
        "UPDATE scrape_request SET status='succeeded' WHERE id=:r"),
        {"r": request_id})
    app.commit()

    assert key.post(f"/v1/requests/{request_id}/cancel").status_code == 409


def test_a_consumer_cancels_its_search(consumer, app, app_url, monkeypatch):
    _real_submit(monkeypatch, app_url)
    key = consumer()
    request_id = key.post("/v1/search", json={
        "query": "acme engineering jobs", "wait_seconds": 0}).json()["request_id"]

    response = key.post(f"/v1/requests/{request_id}/cancel")

    assert response.json()["cancel"] == "cancelled"


def _two_attempts(app, request_id: str) -> None:
    """Two closed attempts, each with one journal line and one capture."""
    for number, cause in ((1, "timeout"), (2, None)):
        attempt_id = app.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, started_at, ended_at,
                 cause)
            VALUES (:r, :n, now(), now(), now(), :cause)
            RETURNING id
        """), {"r": request_id, "n": number, "cause": cause}).scalar_one()
        app.execute(text("""
            INSERT INTO attempt_event
                (attempt_id, at, elapsed_ms, kind, level, phase, detail)
            VALUES (:a, now(), 1000, 'net', 'info', :phase, '')
        """), {"a": attempt_id, "phase": f"attempt {number} phase"})
    app.commit()


def test_asking_for_an_attempt_that_never_happened_is_a_404(
        client, app, app_url, monkeypatch):
    """An empty journal would read as 'that attempt did nothing', which is a
    different and wrong answer."""
    _real_submit(monkeypatch, app_url)
    request_id = client.post("/v1/scrape/url", json={
        "url": "https://example.com/only-two"}).json()["request_id"]
    _two_attempts(app, request_id)

    assert client.get(
        f"/v1/requests/{request_id}/events?attempt=9").status_code == 404


def test_a_google_search_runs_at_the_tempo_it_was_asked_for(
        app, app_url, monkeypatch):
    """The worker has always paced these runs -- `_work_google` reads
    `request.tempo` -- but nothing could set it, so every query took the
    column default."""
    from career_lens.api import requests as submission

    _real_submit(monkeypatch, app_url)
    (request_id, _, *_), _, _ = submission.submit_google(
        "Enterprise AI", top_n=2, tempo="human", database=app_url)

    tempo = app.execute(text(
        "SELECT tempo FROM scrape_request WHERE id=:request"),
        {"request": request_id}).scalar_one()
    assert tempo == "human"


def test_a_google_search_without_a_tempo_keeps_the_default(
        app, app_url, monkeypatch):
    from career_lens.api import requests as submission

    _real_submit(monkeypatch, app_url)
    (request_id, _, *_), _, _ = submission.submit_google(
        "Enterprise AI", top_n=2, database=app_url)

    tempo = app.execute(text(
        "SELECT tempo FROM scrape_request WHERE id=:request"),
        {"request": request_id}).scalar_one()
    assert tempo == "human"


def test_a_google_search_refuses_a_tempo_the_worker_cannot_run(
        app, app_url, monkeypatch):
    from career_lens.api import requests as submission
    from career_lens.capture.options import InvalidCaptureOption

    _real_submit(monkeypatch, app_url)
    with pytest.raises(InvalidCaptureOption) as refused:
        submission.submit_google(
            "Enterprise AI", tempo="leisurely", database=app_url)
    assert "human, research, brisk, fast" in str(refused.value)


def _person_extraction(app, request_id: str, statuses: dict[str, str]) -> None:
    """One extraction with its projected section statuses, and nothing else."""
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES ('ex-sections', :r, 'linkedin_profile_json', '1', now(), 'ok',
                'documents/ex-sections.json', repeat('a', 64), 2)
    """), {"r": request_id})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, observed_at)
        VALUES ('ex-sections', 'jane', 'https://www.linkedin.com/in/jane/',
                :observed_at)
        RETURNING id
    """), {"observed_at": NOW}).scalar_one()
    for section, status in statuses.items():
        app.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at)
            VALUES (:o, :s, :status, :t)
        """), {"o": observation, "s": section, "status": status, "t": NOW})
    app.commit()


def test_about_this_member_is_a_section_on_the_v1_profile(client, app):
    """The dialog was projected and then never handed to anyone.

    person_observation.about_this_member has been written since the column
    existed, and /v1 emitted no key for it -- so the one place on LinkedIn that
    says when an account was made was readable only by opening its capture.

    Not the {status, entries} envelope the list sections use: this section is
    one record rather than a list of them, the way `interests` is a map rather
    than a list and carries its own shape too.
    """
    import json

    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/in/about-member/"
    about = {
        "account_history": {
            "rows": [{"label": "Joined LinkedIn", "value": "June 2022"}],
        },
        "verifications": [{
            "entity": "BlackRock",
            "detail": "Verified using work email in August 2025",
            "method": "work email",
            "verified_on": "August 2025",
            "logo_url": "https://media.licdn.com/blackrock.png",
        }],
        "links": [],
    }
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.9', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, about_this_member,
             observed_at)
        VALUES (:e, 'about-member', :u, 'Ada', CAST(:a AS jsonb), :t)
        RETURNING id
    """), {"e": eid, "u": url, "a": json.dumps(about), "t": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'about-this-member', 'parsed', :t)
    """), {"o": observation, "t": NOW})

    response = client.get(f"/v1/linkedin-individual?url={url}").json()
    assert response["meta"]["about_this_member"]["status"] == "parsed"
    assert response["data"]["about_this_member"] == about


def test_a_profile_whose_about_dialog_never_opened_still_has_the_key(client, app):
    """A section that says nothing must still say that it says nothing.

    An absent key reads as a build that does not know about this section; a
    present one with a status says the run looked and came back empty.
    """
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    url = "https://www.linkedin.com/in/no-about-member/"
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.9', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, 'no-about-member', :u, 'Ada', :t)
    """), {"e": eid, "u": url, "t": NOW})

    response = client.get(f"/v1/linkedin-individual?url={url}").json()
    assert response["meta"]["about_this_member"]["status"] == "not_captured"
    assert response["data"]["about_this_member"] == {
        "account_history": {"rows": []},
        "verifications": [],
        "links": [],
    }


def test_all_scrape_endpoints_reject_unknown_fields(
        client, monkeypatch):
    # Stray consumer controls must not be silently ignored by older routes.
    from career_lens.api import requests as submission

    monkeypatch.setattr(submission, "submit",
                        lambda url, **kwargs: Submission("rq-old", True))
    for path, url in (
        ("linkedin-individual", "https://www.linkedin.com/in/jane/"),
        ("url", "https://example.com/article"),
    ):
        response = client.post(f"/v1/scrape/{path}", json={
            "url": url, "something_we_never_read": 1})
        assert response.status_code == 400
        assert "something_we_never_read" in response.json()["detail"]




def test_a_company_request_reads_five_job_pages_by_default(app, app_url):
    from career_lens.api import requests as submission

    request_id, *_ = submission.submit(
        "https://www.linkedin.com/company/acme/", database=app_url)
    assert app.execute(text(
        "SELECT job_pages FROM scrape_request WHERE id = :r"),
        {"r": request_id}).scalar_one() == 5


def test_a_company_request_keeps_the_job_pages_it_asked_for(
        client, app, app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    response = client.post("/v1/scrape/linkedin-company", json={
        "url": "https://www.linkedin.com/company/acme/", "job_pages": 12})
    assert response.status_code == 202, response.text
    assert app.execute(text(
        "SELECT job_pages FROM scrape_request WHERE id = :r"),
        {"r": response.json()["request_id"]}).scalar_one() == 12


@pytest.mark.parametrize("pages", [0, 21, "5", True, 2.5])
def test_job_pages_out_of_range_is_a_400(client, pages):
    response = client.post("/v1/scrape/linkedin-company", json={
        "url": "https://www.linkedin.com/company/acme/", "job_pages": pages})
    assert response.status_code == 400
    assert "job_pages" in response.json()["detail"]


@pytest.mark.parametrize("path,body", [
    ("linkedin-individual", {"url": "https://www.linkedin.com/in/jane/"}),
    ("url", {"url": "https://example.com/"}),
])
def test_job_pages_is_refused_where_there_are_no_job_lists(client, path, body):
    response = client.post(f"/v1/scrape/{path}", json={**body, "job_pages": 2})
    assert response.status_code == 400


def test_a_person_request_stores_no_job_pages(app, app_url):
    from career_lens.api import requests as submission

    request_id, *_ = submission.submit(
        "https://www.linkedin.com/in/jane/", database=app_url)
    assert app.execute(text(
        "SELECT job_pages FROM scrape_request WHERE id = :r"),
        {"r": request_id}).scalar_one() is None


def test_an_outstanding_company_request_with_other_job_pages_conflicts(
        client, app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    url = "https://www.linkedin.com/company/acme/"
    assert client.post("/v1/scrape/linkedin-company",
                       json={"url": url, "job_pages": 2}).status_code == 202
    response = client.post("/v1/scrape/linkedin-company",
                           json={"url": url, "job_pages": 9})
    assert response.status_code == 409


def test_a_job_posting_is_submitted_by_its_url(client, app, app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    response = client.post("/v1/scrape/linkedin-job", json={
        "url": "https://in.linkedin.com/jobs/view/backend-at-acme-4012345678?trk=a"})
    assert response.status_code == 202, response.text
    row = app.execute(text(
        "SELECT kind, url_normalised, job_pages FROM scrape_request WHERE id = :r"),
        {"r": response.json()["request_id"]}).one()
    assert tuple(row) == (
        "linkedin_job", "https://www.linkedin.com/jobs/view/4012345678/", None)


def test_a_company_url_posted_to_the_job_endpoint_is_a_400(client):
    response = client.post("/v1/scrape/linkedin-job", json={
        "url": "https://www.linkedin.com/company/acme/"})
    assert response.status_code == 400


def test_a_job_search_is_submitted_by_its_terms(client, app, app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    response = client.post("/v1/scrape/linkedin-job-search", json={
        "keywords": "  backend   engineer ", "location": "Bengaluru",
        "posted_within": "week", "workplace_types": ["remote", "hybrid"],
        "job_pages": 3})
    assert response.status_code == 202, response.text
    row = app.execute(text(
        "SELECT kind, url_normalised, job_pages FROM scrape_request WHERE id = :r"),
        {"r": response.json()["request_id"]}).one()
    assert tuple(row) == (
        "linkedin_job_search",
        "https://www.linkedin.com/jobs/search/?keywords=backend+engineer"
        "&location=Bengaluru&f_TPR=r604800&f_WT=2%2C3", 3)


def test_a_job_search_reads_five_pages_by_default(client, app, app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    response = client.post("/v1/scrape/linkedin-job-search",
                           json={"keywords": "go"})
    assert response.status_code == 202, response.text
    assert app.execute(text(
        "SELECT job_pages FROM scrape_request WHERE id = :r"),
        {"r": response.json()["request_id"]}).scalar_one() == 5


@pytest.mark.parametrize("body", [
    {},
    {"keywords": "   "},
    {"keywords": 5},
    {"keywords": "go", "posted_within": "year"},
    {"keywords": "go", "workplace_types": ["moon"]},
    {"keywords": "go", "workplace_types": "remote"},
    {"keywords": "go", "url": "https://www.linkedin.com/jobs/search-results/"},
    {"keywords": "go", "job_pages": 0},
])
def test_a_bad_job_search_is_a_400(client, body):
    response = client.post("/v1/scrape/linkedin-job-search", json=body)
    assert response.status_code == 400, response.text
