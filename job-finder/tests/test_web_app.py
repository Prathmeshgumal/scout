import asyncio
import json

import httpx
import pytest

from job_finder.db import GoalLock
from job_finder.models import from_sdk
from job_finder.web.app import create_app, sse_stream
from job_finder.web.sessions import GoalSessions

from .fakes import SDK_MODELS, Streams, agent_call, make_services, replies, result, task_started


@pytest.fixture
def streams():
    return Streams(replies("On it."))


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


@pytest.fixture
async def sessions(services):
    manager = GoalSessions(services, idle_s=600)
    yield manager
    await manager.close_all()


# The models of the SDK, and a model with fewer effort levels, as an earlier model has.
MENU = [*SDK_MODELS, {"value": "claude-sonnet-4-6", "resolvedModel": "claude-sonnet-4-6",
                      "description": "Sonnet 4.6 · Earlier Sonnet", "supportsEffort": True,
                      "supportedEffortLevels": ["low", "medium", "high", "max"]}]
HAIKU = "claude-haiku-4-5-20251001"


async def sdk_models():
    return from_sdk(MENU)


@pytest.fixture
async def client(app_settings, services, sessions):
    app = create_app(app_settings, services, sessions, model_loader=sdk_models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def idle_goal(store, title="g", agent=None):
    goal = await store.create_goal(title, agent=agent)
    await store.set_status(goal["id"], "done")
    return goal["id"]


async def test_create_goal_sends_the_first_message(client, streams, store):
    response = await client.post("/api/goals", json={"text": "  Find 5 EU fintech jobs  "})
    assert response.status_code == 201
    goal_id = response.json()["goal_id"]
    assert streams.last.queries == ["Find 5 EU fintech jobs"]
    await streams.last.flush()
    detail = (await client.get(f"/api/goals/{goal_id}")).json()
    assert detail["goal"]["title"] == "Find 5 EU fintech jobs"
    assert detail["pipeline"]["jobs"] == 0 and detail["tasks"] == []
    assert "events" not in (await client.get(f"/api/goals/{goal_id}?events=false")).json()
    types = [event["type"] for event in detail["events"]]
    assert types[0] == "user_message" and "text" in types


async def test_a_new_goal_has_no_profile_and_a_reply_never_has_the_resume(client, store):
    goal_id = (await client.post("/api/goals", json={"text": "Find jobs"})).json()["goal_id"]
    assert (await store.get_goal(goal_id))["profile"] is None
    await store.set_profile(goal_id, {"locations": {"value": ["Pune"], "basis": "stated"},
                                      "resume_text": "Private resume words"}, ready=True)
    second = (await client.post("/api/goals", json={"text": "Find more jobs"})).json()["goal_id"]
    assert (await store.get_goal(second))["profile"] is None  # no copy from the newest goal
    response = await client.get(f"/api/goals/{goal_id}?events=false")
    goal = response.json()["goal"]
    assert goal["profile"]["locations"]["value"] == ["Pune"] and goal["profile"]["has_resume"] is True
    assert "Private resume words" not in response.text


async def test_blank_goal_is_rejected(client):
    assert (await client.post("/api/goals", json={"text": "   "})).status_code == 422


async def test_no_claude_login_gives_503(app_settings, services, sessions):
    settings = type(app_settings)(**{**app_settings.__dict__, "anthropic_api_key": ""})
    app = create_app(settings, services, sessions, model_loader=sdk_models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post("/api/goals", json={"text": "x"})
        status = (await c.get("/api/status")).json()
    detail = response.json()["detail"]
    assert response.status_code == 503 and "CLAUDE_CODE_OAUTH_TOKEN" in detail and "job-finder login" in detail
    assert status["anthropic_configured"] is False and status["claude_login"] == "Claude not connected"


async def test_a_subscription_token_alone_is_a_claude_login(app_settings, services, sessions):
    settings = type(app_settings)(**{**app_settings.__dict__, "anthropic_api_key": "",
                                     "claude_oauth_token": "sk-ant-oat01-t"})
    app = create_app(settings, services, sessions, model_loader=sdk_models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await c.post("/api/goals", json={"text": "x"})
        status = (await c.get("/api/status")).json()
    assert response.status_code != 503
    assert status["anthropic_configured"] is True and status["claude_login"] == "Claude subscription token"


async def test_list_goals_has_spend(client, store):
    goal_id = await idle_goal(store, "one")
    [goal] = (await client.get("/api/goals")).json()
    assert goal["id"] == goal_id and goal["spend_usd"] == 0 and goal["max_spend_usd"] == 2.0


async def test_unknown_goal_gives_404(client):
    assert (await client.get("/api/goals/nope")).status_code == 404
    assert (await client.get("/api/goals/00000000-0000-0000-0000-000000000000")).status_code == 404


async def test_message_reopens_a_closed_goal_and_adds_the_pending_note(client, store, streams):
    goal_id = await idle_goal(store)
    await store.add_scrape(goal_id, "rq-1", "linkedin_person", "https://www.linkedin.com/in/jane/", True)
    response = await client.post(f"/api/goals/{goal_id}/messages", json={"text": "Skip Germany"})
    assert response.status_code == 202
    assert streams.last.queries[-1].startswith("Skip Germany\n\n(Note: 1 scrape for this goal is still pending")
    detail = (await client.get(f"/api/goals/{goal_id}?events=false")).json()
    assert detail["scrapes"] == {"fresh": 1, "pending": 1}


async def test_message_while_a_subagent_works_is_accepted(client, store, streams):
    goal_id = await idle_goal(store)
    await client.post(f"/api/goals/{goal_id}/messages", json={"text": "Find 5"})
    streams.last.push(agent_call("call-1", "job-gen"), task_started("a1", "call-1", "Find 5"), result(cost=0.02))
    await streams.last.flush()
    detail = (await client.get(f"/api/goals/{goal_id}")).json()
    assert [task["name"] for task in detail["tasks"]] == ["job-gen"]
    response = await client.post(f"/api/goals/{goal_id}/messages", json={"text": "Skip Germany"})
    assert response.status_code == 202 and streams.last.queries[-1] == "Skip Germany"


async def test_goal_open_in_a_terminal_gives_409(client, store, database_url):
    goal_id = await idle_goal(store)
    lock = await GoalLock.acquire(database_url, goal_id)
    try:
        response = await client.post(f"/api/goals/{goal_id}/messages", json={"text": "hi"})
        assert response.status_code == 409 and "terminal" in response.json()["detail"]
        assert (await client.get(f"/api/goals/{goal_id}")).status_code == 200  # still readable
    finally:
        await lock.release()


async def test_a_subagent_goal_is_read_only(client, store):
    goal_id = await idle_goal(store, agent="research")
    response = await client.post(f"/api/goals/{goal_id}/messages", json={"text": "hi"})
    assert response.status_code == 409


async def test_stop_and_retry(client, store, streams):
    goal_id = await idle_goal(store)
    assert (await client.post(f"/api/goals/{goal_id}/stop")).status_code == 409  # done: nothing to stop
    assert (await client.post(f"/api/goals/{goal_id}/retry")).status_code == 409  # done: nothing to retry
    await client.post(f"/api/goals/{goal_id}/messages", json={"text": "Find 5"})
    streams.last.push(agent_call("call-1", "job-gen"), task_started("a1", "call-1"), result(cost=0.02))
    await streams.last.flush()
    assert (await client.post(f"/api/goals/{goal_id}/stop")).status_code == 202
    assert streams.last.stopped == ["a1"]
    assert (await store.get_goal(goal_id))["status"] == "stopped"
    assert (await client.post(f"/api/goals/{goal_id}/retry")).status_code == 202
    assert len(streams.last.queries) == 2


async def test_status(client):
    status = (await client.get("/api/status")).json()
    account = status.pop("claude_account")
    assert account["loaded_at"]
    assert set(status.pop("version")) == {"commit", "local", "modified"}
    assert status.pop("career_lens") == {"state": "not_configured", "checked_at": None}
    assert status == {"model": "claude-sonnet-5-5", "effort": "medium", "anthropic_configured": True,
                      "claude_login": "API key", "career_lens_configured": False, "open_sessions": 0}


async def test_report_and_its_download(client, store):
    goal_id = await idle_goal(store, "Find 2 jobs")
    report = (await client.get(f"/api/goals/{goal_id}/report")).json()
    assert report["goal"]["title"] == "Find 2 jobs" and report["jobs"] == []
    assert (await client.get("/api/goals/nope/report")).status_code == 404
    md = await client.get(f"/api/goals/{goal_id}/report.md")
    assert md.headers["content-type"].startswith("text/markdown") and "report.md" in md.headers["content-disposition"]
    assert md.text.startswith("# Find 2 jobs\n")


async def test_a_goal_link_gives_the_browser_app(app_settings, services, sessions, tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<div id=root></div>")
    monkeypatch.setattr("job_finder.web.app.STATIC_DIR", tmp_path)
    app = create_app(app_settings, services, sessions, model_loader=sdk_models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        for path in ("/", "/goals/b7602321-26e1-4e7c-942e-243d23fec79b/report", "/mandates", "/jobs", "/pipeline"):
            response = await c.get(path)
            assert response.status_code == 200 and "root" in response.text
        assert (await c.get("/api/goals")).status_code == 200  # the API still answers


def _payload(chunk: str) -> dict:
    return json.loads(chunk.split("data: ", 1)[1])


async def test_sse_stream_replays_then_follows(store, hub, recorder):
    goal_id = await idle_goal(store)
    await recorder.record(goal_id, "user_message", text="one")
    stream = sse_stream(store, hub, goal_id, after=0, poll_s=0.05, keepalive_s=0.1)
    first = await anext(stream)
    assert first.startswith("id: 1\n") and _payload(first)["text"] == "one"
    assert await asyncio.wait_for(anext(stream), 1) == ": ping\n\n"
    recorder.live(goal_id, "text_delta", text="fo")
    assert _payload(await asyncio.wait_for(anext(stream), 1))["text"] == "fo"
    await stream.aclose()


async def make_mandate(store, pool):
    from datetime import UTC, datetime, time

    from job_finder.mandate_store import MandateStore
    home = await store.create_goal("home")
    return await MandateStore(pool).create(home["id"], {
        "title": "EU fintech jobs", "instruction": "fintech", "every_unit": "day", "every_count": 1, "jobs_per_run": 20,
        "window_days": [1, 2, 3, 4, 5], "window_start": time(9), "timezone": "UTC", "max_spend_per_run": 2.0,
        "max_scrapes_per_run": 15, "next_run_at": datetime(2030, 1, 1, tzinfo=UTC)})


async def test_the_mandate_api_only_lists_mandates(client, store, pool):
    # Only the agent of the mandate's chat reads and changes a mandate in detail, after the user says yes.
    m = await make_mandate(store, pool)
    [row] = (await client.get("/api/mandates")).json()
    assert row["id"] == m["id"] and row["title"] == "EU fintech jobs" and "summary" not in row
    assert (await client.get(f"/api/mandates/{m['id']}")).status_code == 404
    assert (await client.patch(f"/api/mandates/{m['id']}", json={"jobs_per_run": 5})).status_code in (404, 405)
    for action in ("pause", "resume", "end", "run-now"):
        assert (await client.post(f"/api/mandates/{m['id']}/{action}")).status_code in (404, 405)
    [row] = (await client.get("/api/mandates")).json()
    assert (row["jobs_per_run"], row["status"]) == (20, "active")


async def test_the_goal_list_marks_home_chats_and_runs(client, store, pool):
    from datetime import UTC, datetime

    from job_finder.mandate_store import MandateStore
    m = await make_mandate(store, pool)
    run = await MandateStore(pool).create_run(m, datetime(2030, 1, 1, tzinfo=UTC), 2.0, 1)
    plain = await store.create_goal("plain")
    rows = {row["id"]: row for row in (await client.get("/api/goals")).json()}
    assert rows[m["home_goal_id"]]["home_of"] == m["id"] and rows[m["home_goal_id"]]["mandate_id"] is None
    assert rows[run["id"]]["mandate_id"] == m["id"] and rows[run["id"]]["home_of"] is None
    assert rows[plain["id"]]["mandate_id"] is None and rows[plain["id"]]["home_of"] is None


async def test_delete_a_goal(client, store):
    goal_id = await idle_goal(store)
    preview = (await client.get(f"/api/goals/{goal_id}/delete-preview")).json()
    assert preview == {"goals": 1, "jobs": 0, "notes": 0, "working": False}
    assert (await client.delete(f"/api/goals/{goal_id}")).status_code == 204
    assert (await client.get(f"/api/goals/{goal_id}")).status_code == 404
    assert (await client.delete(f"/api/goals/{goal_id}")).status_code == 404


async def test_delete_a_mandate_and_not_its_chat_alone(client, store, make_run):
    mandate, run = await make_run()
    refused = await client.delete(f"/api/goals/{mandate['home_goal_id']}")
    assert refused.status_code == 409 and "Delete the mandate" in refused.json()["detail"]
    preview = (await client.get(f"/api/mandates/{mandate['id']}/delete-preview")).json()
    assert preview["goals"] == 1 and preview["status"] == "active" and preview["working"] is True
    assert (await client.delete(f"/api/mandates/{mandate['id']}")).status_code == 204
    assert (await client.get("/api/mandates")).json() == []
    assert (await client.get(f"/api/goals/{run['id']}")).status_code == 404
    assert (await client.delete(f"/api/mandates/{mandate['id']}")).status_code == 404
    assert (await client.get("/api/mandates/not-a-uuid/delete-preview")).status_code == 404


async def test_a_delete_ends_the_event_stream(store, hub, recorder):
    goal_id = await idle_goal(store)
    stream = sse_stream(store, hub, goal_id, 0, poll_s=0.05)
    reading = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.01)
    recorder.live(goal_id, "goal_deleted")
    assert '"goal_deleted"' in await reading
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_the_model_and_effort_of_a_goal(client, store, streams, app_settings):
    models = (await client.get("/api/models")).json()
    assert models["default"] == {"model": app_settings.model, "effort": app_settings.effort}
    assert [e["id"] for e in models["efforts"]] == ["low", "medium", "high", "xhigh", "max"]
    assert app_settings.model in [m["id"] for m in models["models"]]
    created = await client.post("/api/goals", json={"text": "Find jobs", "model": "claude-opus-5-5", "effort": "high"})
    goal_id = created.json()["goal_id"]
    assert streams.last.options.model == "claude-opus-5-5" and streams.last.options.effort == "high"
    await streams.last.flush()
    changed = await client.put(f"/api/goals/{goal_id}/model", json={"model": HAIKU, "effort": "low"})
    assert changed.status_code == 200
    goal = (await client.get(f"/api/goals/{goal_id}?events=false")).json()["goal"]
    assert (goal["model"], goal["effort"]) == (HAIKU, "low")
    await client.post(f"/api/goals/{goal_id}/messages", json={"text": "More"})
    assert streams.last.options.model == HAIKU  # the next message opens the session with it
    bad = await client.put(f"/api/goals/{goal_id}/model", json={"model": "gpt-9", "effort": "low"})
    assert bad.status_code == 422
    bad = await client.post("/api/goals", json={"text": "x", "effort": "turbo"})
    assert bad.status_code == 422


async def test_the_menu_has_only_the_models_and_levels_of_the_sdk(client):
    models = (await client.get("/api/models")).json()
    assert [m["id"] for m in models["models"]] == ["claude-sonnet-5-5", "claude-fable-5-1", "claude-opus-5-5", HAIKU,
                                                   "claude-sonnet-4-6"]
    menu = {m["id"]: m for m in models["models"]}
    assert menu["claude-sonnet-4-6"]["efforts"] == ["low", "medium", "high", "max"] and menu[HAIKU]["efforts"] == []
    assert [(e["id"], e["label"]) for e in models["efforts"]] == [
        ("low", "Low"), ("medium", "Medium"), ("high", "High"), ("xhigh", "Extra"), ("max", "Max")]


async def test_each_model_of_the_menu_runs_the_agents_of_its_goal(client, streams):
    """Each model of the menu goes to the main agent, with an effort that the SDK gives for it. The subagents use
    the main model and the same effort."""
    for m in (await client.get("/api/models")).json()["models"]:
        effort = m["efforts"][-1] if m["efforts"] else "medium"
        await client.post("/api/goals", json={"text": "Find jobs", "model": m["id"], "effort": effort})
        options = streams.last.options
        assert (options.model, options.effort) == (m["id"], effort)
        assert all(agent.model is None and agent.effort == effort for agent in options.agents.values())
        await streams.last.flush()


async def test_a_model_or_an_effort_that_the_sdk_does_not_give_is_refused(client):
    bad = await client.post("/api/goals", json={"text": "x", "model": "claude-sonnet-4-6", "effort": "xhigh"})
    assert bad.status_code == 422 and "low, medium, high, max" in bad.json()["detail"]
    bad = await client.post("/api/goals", json={"text": "x", "model": "claude-opus-4-8", "effort": "high"})
    assert bad.status_code == 422  # not in the SDK's list
    ok = await client.post("/api/goals", json={"text": "x", "model": HAIKU, "effort": "medium"})
    assert ok.status_code == 201  # a model without effort levels keeps the effort, and Claude Code ignores it


async def test_the_model_cannot_change_while_the_agents_work(client, streams):
    """A change applies when the session opens again, so the user stops the work first."""
    goal_id = (await client.post("/api/goals", json={"text": "Find jobs"})).json()["goal_id"]
    busy = await client.put(f"/api/goals/{goal_id}/model", json={"model": "claude-opus-5-5", "effort": "high"})
    assert busy.status_code == 409 and "Stop the work" in busy.json()["detail"]
    await streams.last.flush()  # the main agent answered: the goal is idle
    idle = await client.put(f"/api/goals/{goal_id}/model", json={"model": "claude-opus-5-5", "effort": "high"})
    assert idle.status_code == 200


async def test_the_browser_gives_the_time_zone_of_the_user(client, store):
    created = await client.post("/api/goals", json={"text": "Find jobs", "timezone": "Asia/Kolkata"})
    goal_id = created.json()["goal_id"]
    assert (await store.get_goal(goal_id))["timezone"] == "Asia/Kolkata"
    await store.set_status(goal_id, "done")
    await client.post(f"/api/goals/{goal_id}/messages", json={"text": "More", "timezone": "Europe/Berlin"})
    assert (await store.get_goal(goal_id))["timezone"] == "Europe/Berlin"  # the user moved: the newest zone
    await store.set_status(goal_id, "done")
    response = await client.post(f"/api/goals/{goal_id}/messages", json={"text": "More", "timezone": "Mars/Base"})
    assert response.status_code == 202 and (await store.get_goal(goal_id))["timezone"] == "Europe/Berlin"
    other = (await client.post("/api/goals", json={"text": "x", "timezone": "nope"})).json()["goal_id"]
    assert (await store.get_goal(other))["timezone"] is None  # an unknown zone: not kept


async def test_the_markdown_report_shows_the_times_in_the_given_zone(client, store):
    goal_id = await idle_goal(store, "Find 2 jobs")
    assert "(Asia/Kolkata), after" in (await client.get(f"/api/goals/{goal_id}/report.md?tz=Asia/Kolkata")).text
    assert "(UTC), after" in (await client.get(f"/api/goals/{goal_id}/report.md?tz=Mars/Base")).text


async def test_status_reports_cached_outage_without_failing_health(app_settings, services, sessions, monkeypatch):
    from types import SimpleNamespace

    import job_finder.web.app as web_app

    monkeypatch.setattr(web_app, "read_version", lambda root: {
        "commit": "a" * 40, "local": False, "modified": False})
    app = create_app(app_settings, services, sessions, model_loader=sdk_models)
    app.state.career_lens_status = SimpleNamespace(snapshot={"state": "unreachable", "checked_at": "2026-10-15T10:00:00Z"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/status")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["version"]["commit"] == "a" * 40
    assert response.json()["career_lens"]["state"] == "unreachable"


async def test_status_keeps_the_loaded_account_until_restart(app_settings, services, sessions, tmp_path, monkeypatch):
    from dataclasses import replace

    from job_finder.login import save_credential
    path = tmp_path / '.env'
    monkeypatch.setenv('JOB_FINDER_ENV_FILE', str(path))
    save_credential(path, 'sk-ant-oat01-first', account_label='First account')
    settings = replace(app_settings, anthropic_api_key='', claude_oauth_token='sk-ant-oat01-first')
    app = create_app(settings, services, sessions, model_loader=sdk_models)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        initial = (await client.get('/api/status')).json()['claude_account']
        assert initial['account_label'] == 'First account' and not initial['restart_required']
        save_credential(path, 'sk-ant-oat01-second', account_label='Second account')
        response = await client.get('/api/status')
        pending = response.json()['claude_account']
        assert pending['account_label'] == 'First account' and pending['restart_required']
        assert 'sk-ant-' not in response.text and 'fingerprint' not in response.text


async def test_continue_after_the_time_limit(client, store, streams, make_run, pool):
    from job_finder.mandate_store import MandateStore
    from job_finder.time_limit import TIME_REASON
    m, run = await make_run()
    async with pool.connection() as conn:
        await conn.execute("UPDATE goals SET created_at = now() - interval '151 minutes' WHERE id = %s", (run["id"],))
    await store.set_soft_limit(run["id"])
    await store.set_status(run["id"], "capped", TIME_REASON)
    detail = (await client.get(f"/api/goals/{run['id']}?events=false")).json()
    assert detail["time_limit"]["ok"] is False and "Raise the time limit" in detail["time_limit"]["reason"]
    refused = await client.post(f"/api/goals/{run['id']}/continue")
    assert refused.status_code == 409 and "Raise the time limit" in refused.json()["detail"]
    await MandateStore(pool).update(m["id"], {"time_limit_minutes": 240})
    assert (await client.get(f"/api/goals/{run['id']}?events=false")).json()["time_limit"]["ok"] is True
    assert (await client.post(f"/api/goals/{run['id']}/continue")).status_code == 202
    assert streams.last.queries[-1].startswith("The user gave this goal more time.")
    assert (await store.get_goal(run["id"]))["status"] == "running"
    other = await idle_goal(store)
    assert (await client.get(f"/api/goals/{other}?events=false")).json()["time_limit"] is None
