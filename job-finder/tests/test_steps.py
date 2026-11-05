"""The input and output of each step, kept for the Activity tab. A step that can hold resume text keeps neither."""

import json

import pytest
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from job_finder.translate import DETAIL_TOOLS, MAX_DETAIL, PRIVATE_TOOLS, Translator
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions
from tests.conftest import signed_in_client

from .fakes import Streams, make_services, replies


def use(tool, tool_input, *, call="t1", parent=None):
    return AssistantMessage(model="m", parent_tool_use_id=parent,
                            content=[ToolUseBlock(id=call, name=f"mcp__x__{tool}", input=tool_input)])


def done(text, *, call="t1", error=False):
    return UserMessage(content=[ToolResultBlock(tool_use_id=call, content=[{"type": "text", "text": text}],
                                                is_error=error)])


def test_a_web_step_keeps_its_input_and_its_output():
    t = Translator()
    [started] = t.translate(use("search_linkedin_jobs", {"keywords": "go engineer", "location": "Pune"}))
    assert started.detail == {"input": {"keywords": "go engineer", "location": "Pune"}}
    [finished] = t.translate(done("18 roles\n1. Go engineer at Acme"))
    assert finished.detail == {"output": "18 roles\n1. Go engineer at Acme"}
    assert "output" not in finished.data  # stored only, never pushed live


def test_a_failed_step_keeps_why_it_failed():
    t = Translator()
    t.translate(use("read_page", {"url": "https://acme.example/careers"}))
    [finished] = t.translate(done("career-lens error (HTTP 503)", error=True))
    assert finished.detail == {"output": "career-lens error (HTTP 503)"}


@pytest.mark.parametrize("tool", sorted(PRIVATE_TOOLS))
def test_a_step_that_can_hold_resume_text_keeps_nothing(tool):
    t = Translator()
    [started] = t.translate(use(tool, {"text": "made-up resume line"}))
    [finished] = t.translate(done("made-up resume line"))
    assert started.detail is None and finished.detail is None


def test_no_tool_is_both_kept_and_private():
    assert not DETAIL_TOOLS & PRIVATE_TOOLS


def test_an_unknown_tool_keeps_nothing():
    t = Translator()
    [started] = t.translate(use("a_new_tool", {"x": 1}))
    [finished] = t.translate(done("ok"))
    assert started.detail is None and finished.detail is None


def test_the_prompt_of_a_subagent_is_not_kept():
    t = Translator()
    [started] = t.translate(AssistantMessage(model="m", content=[ToolUseBlock(
        id="a1", name="Agent", input={"subagent_type": "job-research", "description": "Read roles",
                                      "prompt": "made-up resume line"})]))
    assert started.detail is None


def test_a_long_value_is_cut_and_says_so():
    t = Translator()
    [started] = t.translate(use("google_search", {"query": "x" * (MAX_DETAIL + 50)}))
    assert started.detail["truncated"] is True
    assert isinstance(started.detail["input"], str) and len(started.detail["input"]) == MAX_DETAIL
    [finished] = t.translate(done("y" * (MAX_DETAIL + 50)))
    assert finished.detail == {"output": "y" * MAX_DETAIL, "truncated": True}


async def test_the_store_gives_the_input_and_the_output_of_a_step(store):
    goal = await store.create_goal("g")
    await store.append_event(goal["id"], "tool_started", {"tool_use_id": "t1", "tool": "google_search"},
                             {"input": {"query": "acme"}})
    await store.append_event(goal["id"], "tool_finished", {"tool_use_id": "t1", "tool": "google_search", "ok": True},
                             {"output": "3 results", "truncated": True})
    assert await store.step_detail(goal["id"], "t1") == {
        "tool": "google_search", "input": {"query": "acme"}, "output": "3 results", "truncated": True}
    assert await store.step_detail(goal["id"], "t-none") is None


@pytest.fixture
async def client(app_settings, store, hub, recorder):
    services = make_services(app_settings, store, hub, recorder, Streams(replies("ok")))
    sessions = GoalSessions(services, idle_s=600)
    app = create_app(app_settings, services, sessions, model_loader=lambda: [])
    async with signed_in_client(app) as http:
        yield http
    await sessions.close_all()


async def step(store, goal_id, tool, *, detail_in=None, detail_out=None, call="t1"):
    await store.append_event(goal_id, "tool_started", {"tool_use_id": call, "tool": tool, "agent": "main"}, detail_in)
    await store.append_event(goal_id, "tool_finished", {"tool_use_id": call, "tool": tool, "ok": True}, detail_out)


async def test_the_api_gives_a_kept_step(client, store):
    goal = await store.create_goal("g")
    await step(store, goal["id"], "google_search", detail_in={"input": {"query": "acme"}}, detail_out={"output": "ok"})
    body = (await client.get(f"/api/goals/{goal['id']}/steps/t1")).json()
    assert body == {"input": {"query": "acme"}, "output": "ok", "truncated": False, "kept": True}


async def test_the_api_says_when_a_step_keeps_nothing(client, store):
    goal = await store.create_goal("g")
    await step(store, goal["id"], "read_file")
    body = (await client.get(f"/api/goals/{goal['id']}/steps/t1")).json()
    assert body == {"input": None, "output": None, "truncated": False, "kept": False}


async def test_a_missing_step_or_goal_is_404(client, store):
    goal = await store.create_goal("g")
    assert (await client.get(f"/api/goals/{goal['id']}/steps/t-none")).status_code == 404
    assert (await client.get("/api/goals/00000000-0000-0000-0000-000000000000/steps/t1")).status_code == 404


async def test_the_log_downloads_as_json_lines_with_no_step_detail(client, store):
    goal = await store.create_goal("g")
    await step(store, goal["id"], "google_search", detail_in={"input": {"query": "secret-input"}},
               detail_out={"output": "secret-output"})
    response = await client.get(f"/api/goals/{goal['id']}/events.jsonl")
    assert response.status_code == 200
    assert response.headers["content-disposition"] == f'attachment; filename="mission-{goal["id"][:8]}-events.jsonl"'
    lines = [json.loads(line) for line in response.text.splitlines()]
    assert [line["type"] for line in lines] == ["tool_started", "tool_finished"]
    assert "secret-" not in response.text
    assert (await client.get("/api/goals/00000000-0000-0000-0000-000000000000/events.jsonl")).status_code == 404
