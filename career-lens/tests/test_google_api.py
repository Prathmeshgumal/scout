from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from career_lens.api.main import create_app
from auth_support import customer_client
from career_lens.google.store import store_extraction
from career_lens.store.blobs import sha256
from career_lens.store.ids import CAPTURE, REQUEST, new_id


NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "fixtures" / "google" / "success.json"


class FakeStore:
    def __init__(self):
        self.objects = {}
        self.urls = []

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def get(self, key):
        return self.objects[key]

    def url(self, key, expires=3600):
        self.urls.append((key, expires))
        return f"https://minio.test/{key}?signed=1"


@pytest.fixture
def client(app_url):
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
    return customer_client(application, app_url)


def _request(app, *, kind="google_search", status="queued"):
    request_id = new_id(REQUEST)
    if kind == "google_search":
        app.execute(text("""
            INSERT INTO scrape_request
                (id, query_requested, query_normalised, google_top_n,
                 kind, status, requested_at)
            VALUES (:request, 'Enterprise AI', 'enterprise ai', 1,
                    'google_search', :status, :now)
        """), {"request": request_id, "status": status, "now": NOW})
    else:
        app.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status, requested_at)
            VALUES (:request, 'https://example.com', 'https://example.com',
                    'generic', :status, :now)
        """), {"request": request_id, "status": status, "now": NOW})
    return request_id


def test_running_google_request_returns_stable_202(client, app):
    request_id = _request(app, status="running")

    response = client.get(f"/v1/google-search/{request_id}")

    assert response.status_code == 202
    assert response.headers["retry-after"] == "2"
    assert response.json() == {
        "schema_version": "1.0",
        "request_id": request_id,
        "status": "running",
        "search": {
            "query": "Enterprise AI",
            "normalised_query": "enterprise ai",
            "requested_top_n": 1,
        },
        "result": None,
    }


def test_unknown_and_non_google_requests_are_404(client, app):
    other = _request(app, kind="generic")
    assert client.get("/v1/google-search/rq-missing").status_code == 404
    assert client.get(f"/v1/google-search/{other}").status_code == 404


@pytest.fixture
def stored_google(app, app_url, monkeypatch):
    from career_lens.api import google as google_api

    request_id = _request(app, status="succeeded")
    attempt_id = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at)
        VALUES (:request, 1, :now, :now) RETURNING id
    """), {"request": request_id, "now": NOW}).scalar_one()
    document = json.loads(GOLDEN.read_text())
    document["request"].update({
        "request_id": request_id,
        "requested_at": NOW.isoformat(),
        "started_at": NOW.isoformat(),
        "finished_at": NOW.isoformat(),
    })
    captures = []
    for section, url, host in (
        ("serp", document["serp"]["url_final"], "www.google.co.in"),
        ("result-001", "https://example.com/article", "example.com"),
    ):
        capture_id = new_id(CAPTURE)
        app.execute(text("""
            INSERT INTO capture
                (id, request_id, attempt_id, section, url_requested, url_final,
                 host, captured_at)
            VALUES (:capture, :request, :attempt, :section, :url, :url,
                    :host, :now)
        """), {
            "capture": capture_id, "request": request_id,
            "attempt": attempt_id, "section": section, "url": url,
            "host": host, "now": NOW,
        })
        image = f"image-{section}".encode()
        key = f"captures/{capture_id}/screen-00.png"
        app.execute(text("""
            INSERT INTO artifact
                (capture_id, kind, ordinal, blob_key, content_type,
                 byte_size, sha256)
            VALUES (:capture, 'screenshot', 0, :key, 'image/png',
                    :size, :digest)
        """), {
            "capture": capture_id, "key": key,
            "size": len(image), "digest": sha256(image),
        })
        captures.append((section, capture_id))
    document["serp"]["capture_id"] = captures[0][1]
    document["results"][0]["capture"]["capture_id"] = captures[1][1]
    store = FakeStore()
    store_extraction(
        app_url, request_id=request_id, attempt_id=attempt_id,
        capture_ids=captures, document=document, store=store)
    monkeypatch.setattr(google_api, "open_store", lambda: store)
    return request_id, document, store


