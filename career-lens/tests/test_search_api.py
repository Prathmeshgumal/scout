from __future__ import annotations

import pytest

from career_lens.api.main import create_app
from auth_support import customer_client
from career_lens.api.requests import Submission
from career_lens.google.read import (
    GoogleDocumentCorrupt,
    GoogleDocumentUnavailable,
    GoogleRequestMissing,
    SearchResult,
)
from career_lens.observe.freshness import InvalidFreshness


QUERY = '"BuenTrip Ventures" site:linkedin.com/company'
REQUEST_ID = "rq-search"
ACTIVE = {
    "request_id": REQUEST_ID,
    "status": "running",
    "search": {"query": QUERY},
}
TERMINAL = {
    "request": {"request_id": REQUEST_ID, "status": "succeeded"},
    "search": {"query": QUERY, "searched_at": "2026-08-27T10:00:00Z"},
    "outcome": "ok",
    "results": [
        {
            "rank": 1,
            "title": "BuenTrip Ventures | LinkedIn",
            "url": "https://www.linkedin.com/company/buentrip-ventures/",
            "snippet": "An early-stage venture capital firm.",
            "selection": {"selected": False, "reason": "beyond_top_n"},
            "capture": None,
        },
        {
            "rank": 2,
            "title": "BuenTrip Ventures",
            "url": "https://www.buentrip.com/",
            "snippet": "Official website.",
            "selection": {"selected": False, "reason": "beyond_top_n"},
            "capture": None,
        },
    ],
}
FAILED = {
    "request": {"request_id": REQUEST_ID, "status": "failed"},
    "search": {"query": QUERY, "searched_at": None},
    "outcome": "failed",
    "failure": {"kind": "browser_error", "detail": "browser process exited"},
    "results": [],
}


@pytest.fixture
def client(app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    return customer_client(create_app(), app_url)


def _submission(monkeypatch, search_api):
    calls = []

    def submit(query, **options):
        calls.append((query, options))
        return Submission(REQUEST_ID, True), QUERY, 0

    monkeypatch.setattr(search_api.submission, "submit_google", submit)
    return calls


def test_post_submits_exact_query_as_serp_only_and_returns_compact_200(
    client, monkeypatch
):
    from career_lens.api import search as search_api

    calls = _submission(monkeypatch, search_api)
    monkeypatch.setattr(
        search_api,
        "_read_or_http_error",
        lambda request_id: SearchResult(TERMINAL, pending=False),
    )

    response = client.post(
        "/v1/search",
        json={"query": QUERY, "priority": "high", "freshness": "24h"},
    )

    assert response.status_code == 200
    [(query, options)] = calls
    # The client's key, so that it can cancel the search later.
    assert options.pop("api_key_id").startswith("ak-")
    assert (query, options) == (
        QUERY, {"top_n": 0, "priority": "high", "freshness_window": "24h"},
    )
    assert response.json() == {
        "schema_version": "1.0",
        "request_id": REQUEST_ID,
        "status": "succeeded",
        "query": QUERY,
        "observed_at": "2026-08-27T10:00:00Z",
        "outcome": "ok",
        "failure": None,
        "results": [
            {key: result[key] for key in ("rank", "title", "url", "snippet")}
            for result in TERMINAL["results"]
        ],
    }
    assert "selection" not in response.text
    assert "capture" not in response.text


def test_post_returns_pollable_202_when_zero_wait_expires(client, monkeypatch):
    from career_lens.api import search as search_api

    _submission(monkeypatch, search_api)
    monkeypatch.setattr(
        search_api,
        "_read_or_http_error",
        lambda request_id: SearchResult(ACTIVE, pending=True),
    )

    response = client.post("/v1/search", json={"query": QUERY, "wait_seconds": 0})

    assert response.status_code == 202
    assert response.headers["location"] == f"/v1/search/{REQUEST_ID}"
    assert response.headers["retry-after"] == "2"
    assert response.json() == {
        "schema_version": "1.0",
        "request_id": REQUEST_ID,
        "status": "running",
        "query": QUERY,
        "result_url": f"/v1/search/{REQUEST_ID}",
        "retry_after": 2,
        "results": None,
    }


def test_get_transitions_from_pending_to_empty_terminal_failure(client, monkeypatch):
    from career_lens.api import search as search_api

    answers = iter(
        [SearchResult(ACTIVE, pending=True), SearchResult(FAILED, pending=False)]
    )
    monkeypatch.setattr(
        search_api, "_read_or_http_error", lambda request_id: next(answers)
    )

    assert client.get(f"/v1/search/{REQUEST_ID}").status_code == 202
    terminal = client.get(f"/v1/search/{REQUEST_ID}")
    assert terminal.status_code == 200
    assert terminal.json() == {
        "schema_version": "1.0",
        "request_id": REQUEST_ID,
        "status": "failed",
        "query": QUERY,
        "observed_at": None,
        "outcome": "failed",
        "failure": {
            "kind": "browser_error",
            "detail": "The scrape could not complete.",
        },
        "results": [],
    }


@pytest.mark.parametrize(
    "body",
    [
        {"query": QUERY, "unexpected": True},
        {"query": QUERY, "wait_seconds": -1},
        {"query": QUERY, "wait_seconds": 31},
        {"query": QUERY, "wait_seconds": True},
    ],
)
def test_post_rejects_unknown_fields_and_invalid_waits(client, body):
    assert client.post("/v1/search", json=body).status_code == 422


def test_post_reuses_existing_query_validation(client):
    response = client.post("/v1/search", json={"query": "   "})
    assert response.status_code == 400
    assert response.json()["detail"] == "query must contain 1 through 500 codepoints"


def test_post_returns_invalid_freshness_as_a_caller_error(client, monkeypatch):
    from career_lens.api import search as search_api

    monkeypatch.setattr(
        search_api.submission,
        "submit_google",
        lambda *args, **kwargs: (_ for _ in ()).throw(InvalidFreshness("bad window")),
    )

    response = client.post("/v1/search", json={"query": QUERY, "freshness": "soon"})

    assert response.status_code == 400
    assert response.json()["detail"] == "bad window"


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (GoogleRequestMissing("missing"), 404, "missing"),
        (GoogleDocumentUnavailable("unavailable"), 503, "unavailable"),
        (
            GoogleDocumentCorrupt("corrupt"),
            503,
            "Google search document failed integrity verification",
        ),
    ],
)
def test_get_preserves_canonical_reader_errors(
    client, monkeypatch, error, status, detail
):
    from career_lens.api import search as search_api

    def fail(request_id):
        raise error

    monkeypatch.setattr(search_api, "_read", fail)
    response = client.get(f"/v1/search/{REQUEST_ID}")
    assert response.status_code == status
    assert response.json() == {"schema_version": "1.0", "detail": detail}
