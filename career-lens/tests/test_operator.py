"""The operator surface: a password and a session cookie, and never a customer key."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from auth_support import customer_client

from career_lens.api import console, operator_auth
from career_lens.api.main import create_app
from career_lens.api.requests import Submission
from career_lens.store import blobs
from career_lens.store.ids import EXTRACTION, REQUEST, new_id

PASSWORD = "made-up-operator-password"
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


@pytest.fixture(autouse=True)
def fresh_guard(monkeypatch):
    monkeypatch.setenv(operator_auth.PASSWORD_ENV, PASSWORD)
    operator_auth.guard.reset()


@pytest.fixture
def application(app_url):
    return _application(app_url)


@pytest.fixture
def anonymous(application):
    return TestClient(application)


@pytest.fixture
def operator(application):
    client = TestClient(application)
    assert client.post("/operator/api/login", json={"password": PASSWORD}).status_code == 204
    return client


def test_no_cookie_gets_401(anonymous):
    assert anonymous.get("/operator/api/overview").status_code == 401


def test_a_customer_key_never_reaches_an_operator_route(application, app_url):
    customer = customer_client(application, app_url)
    assert customer.get("/operator/api/overview").status_code == 401
    assert customer.post("/operator/api/accounts", json={}).status_code == 401


def test_a_customer_key_with_a_valid_session_is_refused(operator, application, app_url):
    key = customer_client(application, app_url).headers["Authorization"]
    assert operator.get("/operator/api/overview", headers={"Authorization": key}).status_code == 401


def test_the_old_console_api_is_gone(application, app_url):
    assert customer_client(application, app_url).get("/console/api/overview").status_code == 404


def test_the_right_password_opens_a_session(operator):
    assert operator.get("/operator/api/overview").status_code == 200
    assert operator.get("/operator/api/session").json() == {"signed_in": True}


def test_the_cookie_cannot_be_read_by_scripts_or_sent_from_other_sites(anonymous):
    response = anonymous.post("/operator/api/login", json={"password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/operator" in cookie


def test_the_cookie_is_secure_over_https(application):
    client = TestClient(application, base_url="https://testserver")
    response = client.post("/operator/api/login", json={"password": PASSWORD})
    assert "secure" in response.headers["set-cookie"].lower()


def test_a_wrong_password_gets_401_and_no_cookie(anonymous):
    response = anonymous.post("/operator/api/login", json={"password": "wrong"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert PASSWORD not in response.text


def test_many_wrong_passwords_stop_all_logins_for_a_time(anonymous):
    for _ in range(operator_auth.MAX_FAILURES):
        anonymous.post("/operator/api/login", json={"password": "wrong"})
    assert anonymous.post("/operator/api/login", json={"password": PASSWORD}).status_code == 429


def test_no_password_set_closes_the_surface(anonymous, monkeypatch):
    monkeypatch.delenv(operator_auth.PASSWORD_ENV)
    assert anonymous.post("/operator/api/login", json={"password": ""}).status_code == 503
    assert anonymous.get("/operator/api/overview").status_code == 401


def test_sign_out_clears_the_cookie(operator):
    assert operator.post("/operator/api/logout").status_code == 204
    assert operator.get("/operator/api/overview").status_code == 401


def test_a_new_password_ends_old_sessions(operator, monkeypatch):
    monkeypatch.setenv(operator_auth.PASSWORD_ENV, "another-made-up-password")
    assert operator.get("/operator/api/overview").status_code == 401


def test_an_old_session_ends(operator, monkeypatch):
    monkeypatch.setattr(operator_auth, "_now", lambda: operator_auth.time.time() + operator_auth.SESSION_SECONDS + 1)
    assert operator.get("/operator/api/overview").status_code == 401


def test_a_changed_cookie_is_refused(anonymous):
    for value in ("9999999999.abc.def", "no-dots", "9999999999.abc.caf%C3%A9"):
        anonymous.cookies.set(operator_auth.COOKIE, value, path="/operator")
        assert anonymous.get("/operator/api/overview").status_code == 401


# The requests


def _request(app, *, kind="linkedin_company", status="succeeded", url="https://www.linkedin.com/company/acme/",
             minutes=0, reason=None, query=None):
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request (id, url_requested, url_normalised, query_requested, query_normalised,
                                    google_top_n, kind, status, requested_at, dispatch_wait_reason)
        VALUES (:r, :u, :u, :q, :q, :n, :k, :s, :t, :w)
    """), {"r": rid, "u": url, "q": query, "n": 5 if query else None, "k": kind, "s": status,
           "t": NOW - timedelta(minutes=minutes), "w": reason})
    return rid


def _ids(response) -> list[str]:
    return [row["id"] for row in response.json()["items"]]


def test_the_list_gives_the_newest_request_first_with_counts(operator, app):
    old = _request(app, minutes=5, status="failed", url="https://example.com/a")
    new = _request(app, status="queued", url="https://example.com/b", reason="accounts_off_shift")
    body = operator.get("/operator/api/requests").json()
    assert [row["id"] for row in body["items"]] == [new, old]
    assert body["items"][0]["wait_reason"] == "accounts_off_shift"
    assert body["counts"] == {"failed": 1, "queued": 1}


def test_the_list_filters_by_status_kind_and_text(operator, app):
    failed = _request(app, status="failed", url="https://example.com/a")
    job = _request(app, kind="linkedin_job", url="https://www.linkedin.com/jobs/view/1/")
    search = _request(app, kind="google_search", url=None, query="go engineer pune")
    assert _ids(operator.get("/operator/api/requests", params={"status": "failed"})) == [failed]
    assert _ids(operator.get("/operator/api/requests", params={"kind": "linkedin_job"})) == [job]
    assert _ids(operator.get("/operator/api/requests", params={"q": "ENGINEER"})) == [search]


