from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from auth_support import operator_client
from career_lens.api.main import create_app
from career_lens.store.blobs import sha256
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id


NOW = datetime(2026, 8, 20, 5, 27, 24, tzinfo=UTC)
URL = "https://example.com/article"
PAGE_TEXT = "Article heading\nA complete rendered article."


class FakeStore:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = objects or {}

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    def head(self, key: str) -> int | None:
        return len(self.objects[key]) if key in self.objects else None

    def url(self, key: str, expires: int) -> str:
        return f"https://blobs.example/{key}?expires={expires}"


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
    return operator_client(application, app_url)


def _request(app, *, kind: str = "generic", status: str = "queued") -> str:
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:request, :url, :url, :kind, :status, :now)
    """), {
        "request": request_id, "url": URL, "kind": kind,
        "status": status, "now": NOW,
    })
    return request_id


def test_running_generic_request_returns_stable_202(client, app):
    request_id = _request(app, status="running")

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 202
    assert response.headers["retry-after"] == "2"
    assert response.json() == {
        "schema_version": "1.0",
        "request_id": request_id,
        "status": "running",
        "url": URL,
        "result": None,
    }


def _stored_reading(app, *, at, url=URL, text_body=PAGE_TEXT, markdown=None):
    """One generic request whose document is stored and projected."""
    from career_lens.capture.writer import Document
    from career_lens.generic.contract import build
    from career_lens.generic.project import project

    request_id = _request(app, status="succeeded")
    capture_id, extraction_id = new_id(CAPTURE), new_id(EXTRACTION)
    app.execute(text("""
        UPDATE scrape_request SET url_requested=:u, url_normalised=:u
        WHERE id=:request
    """), {"request": request_id, "u": url})
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped, scroll_growing,
             markdown)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Example article', 'bottom', false, :markdown)
    """), {"capture": capture_id, "request": request_id, "url": url,
           "now": at, "markdown": markdown})
    document = build(Document(
        section="page", url_requested=url, url_final=url, host="example.com",
        http_status=200, captured_at=at, title="Example article", html=b"",
        text=text_body, scroll_stopped="bottom", scroll_growing=False,
    ), capture_id=capture_id, normalised_url=url)
    payload = json.dumps(document).encode()
    key = f"extractions/{extraction_id}/generic.json"
    app.execute(text("""
        INSERT INTO extraction
            (id, request_id, extractor, extractor_version, extracted_at,
             outcome, document_key, document_sha256, document_bytes)
        VALUES (:id, :request, 'generic_page_json', '1.0', :now, 'ok',
                :key, :digest, :size)
    """), {"id": extraction_id, "request": request_id, "now": at,
           "key": key, "digest": sha256(payload), "size": len(payload)})
    app.execute(text("""
        INSERT INTO extraction_input (extraction_id, capture_id, section)
        VALUES (:extraction, :capture, 'page')
    """), {"extraction": extraction_id, "capture": capture_id})
    project(app, extraction_id=extraction_id, request_id=request_id,
            document=document)
    return request_id, {key: payload}


def test_a_url_reads_back_without_knowing_which_run_read_it(
        client, app, monkeypatch):
    """The question a caller actually has. Before the projection existed the
    only way in was the id of the request that happened to make the reading."""
    from career_lens.store import blobs

    request_id, objects = _stored_reading(app, at=NOW)
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore(objects))

    body = client.get("/v1/url", params={"url": URL}).json()

    assert body["request_id"] == request_id
    assert body["normalised_url"] == URL
    assert body["observed_at"] == NOW.isoformat()
    assert body["result"]["capture"]["text"] == PAGE_TEXT


def test_a_url_reads_back_as_it_stood_at_an_instant(client, app, monkeypatch):
    from career_lens.store import blobs

    _, march = _stored_reading(
        app, at=datetime(2026, 3, 1, tzinfo=UTC), text_body="March text")
    _, may = _stored_reading(
        app, at=datetime(2026, 5, 1, tzinfo=UTC), text_body="May text")
    monkeypatch.setattr(
        blobs, "open_store", lambda: FakeStore({**march, **may}))

    body = client.get("/v1/url", params={
        "url": URL, "at": "2026-04-01T00:00:00Z"}).json()

    assert body["result"]["capture"]["text"] == "March text"


