"""The Claude login in the web app. No test contacts Claude: a small script takes the place of `claude`."""

import json
import sys
from dataclasses import replace

import httpx
import pytest
from dotenv import dotenv_values

from job_finder.config import Settings, claude_env
from job_finder.login import SetupTokenFlow, save_credential
from job_finder.login_status import LoginStatus, read_metadata
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions

from .fakes import Streams, make_services, replies

KEY = "sk-ant-api03-web-key"
TOKEN = "sk-ant-oat01-web-token"
LOCAL = {"Host": "127.0.0.1:8000", "Origin": "http://127.0.0.1:8000"}
URL = "https://claude.ai/oauth/authorize?code=true&state=abc"

FAKE_CLAUDE = f"""#!{sys.executable}
import sys
print("\\x1b[2JOpen\\x1b[7Gthis\\x1b[12Gpage:\\x1b[18G\\x1b]8;;{URL}\\x07{URL}\\x1b]8;;\\x07")
print("\\x1b[2GPaste\\x1b[8Gcode\\x1b[13Ghere\\x1b[18Gif\\x1b[21Gprompted\\x1b[30G>", flush=True)
code = sys.stdin.readline().strip()
if code == "good-code":
    print("\\x1b[32mYour token:\\x1b[0m {TOKEN}", flush=True)
else:
    print("Invalid code", flush=True)
"""


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


@pytest.fixture
async def sessions(services):
    manager = GoalSessions(services, idle_s=600)
    yield manager
    await manager.close_all()


@pytest.fixture
def streams():
    return Streams(replies("ok"))