def test_the_list_text_filter_takes_percent_as_text(operator, app):
    _request(app, url="https://example.com/a")
    assert _ids(operator.get("/operator/api/requests", params={"q": "%"})) == []


def test_the_list_pages_with_a_cursor(operator, app):
    rows = [_request(app, minutes=n, url=f"https://example.com/{n}") for n in range(3)]
    first = operator.get("/operator/api/requests", params={"limit": 2}).json()
    assert [row["id"] for row in first["items"]] == rows[:2]
    second = operator.get("/operator/api/requests", params={"limit": 2, "before": first["next"]}).json()
    assert [row["id"] for row in second["items"]] == rows[2:]
    assert second["next"] is None


def test_a_bad_cursor_or_filter_gets_422(operator):
    assert operator.get("/operator/api/requests", params={"before": "nope"}).status_code == 422
    assert operator.get("/operator/api/requests", params={"status": "nope"}).status_code == 422


def test_the_detail_gives_the_run_log_and_the_screenshots(operator, app):
    rid = _request(app, status="failed")
    attempt = app.execute(text("""
        INSERT INTO scrape_attempt (request_id, attempt_no, worker, started_at, ended_at, cause, cause_detail)
        VALUES (:r, 1, 'w1', :t, :t, 'blocked', 'page said no') RETURNING id
    """), {"r": rid, "t": NOW}).scalar_one()
    app.execute(text("""
        INSERT INTO attempt_event (attempt_id, at, elapsed_ms, kind, level, phase, detail, failed)
        VALUES (:a, :t, 12, 'work', 'info', 'open page', 'detail', false)
    """), {"a": attempt, "t": NOW})
    body = operator.get(f"/operator/api/requests/{rid}").json()
    assert body["failure"] == {"kind": "blocked", "detail": "page said no"}
    assert [e["phase"] for e in body["events"]] == ["open page"]
    assert body["events"][0]["attempt"] == 1
    assert body["screenshots"] == []


def test_an_unknown_request_gets_404(operator):
    assert operator.get("/operator/api/requests/rq-none").status_code == 404
    assert operator.get("/operator/api/requests/rq-none/result").status_code == 404
    assert operator.get("/operator/api/artifacts/none").status_code == 404


def test_a_customer_key_cannot_read_requests(application, app_url):
    customer = customer_client(application, app_url)
    assert customer.get("/operator/api/requests").status_code == 401


class FakeBlobs:
    def __init__(self, saved):
        self.saved = saved

    def get(self, key):
        return self.saved[key]


def test_the_result_is_the_stored_document(operator, app, monkeypatch):
    rid = _request(app)
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version, extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'x', '1', :t, 'partial', 'doc/1', 'abc', 2)
    """), {"e": new_id(EXTRACTION), "r": rid, "t": NOW})
    monkeypatch.setattr(blobs, "open_store", lambda: FakeBlobs({"doc/1": b'{"name": "Acme"}'}))
    assert operator.get(f"/operator/api/requests/{rid}/result").json() == {"name": "Acme"}


def test_a_request_with_no_result_gets_404(operator, app):
    assert operator.get(f"/operator/api/requests/{_request(app)}/result").status_code == 404


# Try it


def test_try_it_submits_with_no_customer_key(operator, monkeypatch):
    from career_lens.api import requests as submission

    seen = {}

    def submit(url, **kwargs):
        seen.update(url=url, key=kwargs.get("api_key_id"))
        return Submission("rq-try", True)

    monkeypatch.setattr(submission, "submit", submit)
    response = operator.post("/operator/api/try/url", json={"url": "https://example.com/"})
    assert response.status_code == 202
    assert response.json()["request_id"] == "rq-try"
    assert seen == {"url": "https://example.com/", "key": None}


def test_try_it_refuses_an_unknown_path(operator):
    assert operator.post("/operator/api/try/nope", json={}).status_code == 404


def test_try_it_needs_a_session(anonymous, application, app_url):
    assert anonymous.post("/operator/api/try/url", json={"url": "https://example.com/"}).status_code == 401
    customer = customer_client(application, app_url)
    assert customer.post("/operator/api/try/url", json={"url": "https://example.com/"}).status_code == 401


# The page


@pytest.fixture
def built(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<title>career-lens</title>")
    (tmp_path / "assets" / "app.js").write_text("run()")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    monkeypatch.setattr(console, "DIST", tmp_path)
    return tmp_path


def test_each_console_path_gives_the_app(anonymous, built):
    for path in ("/console", "/console/", "/console/requests", "/console/requests/rq-1"):
        response = anonymous.get(path)
        assert response.status_code == 200 and "career-lens" in response.text, path
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_console_gives_its_files(anonymous, built):
    assert anonymous.get("/console/assets/app.js").text == "run()"
    assert anonymous.get("/console/favicon.svg").text == "<svg/>"


def test_the_console_gives_no_file_outside_its_folder(anonymous, built):
    (built.parent / "secret.txt").write_text("secret")
    response = anonymous.get("/console/assets/%2e%2e/%2e%2e/secret.txt")
    assert "secret" not in response.text


def test_a_missing_build_says_so(anonymous, tmp_path, monkeypatch):
    monkeypatch.setattr(console, "DIST", tmp_path / "none")
    assert anonymous.get("/console").status_code == 503
