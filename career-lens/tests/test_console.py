"""The console page and the account screens behind it."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from career_lens.api import db as api_db, operator_auth
from career_lens.api.main import create_app
from career_lens.fleet.accounts import password_for_login
from career_lens.store import db

ACCOUNT = {
    "label": "Spare", "username": "spare@example.com",
    "max_attempts_24h": 20, "min_interval_seconds": 60,
}


def _week(**overrides) -> dict:
    working = {
        "working": True, "start_local": "09:00", "end_local": "18:00",
        "lunch_minutes": 30, **overrides,
    }
    days = [{"weekday": day, **working} for day in range(1, 6)]
    days += [{"weekday": day, "working": False} for day in (6, 7)]
    return {"enabled": True, "timezone": "Asia/Kolkata", "days": days}


@pytest.fixture
def application(app_url, monkeypatch):
    key = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", json.dumps({"1": key}))
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", "1")

    def override():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    instance = create_app()
    instance.dependency_overrides[api_db.database] = override
    return instance


@pytest.fixture
def client(application):
    return TestClient(application)


@pytest.fixture
def authorised_client(application, monkeypatch):
    monkeypatch.setenv(operator_auth.PASSWORD_ENV, "made-up-operator-password")
    operator_auth.guard.reset()
    client = TestClient(application)
    assert client.post("/operator/api/login", json={"password": "made-up-operator-password"}).status_code == 204
    return client


def _worker(app) -> str:
    app.execute(text("""
        INSERT INTO supervisor (id, label, desired_state, instance_id, hostname,
                                software_version, started_at, last_heartbeat_at)
        VALUES ('sv', 'Laptop', 'active', 'boot', 'host', '1', now(), now())
    """))
    app.execute(text("""
        INSERT INTO worker (id, supervisor_id, local_name, desired_state, health,
                            last_heartbeat_at)
        VALUES ('wk', 'sv', 'one', 'enabled', 'healthy', now())
    """))
    app.execute(text("INSERT INTO worker_google_policy (worker_id) VALUES ('wk')"))
    return "wk"


def test_console_api_needs_a_key(client):
    assert client.get("/operator/api/overview").status_code == 401


def test_add_account_never_returns_the_password(authorised_client):
    response = authorised_client.post(
        "/operator/api/accounts", json=ACCOUNT | {"password": "pw-secret"})
    assert response.status_code == 201, response.text
    assert "pw-secret" not in response.text
    overview = authorised_client.get("/operator/api/overview")
    assert overview.status_code == 200
    assert "pw-secret" not in overview.text
    assert [a["label"] for a in overview.json()["accounts"]] == ["Spare"]


def test_a_refused_account_does_not_echo_the_password(authorised_client):
    response = authorised_client.post(
        "/operator/api/accounts",
        json={"label": "", "username": "x", "password": "pw-secret",
              "max_attempts_24h": 0, "min_interval_seconds": 60})
    assert response.status_code == 422
    assert "pw-secret" not in response.text


def _account(client) -> str:
    return client.post("/operator/api/accounts",
                       json=ACCOUNT | {"password": "pw"}).json()["id"]


def test_a_good_shift_is_saved(authorised_client):
    account = _account(authorised_client)
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/shift", json=_week())
    assert response.status_code == 200, response.text
    overview = authorised_client.get("/operator/api/overview").json()
    assert overview["accounts"][0]["schedule"]["timezone"] == "Asia/Kolkata"


def test_short_shift_is_refused(authorised_client):
    account = _account(authorised_client)
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/shift",
        json=_week(start_local="09:00", end_local="09:40", lunch_minutes=30))
    assert response.status_code == 422
    assert "jitter" in response.text


def test_bad_timezone_is_refused(authorised_client):
    account = _account(authorised_client)
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/shift",
        json=_week() | {"timezone": "Mars/Olympus"})
    assert response.status_code == 422


def test_shift_for_an_unknown_account_is_404(authorised_client):
    response = authorised_client.put(
        "/operator/api/accounts/la-missing/shift", json=_week())
    assert response.status_code == 404


def test_assign_shows_in_overview(authorised_client, app):
    worker = _worker(app)
    account = _account(authorised_client)
    response = authorised_client.post(
        f"/operator/api/workers/{worker}/assignments", json={"account_id": account})
    assert response.status_code == 202, response.text
    overview = authorised_client.get("/operator/api/overview").json()
    assert overview["workers"][0]["assignment"]["account_id"] == account
    assert overview["accounts"][0]["assignment"]["state"] == "login_pending"


def test_a_command_on_an_unknown_assignment_is_404(authorised_client):
    response = authorised_client.post("/operator/api/assignments/as-missing/verify")
    assert response.status_code == 404


def test_an_unknown_command_is_404(authorised_client):
    response = authorised_client.post("/operator/api/assignments/as-x/explode")
    assert response.status_code == 404


def test_google_policy_can_be_switched_on(authorised_client, app):
    worker = _worker(app)
    response = authorised_client.patch(
        f"/operator/api/workers/{worker}/google-policy",
        json={"enabled": True, "max_searches_hour": 20, "max_searches_24h": 100})
    assert response.status_code == 200, response.text
    assert response.json()["google"]["policy"]["enabled"] is True
    overview = authorised_client.get("/operator/api/overview").json()
    assert overview["workers"][0]["google"]["policy"]["enabled"] is True


def test_overview_survives_a_worker_with_no_google_policy(authorised_client, app):
    _worker(app)
    app.execute(text("DELETE FROM worker_google_policy"))
    response = authorised_client.get("/operator/api/overview")
    assert response.status_code == 200
    assert response.json()["workers"][0]["google"] is None


def test_overview_lists_recent_requests(authorised_client, app):
    app.execute(text("""
        INSERT INTO scrape_request (id, url_requested, url_normalised, kind, status,
                                    requested_at, dispatch_wait_reason)
        VALUES ('rq-1', 'https://example.org', 'https://example.org', 'generic',
                'queued', now(), 'no_assignments')
    """))
    requests = authorised_client.get("/operator/api/overview").json()["requests"]
    assert [r["id"] for r in requests] == ["rq-1"]
    assert requests[0]["wait_reason"] == "no_assignments"


def test_a_retired_account_is_not_listed(authorised_client, app):
    account = _account(authorised_client)
    app.execute(text("UPDATE linkedin_account SET state='retired' WHERE id=:id"),
                {"id": account})
    assert authorised_client.get("/operator/api/overview").json()["accounts"] == []


def test_overview_survives_a_worker_with_no_heartbeat_yet(authorised_client, app):
    _worker(app)
    app.execute(text("UPDATE worker SET last_heartbeat_at = NULL WHERE id = 'wk'"))
    app.commit()
    response = authorised_client.get("/operator/api/overview")
    assert response.status_code == 200, response.text
    worker = response.json()["workers"][0]
    assert worker["live"] is False
    assert worker["last_heartbeat_at"] is None


def test_a_mistyped_password_can_be_replaced(authorised_client, app_url):
    account = _account(authorised_client)
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/password", json={"password": "pw-new-secret"})
    assert response.status_code == 200, response.text
    assert "pw-new-secret" not in response.text
    with db.transaction(app_url) as connection:
        assert password_for_login(connection, account) == "pw-new-secret"


def test_password_of_a_missing_account_is_not_found(authorised_client):
    response = authorised_client.put(
        "/operator/api/accounts/nope/password", json={"password": "pw-new-secret"})
    assert response.status_code == 404
    assert "pw-new-secret" not in response.text


def test_password_of_a_retired_account_is_refused(authorised_client, app):
    account = _account(authorised_client)
    app.execute(text("UPDATE linkedin_account SET state = 'retired' WHERE id = :id"),
                {"id": account})
    app.commit()
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/password", json={"password": "pw-new-secret"})
    assert response.status_code == 409


def test_a_blank_password_is_refused(authorised_client):
    account = _account(authorised_client)
    response = authorised_client.put(
        f"/operator/api/accounts/{account}/password", json={"password": ""})
    assert response.status_code == 422


def _assigned(client, app) -> str:
    worker = _worker(app)
    account = _account(client)
    assert client.post(f"/operator/api/workers/{worker}/assignments",
                       json={"account_id": account}).status_code == 202
    return account


def test_a_login_that_needs_the_operator_says_so(authorised_client, app):
    _assigned(authorised_client, app)
    app.execute(text("""
        UPDATE account_worker_assignment
        SET state = 'attention_required',
            operator_attention_code = 'linkedin_signed_out',
            state_detail = 'signed out'
        WHERE ended_at IS NULL
    """))
    app.commit()
    overview = authorised_client.get("/operator/api/overview").json()
    assignment = overview["accounts"][0]["assignment"]
    assert assignment["waiting_for_you"] is True
    assert "sign in" in assignment["action"]
    assert "Verify" in assignment["action"]


def test_a_login_in_progress_is_not_waiting_for_the_operator(
        authorised_client, app):
    _assigned(authorised_client, app)
    assignment = authorised_client.get(
        "/operator/api/overview").json()["accounts"][0]["assignment"]
    assert assignment["waiting_for_you"] is False
    assert assignment["action"] is None


def test_the_limits_of_an_account_can_be_changed(authorised_client):
    account = _account(authorised_client)
    response = authorised_client.put(f"/operator/api/accounts/{account}/limits",
                                     json={"max_attempts_24h": 35, "min_interval_seconds": 45})
    assert response.status_code == 200
    assert response.json() == {"account_id": account, "max_attempts_24h": 35, "min_interval_seconds": 45}
    shown = authorised_client.get("/operator/api/overview").json()["accounts"][0]
    assert (shown["max_attempts_24h"], shown["min_interval_seconds"]) == (35, 45)


@pytest.mark.parametrize("body", [
    {"max_attempts_24h": 0, "min_interval_seconds": 3},
    {"max_attempts_24h": 10_001, "min_interval_seconds": 3},
    {"max_attempts_24h": 20, "min_interval_seconds": -1},
    {"max_attempts_24h": 20, "min_interval_seconds": 86_401},
    {"max_attempts_24h": "20", "min_interval_seconds": 3},
    {"max_attempts_24h": 20},
])
def test_bad_limits_are_refused(authorised_client, body):
    account = _account(authorised_client)
    assert authorised_client.put(f"/operator/api/accounts/{account}/limits", json=body).status_code == 422


def test_limits_of_a_missing_or_retired_account_are_refused(authorised_client, app):
    body = {"max_attempts_24h": 20, "min_interval_seconds": 3}
    assert authorised_client.put("/operator/api/accounts/la-none/limits", json=body).status_code == 404
    account = _account(authorised_client)
    app.execute(text("UPDATE linkedin_account SET state = 'retired' WHERE id = :id"), {"id": account})
    assert authorised_client.put(f"/operator/api/accounts/{account}/limits", json=body).status_code == 409


def test_limits_need_a_session(client):
    body = {"max_attempts_24h": 20, "min_interval_seconds": 3}
    assert client.put("/operator/api/accounts/la-any/limits", json=body).status_code == 401


def test_the_console_makes_a_key_that_works_until_it_is_revoked(authorised_client):
    created = authorised_client.post("/operator/api/api-keys", json={"name": "  Sales tool  "})
    assert created.status_code == 201
    assert created.headers["cache-control"] == "no-store"
    key = created.json()
    assert key["name"] == "Sales tool"
    assert key["revoked_at"] is None
    bearer = {"Authorization": f"Bearer {key['token']}"}
    assert authorised_client.get("/v1/requests/rq-none", headers=bearer).status_code == 404

    listed = authorised_client.get("/operator/api/api-keys")
    assert [row["id"] for row in listed.json()] == [key["id"]]
    assert set(listed.json()[0]) == {"id", "name", "prefix", "created_at", "revoked_at"}

    revoked = authorised_client.post(f"/operator/api/api-keys/{key['id']}/revoke")
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None
    assert "token" not in revoked.json()
    assert authorised_client.get("/v1/requests/rq-none", headers=bearer).status_code == 401


def test_revoking_a_key_twice_keeps_the_first_time(authorised_client):
    key = authorised_client.post("/operator/api/api-keys", json={"name": "Once"}).json()
    first = authorised_client.post(f"/operator/api/api-keys/{key['id']}/revoke").json()
    second = authorised_client.post(f"/operator/api/api-keys/{key['id']}/revoke").json()
    assert second["revoked_at"] == first["revoked_at"]


@pytest.mark.parametrize("body", [{"name": ""}, {"name": "   "}, {"name": "x" * 121}, {}])
def test_a_bad_key_name_is_refused(authorised_client, body):
    assert authorised_client.post("/operator/api/api-keys", json=body).status_code == 422
    assert authorised_client.get("/operator/api/api-keys").json() == []


def test_revoking_a_missing_key_is_refused(authorised_client):
    assert authorised_client.post("/operator/api/api-keys/ak-none/revoke").status_code == 404


def test_api_keys_need_a_session(client):
    assert client.get("/operator/api/api-keys").status_code == 401
    assert client.post("/operator/api/api-keys", json={"name": "No"}).status_code == 401
    assert client.post("/operator/api/api-keys/ak-any/revoke").status_code == 401


def test_a_customer_key_cannot_make_keys(authorised_client, application):
    token = authorised_client.post("/operator/api/api-keys", json={"name": "Customer"}).json()["token"]
    customer = TestClient(application)
    response = customer.post("/operator/api/api-keys", json={"name": "Stolen"},
                             headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