def test_terminal_document_is_returned_without_screenshots_by_default(
        client, stored_google):
    request_id, document, _ = stored_google
    body = client.get(f"/v1/google-search/{request_id}").json()
    expected = json.loads(json.dumps(document))
    expected["schema_version"] = "1.0"
    expected["serp"].pop("capture_id")
    for item in expected["results"]:
        if item.get("capture"):
            item["capture"].pop("capture_id", None)
    for key in ("browser_locale", "browser_timezone"):
        expected["search"].pop(key, None)
    assert body == expected
    assert "screenshots" not in body["serp"]
    assert "screenshots" not in body["results"][0]["capture"]


def test_requested_screenshots_are_presigned_metadata(client, stored_google):
    request_id, document, store = stored_google
    body = client.get(
        f"/v1/google-search/{request_id}?include_screenshots=true").json()

    assert body["serp"]["screenshots"][0]["url"].startswith("https://")
    assert body["results"][0]["capture"]["screenshots"][0]["ordinal"] == 0
    assert all(expires == 604800 for _, expires in store.urls)
    canonical = json.loads(next(
        value for key, value in store.objects.items()
        if key.endswith("/document.json")))
    assert canonical == document
    assert "screenshots" not in canonical["serp"]


def test_corrupt_terminal_object_is_503(client, stored_google):
    request_id, _, store = stored_google
    key = next(key for key in store.objects if key.endswith("/document.json"))
    store.objects[key] += b"corrupt"

    response = client.get(f"/v1/google-search/{request_id}")
    assert response.status_code == 503