def test_a_url_nobody_has_read_is_404(client, app):
    response = client.get("/v1/url", params={"url": "https://example.com/none"})

    assert response.status_code == 404


def test_a_url_that_is_not_one_is_refused(client, app):
    assert client.get("/v1/url", params={"url": "not a url"}).status_code == 400


def test_unknown_and_non_generic_requests_are_404(client, app):
    linkedin = _request(app, kind="linkedin_person")

    assert client.get("/v1/url/rq-missing").status_code == 404
    assert client.get(f"/v1/url/{linkedin}").status_code == 404


def test_completed_generic_request_returns_google_destination_shape(
        client, app, monkeypatch):
    from career_lens.store import blobs

    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped, scroll_growing)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Example article', 'bottom', false)
    """), {
        "capture": capture_id, "request": request_id,
        "url": URL, "now": NOW,
    })
    payload = PAGE_TEXT.encode()
    key = f"captures/{capture_id}/page.txt"
    app.execute(text("""
        INSERT INTO artifact
            (capture_id, kind, ordinal, blob_key, content_type,
             byte_size, sha256)
        VALUES (:capture, 'text', 0, :key, 'text/plain; charset=utf-8',
                :size, :digest)
    """), {
        "capture": capture_id, "key": key, "size": len(payload),
        "digest": sha256(payload),
    })
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore({key: payload}))

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "1.0",
        "rank": 1,
        "url": URL,
        "normalised_url": URL,
        "host": "example.com",
        "title": "Example article",
        "snippet": "",
        "selection": {"selected": True, "reason": "direct"},
        "capture": {
            "mode": "generic",
            "status": "succeeded",
            "url_requested": URL,
            "url_final": URL,
            "host": "example.com",
            "http_status": 200,
            "title": "Example article",
            "captured_at": NOW.isoformat(),
            "text": PAGE_TEXT,
            "text_characters": len(PAGE_TEXT),
            "completeness": {
                "scroll_stopped": "bottom",
                "scroll_growing": False,
            },
            "error": None,
        },
    }


def test_completed_generic_request_reads_the_stored_canonical_document(
        client, app, app_url, monkeypatch):
    from career_lens.generic.store import store_extraction
    from career_lens.store import blobs

    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Stored article')
    """), {
        "capture": capture_id, "request": request_id,
        "url": URL, "now": NOW,
    })
    canonical = {
        "rank": 1, "url": URL, "normalised_url": URL,
        "host": "example.com", "title": "Stored article", "snippet": "",
        "selection": {"selected": True, "reason": "direct"},
        "capture": {
            "capture_id": capture_id, "mode": "generic",
            "status": "succeeded", "url_requested": URL,
            "url_final": URL, "host": "example.com", "http_status": 200,
            "title": "Stored article", "captured_at": NOW.isoformat(),
            "text": "Text from canonical JSON", "text_characters": 24,
            "completeness": {
                "scroll_stopped": "bottom", "scroll_growing": False,
            },
            "error": None,
        },
    }
    store = FakeStore()
    store_extraction(
        app_url, request_id=request_id, attempt_id=None,
        capture_id=capture_id, document=canonical, store=store)
    monkeypatch.setattr(blobs, "open_store", lambda: store)

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 200
    expected = {**canonical, "schema_version": "1.0", "capture": dict(canonical["capture"])}
    del expected["capture"]["capture_id"]
    assert response.json() == expected


