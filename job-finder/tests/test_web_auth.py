"""The password login, the bearer token, and the check that a request comes from this computer."""

import httpx
import pytest
from fastapi.routing import APIRoute

from job_finder.web import auth
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions
from tests.conftest import PASSWORD

from .fakes import Streams, make_services, replies


@pytest.fixture
def services(app_settings, store, hub, recorder):
    return make_services(app_settings, store, hub, recorder, Streams(replies("ok")))


@pytest.fixture
async def sessions(services):
    manager = GoalSessions(services, idle_s=600)
    yield manager
    await manager.close_all()


@pytest.fixture
def app(app_settings, services, sessions):
    return create_app(app_settings, services, sessions, model_loader=lambda: [])


@pytest.fixture
async def anon(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        yield client


async def sign_in(anon, password=PASSWORD):
    return await anon.post("/api/auth/login", json={"password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


async def test_the_password_gives_a_bearer_token_that_opens_the_api(anon):
    assert (await anon.get("/api/goals")).status_code == 401
    body = (await sign_in(anon)).json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == auth.SESSION_SECONDS
    assert (await anon.get("/api/goals", headers=bearer(body["access_token"]))).status_code == 200


async def test_a_refused_request_names_the_bearer_scheme(anon):
    response = await anon.get("/api/goals")
    assert response.headers["www-authenticate"] == "Bearer"


async def test_every_api_route_needs_the_token(app, anon):
    """A route that a later change adds is in this list too."""
    routes = [route for route in app.routes if isinstance(route, APIRoute) and route.path.startswith("/api/")]
    assert len(routes) > 30
    for route in routes:
        if route.path in auth.OPEN_PATHS:
            continue
        path = route.path.replace("{", "").replace("}", "")
        for method in route.methods:
            assert (await anon.request(method, path)).status_code == 401, (method, route.path)


async def test_only_login_and_health_are_open(anon):
    assert auth.OPEN_PATHS == {"/api/auth/login", "/api/health"}
    response = await anon.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


async def test_a_wrong_password_gets_no_token(anon):
    response = await sign_in(anon, "wrong")
    assert response.status_code == 401
    assert "access_token" not in response.json()


async def test_five_wrong_passwords_stop_all_logins_for_a_time(anon, monkeypatch):
    for _ in range(auth.MAX_FAILURES):
        assert (await sign_in(anon, "wrong")).status_code == 401
    assert (await sign_in(anon)).status_code == 429
    later = auth._now() + auth.LOCK_SECONDS + 1
    monkeypatch.setattr(auth, "_now", lambda: later)
    assert (await sign_in(anon)).status_code == 200


async def test_no_password_set_refuses_login_and_every_token(anon, monkeypatch):
    token = auth.new_token(PASSWORD)
    monkeypatch.setenv(auth.PASSWORD_ENV, "")
    assert (await sign_in(anon, "")).status_code == 503
    assert (await anon.get("/api/goals", headers=bearer(token))).status_code == 401
    assert (await anon.get("/api/goals", headers=bearer(auth.new_token("")))).status_code == 401


async def test_a_new_password_ends_all_tokens(anon, monkeypatch):
    token = (await sign_in(anon)).json()["access_token"]
    monkeypatch.setenv(auth.PASSWORD_ENV, "another-made-up-password")
    assert (await anon.get("/api/goals", headers=bearer(token))).status_code == 401


async def test_an_old_token_is_refused(anon, monkeypatch):
    token = (await sign_in(anon)).json()["access_token"]
    later = auth._now() + auth.SESSION_SECONDS + 1
    monkeypatch.setattr(auth, "_now", lambda: later)
    assert (await anon.get("/api/goals", headers=bearer(token))).status_code == 401


@pytest.mark.parametrize("change", [
    lambda t: t[:-1] + ("0" if t[-1] != "0" else "1"),
    lambda t: "9999999999" + t[t.index("."):],
    lambda t: "",
    lambda t: "...",
    lambda t: "not-a-token",
    lambda t: "é",
])
async def test_a_changed_token_is_refused(anon, change):
    token = (await sign_in(anon)).json()["access_token"]
    headers = {"Authorization": f"Bearer {change(token)}".encode()}
    assert (await anon.get("/api/goals", headers=headers)).status_code == 401


@pytest.mark.parametrize("scheme", ["Basic", "Token", ""])
async def test_only_the_bearer_scheme_is_read(anon, scheme):
    token = (await sign_in(anon)).json()["access_token"]
    assert (await anon.get("/api/goals", headers={"Authorization": f"{scheme} {token}".strip()})).status_code == 401


async def test_a_cookie_or_query_token_is_not_read(anon):
    token = (await sign_in(anon)).json()["access_token"]
    assert (await anon.get("/api/goals", headers={"Cookie": f"token={token}"})).status_code == 401
    assert (await anon.get(f"/api/goals?token={token}&access_token={token}")).status_code == 401


@pytest.mark.parametrize("headers", [
    {"Host": "evil.test"},
    {"Host": "evil.test:8000"},
    {"Host": "192.168.1.9:8000"},
    {"Host": "127.0.0.1.evil.test"},
    {"Host": "localhost@evil.test"},
    {"Host": "[::1"},
    {"Host": ""},
    {"Host": "127.0.0.1:8000", "Origin": "https://evil.test"},
    {"Host": "127.0.0.1:8000", "Origin": "null"},
    {"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:9999"},
])
async def test_a_request_from_elsewhere_is_refused_even_with_a_token(anon, headers):
    token = (await sign_in(anon)).json()["access_token"]
    for path in ("/api/goals", "/api/health", "/"):
        assert (await anon.get(path, headers={**headers, **bearer(token)})).status_code == 403, path
    assert (await anon.post("/api/auth/login", json={"password": PASSWORD}, headers=headers)).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:5173", "[::1]", "[::1]:8000"])
async def test_this_computer_may_use_each_loopback_name(anon, host):
    token = (await sign_in(anon)).json()["access_token"]
    headers = {"Host": host, "Origin": f"http://{host}", **bearer(token)}
    assert (await anon.get("/api/goals", headers=headers)).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_the_api_has_no_public_docs(anon, path):
    assert (await anon.get(path)).status_code == 404