@pytest.fixture
def env_path(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    monkeypatch.setenv("JOB_FINDER_ENV_FILE", str(path))
    return path


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")


@pytest.fixture
async def client(app_settings, services, sessions, env_path):
    app = create_app(app_settings, services, sessions, model_loader=lambda: [])
    app.state.restarts = []
    app.state.restart = lambda: app.state.restarts.append(1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        c.app = app
        yield c
    app.state.setup_token.close()


async def test_an_api_key_is_saved_and_the_app_restarts(client, env_path):
    response = await client.post("/api/login", json={"credential": f"  {KEY}\n", "account_label": "Work"}, headers=LOCAL)
    assert response.status_code == 200
    assert response.json() == {"mode": "api_key", "restarting": True}
    assert dotenv_values(env_path)["ANTHROPIC_API_KEY"] == KEY
    assert KEY not in response.text


async def test_the_app_says_when_it_cannot_restart_itself(client):
    client.app.state.restart = None
    response = await client.post("/api/login", json={"credential": KEY}, headers=LOCAL)
    assert response.json()["restarting"] is False


async def test_a_wrong_value_is_refused_and_not_shown_again(client, env_path):
    response = await client.post("/api/login", json={"credential": "my-secret-value"}, headers=LOCAL)
    assert response.status_code == 400
    assert "my-secret-value" not in response.text
    assert not env_path.exists()
    assert client.app.state.restarts == []


@pytest.mark.parametrize("value", ["sk-ant-a\nCAREER_LENS_API_KEY=x", "sk-ant-a b", "sk-ant-a#b", "sk-ant-", "sk-ant-oat"])
def test_a_value_cannot_add_lines_to_the_env_file(tmp_path, value):
    with pytest.raises(ValueError):
        save_credential(tmp_path / ".env", value)
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("headers", [
    {"Host": "127.0.0.1:8000", "Origin": "https://evil.test"},
    {"Host": "evil.test", "Origin": "http://evil.test"},
    {"Host": "192.168.1.9:8000"},
])
async def test_only_this_computer_can_change_the_login(client, env_path, headers):
    for call in (client.post("/api/login", json={"credential": KEY}, headers=headers),
                 client.post("/api/login/subscription/start", headers=headers),
                 client.post("/api/login/subscription/code", json={"code": "x"}, headers=headers),
                 client.post("/api/login/use", json={"mode": "api_key"}, headers=headers)):
        assert (await call).status_code == 403
    assert not env_path.exists()


async def test_status_says_what_is_saved_and_never_shows_a_credential(client):
    await client.post("/api/login", json={"credential": KEY, "account_label": "Work"}, headers=LOCAL)
    response = await client.get("/api/status")
    assert response.json()["claude_account"]["saved"] == {"api_key": True, "oauth_token": False}
    assert KEY not in response.text


async def test_the_user_can_choose_a_saved_login(client, env_path):
    save_credential(env_path, KEY)
    save_credential(env_path, TOKEN)
    response = await client.post("/api/login/use", json={"mode": "oauth_token"}, headers=LOCAL)
    assert response.status_code == 200 and response.json() == {"mode": "oauth_token", "restarting": True}
    values = dotenv_values(env_path)
    assert values["JOB_FINDER_CLAUDE_LOGIN"] == "oauth_token"
    assert values["ANTHROPIC_API_KEY"] == KEY and values["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert client.app.state.restarts == []  # the restart comes after the reply


async def test_a_login_that_is_not_saved_cannot_be_chosen(client, env_path):
    save_credential(env_path, KEY)
    assert (await client.post("/api/login/use", json={"mode": "oauth_token"}, headers=LOCAL)).status_code == 409
    assert (await client.post("/api/login/use", json={"mode": "nothing"}, headers=LOCAL)).status_code == 404
    assert "JOB_FINDER_CLAUDE_LOGIN" not in dotenv_values(env_path)


async def test_a_new_login_from_the_web_app_is_the_one_in_use(client, env_path):
    save_credential(env_path, KEY)
    await client.post("/api/login", json={"credential": TOKEN}, headers=LOCAL)
    assert dotenv_values(env_path)["JOB_FINDER_CLAUDE_LOGIN"] == "oauth_token"
    await client.post("/api/login", json={"credential": KEY}, headers=LOCAL)
    assert dotenv_values(env_path)["JOB_FINDER_CLAUDE_LOGIN"] == "api_key"


async def test_the_sign_in_makes_the_subscription_the_login_in_use(client, env_path, fake_claude):
    save_credential(env_path, KEY)
    await client.post("/api/login/subscription/start", headers=LOCAL)
    await client.post("/api/login/subscription/code", json={"code": "good-code"}, headers=LOCAL)
    assert dotenv_values(env_path)["JOB_FINDER_CLAUDE_LOGIN"] == "oauth_token"


def test_the_choice_decides_the_login_of_the_agents(app_settings):
    both = replace(app_settings, anthropic_api_key=KEY, claude_oauth_token=TOKEN)
    assert both.auth_mode == "api_key"  # with no choice, the API key wins
    chosen = replace(both, claude_login="oauth_token")
    assert chosen.auth_mode == "oauth_token"
    assert claude_env(chosen) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert replace(chosen, claude_oauth_token="").auth_mode == "api_key"  # a choice with no credential has no effect


def test_the_choice_comes_from_the_environment():
    assert Settings.from_env({"JOB_FINDER_CLAUDE_LOGIN": "oauth_token"}).claude_login == "oauth_token"


def test_the_terminal_login_clears_the_choice(tmp_path):
    path = tmp_path / ".env"
    save_credential(path, TOKEN, use=True)
    save_credential(path, KEY)
    assert "JOB_FINDER_CLAUDE_LOGIN" not in dotenv_values(path)


def test_the_status_compares_the_login_that_was_chosen(app_settings, tmp_path):
    path = tmp_path / ".env"
    save_credential(path, KEY)
    save_credential(path, TOKEN, use=True)
    settings = replace(app_settings, anthropic_api_key=KEY, claude_oauth_token=TOKEN, claude_login="oauth_token")
    assert LoginStatus(settings, path).snapshot()["restart_required"] is False
    other = replace(app_settings, anthropic_api_key=KEY, claude_oauth_token=TOKEN, claude_login="api_key")
    assert LoginStatus(other, path).snapshot()["restart_required"] is True


async def test_the_subscription_sign_in_gives_the_url_then_saves_the_token(client, env_path, fake_claude):
    start = await client.post("/api/login/subscription/start", headers=LOCAL)
    assert start.status_code == 200 and start.json() == {"url": URL}
    done = await client.post("/api/login/subscription/code", json={"code": "good-code"}, headers=LOCAL)
    assert done.status_code == 200 and done.json() == {"mode": "oauth_token", "restarting": True}
    assert dotenv_values(env_path)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in done.text


async def test_the_sign_in_keeps_the_label_of_the_new_account(client, env_path, fake_claude):
    await client.post("/api/login/subscription/start", headers=LOCAL)
    await client.post("/api/login/subscription/code", json={"code": "good-code", "account_label": "Work"}, headers=LOCAL)
    assert read_metadata(env_path)["oauth_token"]["account_label"] == "Work"


async def test_a_new_sign_in_replaces_the_token_of_the_old_account(client, env_path, fake_claude):
    save_credential(env_path, "sk-ant-oat01-old-account", account_label="Old")
    await client.post("/api/login/subscription/start", headers=LOCAL)
    await client.post("/api/login/subscription/code", json={"code": "good-code"}, headers=LOCAL)
    assert dotenv_values(env_path)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert read_metadata(env_path)["oauth_token"]["account_label"] is None


async def test_a_wrong_code_saves_nothing_and_says_to_start_again(client, env_path, fake_claude):
    await client.post("/api/login/subscription/start", headers=LOCAL)
    done = await client.post("/api/login/subscription/code", json={"code": "bad-code"}, headers=LOCAL)
    assert done.status_code == 400 and "Sign in again" in done.json()["detail"]
    assert not env_path.exists()
    assert client.app.state.restarts == []


async def test_a_code_with_no_sign_in_is_refused(client):
    done = await client.post("/api/login/subscription/code", json={"code": "x"}, headers=LOCAL)
    assert done.status_code == 409


async def test_the_sign_in_explains_when_claude_is_missing(client, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    start = await client.post("/api/login/subscription/start", headers=LOCAL)
    assert start.status_code == 409 and "paste" in start.json()["detail"].lower()


def test_the_flow_hides_the_login_of_the_app_from_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"""#!{sys.executable}
import json, os
print("{URL}")
print(json.dumps([n for n in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN") if n in os.environ]))
print("Paste code here if prompted >", flush=True)
input()
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    flow = SetupTokenFlow()
    assert flow.start(timeout=10) == URL
    assert json.dumps([]) in flow.screen()
    flow.close()