def test_terminal_failure_without_document_is_synthesised(client, app):
    request_id = _request(app, status="failed")
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at, cause, cause_detail)
        VALUES (:request, 1, :now, :now, 'blocked', 'challenge')
    """), {"request": request_id, "now": NOW})

    response = client.get(f"/v1/google-search/{request_id}")
    assert response.status_code == 200
    assert response.json()["failure"] == {
        "kind": "blocked", "detail": "The scrape could not complete.",
    }
    assert response.json()["serp"] is None


def test_cancelled_google_request_without_document_reports_cancellation(
        client, app):
    request_id = _request(app, status="cancelled")

    response = client.get(f"/v1/google-search/{request_id}")

    assert response.status_code == 200
    assert response.json()["request"]["status"] == "cancelled"
    assert response.json()["outcome"] == "cancelled"
    assert response.json()["failure"] is None
    assert response.json()["serp"] is None


def test_missing_terminal_success_object_is_503(client, app, monkeypatch):
    from career_lens.api import google as google_api

    request_id = _request(app, status="succeeded")
    monkeypatch.setattr(google_api, "open_store", FakeStore)
    response = client.get(f"/v1/google-search/{request_id}")
    assert response.status_code == 503


def _integration_fleet(connection):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-integration', 'Integration', 'active', 'instance-i',
                'host-i', 'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at, capabilities)
        VALUES ('worker-integration', 'worker-integration', 'shared-profile',
                'enabled', 'healthy', :now, '["google_search"]'::jsonb)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker_google_policy (worker_id)
        VALUES ('worker-integration')
    """))
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-integration', 'Shared', 'shared@example.com',
                'https://www.linkedin.com/in/shared/', 'enabled', 20, 60,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-integration', 'la-integration', 'worker-integration',
                'ready', :now, :now, 'www.linkedin.com')
    """), {"now": NOW})


def _integration_recipe(page, *, query, top_n, before_search, persist,
                        **options):
    from career_lens.capture.writer import Document
    from career_lens.google.browser import BrowserEnvironment, SearchResult
    from career_lens.google.recipe import (
        CapturedPage,
        ObservedResult,
        RecipeResult,
    )

    before_search("www.google.com")
    serp_url = "https://www.google.com/search?q=Enterprise+AI"
    serp_document = Document(
        section="serp", url_requested=serp_url, url_final=serp_url,
        host="www.google.com", http_status=200, captured_at=NOW,
        title="Enterprise AI - Google Search", html=b"<html>serp</html>",
        text="FULL SERP RENDERED TEXT", scroll_stopped="not_scrolled",
        screenshots=[b"serp-image"],
    )
    serp_id = persist(serp_document)
    serp = CapturedPage(
        capture_id=serp_id, mode="generic", status="succeeded",
        url_requested=serp_url, url_final=serp_url,
        host="www.google.com", http_status=200,
        title=serp_document.title, captured_at=NOW.isoformat(),
        text=serp_document.text, text_characters=len(serp_document.text),
        completeness={
            "scroll_stopped": "not_scrolled", "scroll_growing": False,
        },
    )
    target = "https://example.com/article"
    result = SearchResult(
        1, target, target, "example.com", "Article", "Complete snippet")
    destination_document = Document(
        section="result-001", url_requested=target, url_final=target,
        host="example.com", http_status=200, captured_at=NOW,
        title="Article", html=b"<html>destination</html>",
        text="FULL DESTINATION RENDERED TEXT", scroll_stopped="bottom",
        screenshots=[b"destination-image"],
    )
    destination_id = persist(destination_document)
    destination = CapturedPage(
        capture_id=destination_id, mode="generic", status="succeeded",
        url_requested=target, url_final=target, host="example.com",
        http_status=200, title="Article", captured_at=NOW.isoformat(),
        text=destination_document.text,
        text_characters=len(destination_document.text),
        completeness={"scroll_stopped": "bottom", "scroll_growing": False},
    )
    return RecipeResult(
        serp=serp,
        results=(ObservedResult(result, True, "top_n", destination),),
        google_host="www.google.com", serp_url=serp_url,
        browser_environment=BrowserEnvironment("en-IN", "Asia/Kolkata"),
        searched_at=NOW.isoformat(),
    )


@contextmanager
def _integration_session(*, profile, **options):
    assert profile == "shared-profile"
    yield object()


def test_public_google_flow_reaches_verified_json_and_optional_screenshots(
        client, app, app_url, monkeypatch):
    from career_lens.api import google as google_api
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    with transaction(app_url) as connection:
        _integration_fleet(connection)

    submitted = client.post("/v1/scrape/google-search", json={
        "query": "Enterprise AI", "top_n": 1,
    })
    assert submitted.status_code == 202
    request_id = submitted.json()["request_id"]
    assert client.get(f"/v1/google-search/{request_id}").status_code == 202

    with transaction(app_url) as connection:
        attempt_id = dispatch_one(
            connection, now=NOW,
            rng=type("First", (), {
                "uniform": lambda self, start, end: start,
            })())
    store = FakeStore()
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(google_api, "open_store", lambda: store)
    monkeypatch.setattr(recipe, "run", _integration_recipe)

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=_integration_session)

    terminal = client.get(f"/v1/google-search/{request_id}")
    assert terminal.status_code == 200
    body = terminal.json()
    assert body["schema_version"] == "1.0"
    assert body["search"]["results_examined"] == len(body["results"])
    assert body["serp"]["text"] == "FULL SERP RENDERED TEXT"
    assert body["results"][0]["capture"]["text"] == (
        "FULL DESTINATION RENDERED TEXT")
    assert "markdown" not in json.dumps(body).lower()
    assert "screenshots" not in body["serp"]

    enriched = client.get(
        f"/v1/google-search/{request_id}?include_screenshots=true")
    assert enriched.status_code == 200
    assert enriched.json()["serp"]["screenshots"][0]["url"].startswith(
        "https://minio.test/")
    canonical = json.loads(next(
        value for key, value in store.objects.items()
        if key.endswith("/document.json")))
    assert "screenshots" not in canonical["serp"]