def test_failed_generic_request_without_a_capture_returns_failed_result(
        client, app):
    request_id = _request(app, status="failed")
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at, cause, cause_detail)
        VALUES (:request, 1, :now, :now, 'timeout', 'page did not settle')
    """), {"request": request_id, "now": NOW})

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["selection"] == {"selected": True, "reason": "direct"}
    assert body["capture"] == {
        "mode": "generic",
        "status": "failed",
        "url_requested": URL,
        "url_final": None,
        "host": "example.com",
        "http_status": None,
        "title": None,
        "captured_at": None,
        "text": None,
        "text_characters": 0,
        "completeness": {
            "scroll_stopped": "not_started",
            "scroll_growing": False,
        },
        "error": {"kind": "timeout", "detail": "The scrape could not complete."},
    }
    # Public content is useful without exposing its internal evidence ID.
    assert "capture_id" not in body["capture"]


def test_screenshot_urls_are_attached_only_when_asked_for(
        client, app, monkeypatch):
    """Presigned URLs expire, so they are attached on the way out rather than
    stored -- and what comes out still has to satisfy the contract."""
    from career_lens.store import blobs

    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped, scroll_growing)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Example article', 'bottom', false)
    """), {
        "capture": capture_id, "request": request_id, "url": URL, "now": NOW,
    })
    shot = b"not really a png"
    key = f"captures/{capture_id}/screen-00.png"
    app.execute(text("""
        INSERT INTO artifact
            (capture_id, kind, ordinal, blob_key, content_type,
             byte_size, sha256)
        VALUES (:capture, 'screenshot', 0, :key, 'image/png', :size, :digest)
    """), {
        "capture": capture_id, "key": key, "size": len(shot),
        "digest": sha256(shot),
    })
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore({key: shot}))

    plain = client.get(f"/v1/url/{request_id}").json()
    assert "screenshots" not in plain["capture"]

    enriched = client.get(
        f"/v1/url/{request_id}?include_screenshots=true").json()
    assert [shot["ordinal"] for shot in enriched["capture"]["screenshots"]] == [0]
    assert enriched["capture"]["screenshots"][0]["content_type"] == "image/png"
    assert "capture_id" not in enriched["capture"]


MARKDOWN = "# Example article\n\nRead about [Acme](https://acme.com/)."


def test_markdown_with_links_is_attached_only_when_asked_for(
        client, app, monkeypatch):
    """`text` has no links. A caller that needs them asks for the Markdown."""
    from career_lens.store import blobs

    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped, scroll_growing,
             markdown)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Example article', 'bottom', false, :markdown)
    """), {
        "capture": capture_id, "request": request_id, "url": URL, "now": NOW,
        "markdown": MARKDOWN,
    })
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore())

    plain = client.get(f"/v1/url/{request_id}").json()
    assert "markdown" not in plain["capture"]

    enriched = client.get(
        f"/v1/url/{request_id}", params={"include_markdown": "true"}).json()
    assert enriched["capture"]["markdown"] == MARKDOWN
    assert "capture_id" not in enriched["capture"]


def test_markdown_is_attached_to_a_stored_reading_by_url(
        client, app, monkeypatch):
    from career_lens.store import blobs

    request_id, objects = _stored_reading(app, at=NOW, markdown=MARKDOWN)
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore(objects))

    body = client.get(
        "/v1/url", params={"url": URL, "include_markdown": "true"}).json()

    assert body["result"]["capture"]["markdown"] == MARKDOWN
    assert body["result"]["capture"]["text"] == PAGE_TEXT


def test_markdown_is_null_when_there_is_no_capture(client, app):
    request_id = _request(app, status="failed")
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at, cause, cause_detail)
        VALUES (:request, 1, :now, :now, 'timeout', 'page did not settle')
    """), {"request": request_id, "now": NOW})

    body = client.get(
        f"/v1/url/{request_id}", params={"include_markdown": "true"}).json()

    assert body["capture"]["status"] == "failed"
    assert body["capture"]["markdown"] is None


def _satisfied(app, *, source_request: str | None) -> str:
    """A request that freshness answered from an earlier request's page."""
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at,
             freshness_seconds, satisfied_at)
        VALUES (:request, :url, :url, 'generic', 'satisfied', :now, 604800,
                :now)
    """), {"request": request_id, "url": URL, "now": NOW})
    if source_request is not None:
        capture_id = app.execute(text(
            "SELECT id FROM capture WHERE request_id=:r AND section='page'"),
            {"r": source_request}).scalar_one()
        app.execute(text("""
            INSERT INTO request_satisfaction
                (request_id, section, capture_id, observed_at)
            VALUES (:r, 'page', :c, :t)
        """), {"r": request_id, "c": capture_id, "t": NOW})
    return request_id


def test_a_satisfied_request_returns_the_page_that_satisfied_it(
        client, app, monkeypatch):
    """Its own `data_url` points here. It has no capture of its own, and
    reporting the page as a failed capture sent callers away from data we
    hold."""
    from career_lens.store import blobs

    source, objects = _stored_reading(app, at=NOW, markdown=MARKDOWN)
    request_id = _satisfied(app, source_request=source)
    monkeypatch.setattr(blobs, "open_store", lambda: FakeStore(objects))

    response = client.get(
        f"/v1/url/{request_id}", params={"include_markdown": "true"})

    assert response.status_code == 200
    capture = response.json()["capture"]
    assert capture["status"] == "succeeded"
    assert capture["text"] == PAGE_TEXT
    assert capture["markdown"] == MARKDOWN
    assert response.json() == client.get(
        f"/v1/url/{source}", params={"include_markdown": "true"}).json()


def test_a_satisfied_request_without_a_source_is_unavailable(client, app):
    request_id = _satisfied(app, source_request=None)

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 503
    assert "no source capture" in response.json()["detail"]


def test_succeeded_generic_request_without_a_capture_is_unavailable(client, app):
    request_id = _request(app, status="succeeded")

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 503
    assert response.json() == {
        "schema_version": "1.0",
        "detail": "generic URL result has no page capture",
    }


def test_cancelled_generic_request_without_an_attempt_reports_cancellation(
        client, app):
    request_id = _request(app, status="queued")
    app.execute(text("""
        UPDATE scrape_request
        SET status='cancelled', cancel_requested_at=:now
        WHERE id=:request
    """), {"request": request_id, "now": NOW})

    capture = client.get(f"/v1/url/{request_id}").json()["capture"]

    assert capture["error"] == {
        "kind": "cancelled",
        "detail": "The scrape could not complete.",
    }


def test_a_retry_failure_does_not_relabel_an_earlier_attempts_capture(
        client, app):
    request_id = _request(app, status="failed")
    first_attempt = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at)
        VALUES (:request, 1, :now, :now) RETURNING id
    """), {"request": request_id, "now": NOW}).scalar_one()
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, attempt_id, section, url_requested, url_final,
             host, http_status, captured_at, title)
        VALUES (:capture, :request, :attempt, 'page', :url, :url,
                'example.com', 200, :now, 'Old capture')
    """), {
        "capture": capture_id, "request": request_id,
        "attempt": first_attempt, "url": URL, "now": NOW,
    })
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at, cause, cause_detail)
        VALUES (:request, 2, :now, :now, 'timeout', 'retry did not settle')
    """), {"request": request_id, "now": NOW})

    result = client.get(f"/v1/url/{request_id}").json()

    assert result["title"] is None
    assert "capture_id" not in result["capture"]
    assert result["capture"]["error"] == {
        "kind": "timeout",
        "detail": "The scrape could not complete.",
    }


def test_screenshot_failure_keeps_the_structured_page_as_partial(
        client, app):
    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at, title, scroll_stopped,
             scroll_growing, screenshot_error)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now, 'Example article', 'stalled', true,
                'image exceeded the screenshot limit')
    """), {
        "capture": capture_id, "request": request_id,
        "url": URL, "now": NOW,
    })

    capture = client.get(f"/v1/url/{request_id}").json()["capture"]

    assert capture["status"] == "partial"
    assert capture["text"] == ""
    assert capture["completeness"] == {
        "scroll_stopped": "stalled",
        "scroll_growing": True,
    }
    assert capture["error"] == {
        "kind": "capture_error",
        "detail": "The scrape could not complete.",
    }


def test_corrupt_rendered_text_is_not_returned_as_structured_json(
        client, app, monkeypatch):
    from career_lens.store import blobs

    request_id = _request(app, status="succeeded")
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host,
             http_status, captured_at)
        VALUES (:capture, :request, 'page', :url, :url, 'example.com',
                200, :now)
    """), {
        "capture": capture_id, "request": request_id,
        "url": URL, "now": NOW,
    })
    expected = b"expected page text"
    key = f"captures/{capture_id}/page.txt"
    app.execute(text("""
        INSERT INTO artifact
            (capture_id, kind, ordinal, blob_key, content_type,
             byte_size, sha256)
        VALUES (:capture, 'text', 0, :key, 'text/plain; charset=utf-8',
                :size, :digest)
    """), {
        "capture": capture_id, "key": key, "size": len(expected),
        "digest": sha256(expected),
    })
    monkeypatch.setattr(
        blobs, "open_store", lambda: FakeStore({key: b"tampered page text"}))

    response = client.get(f"/v1/url/{request_id}")

    assert response.status_code == 503
    assert response.json() == {
        "schema_version": "1.0",
        "detail": "generic capture text failed integrity verification",
    }
