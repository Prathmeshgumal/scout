from dataclasses import replace
from datetime import UTC, datetime

import pytest

from job_finder.career_lens import CareerLens
from job_finder.mandate_store import MandateStore
from job_finder.tools import ToolContext
from job_finder.tools.mandates import build_tools

NOW = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)
PROFILE = {"target_titles": {"value": ["Backend Engineer"], "basis": "stated"},
           "locations": {"value": ["Pune"], "basis": "stated"}}
NEXT_PROFILE = {**PROFILE, "locations": {"value": ["Pune", "Remote"], "basis": "stated"}}


@pytest.fixture
async def ctx(store, recorder, app_settings, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # the time zone of this machine
    goal = await store.create_goal("Find EU fintech jobs every weekday")
    await store.set_profile(goal["id"], PROFILE, ready=True)
    changed = []
    ctx = ToolContext(goal["id"], store, recorder, CareerLens("", ""),
                      settings=app_settings, now=lambda: NOW,
                      on_change=lambda: changed.append(True))
    ctx.changed = changed
    await say(ctx, "Find Series B fintechs in the EU, 20 each weekday")
    return ctx


async def say(ctx, text):
    """A message from the user in this chat."""
    await ctx.recorder.record(ctx.goal_id, "user_message", text=text, trigger="user")


class Chat:
    """The tools of one session: a proposal waits in them for the user's yes."""

    def __init__(self, ctx, kind):
        self.tools = {t.name: t.handler for t in build_tools(ctx, kind)}

    async def __call__(self, name, args=None):
        return await self.tools[name](args or {})


def text(result):
    return result["content"][0]["text"]


def test_each_kind_gets_its_own_tools(ctx):
    assert list(Chat(ctx, "goal").tools) == ["propose_mandate", "confirm_mandate"]
    assert list(Chat(ctx, "home").tools) == ["get_mandate", "list_runs", "propose_mandate", "confirm_mandate"]


async def test_a_proposal_saves_nothing_and_shows_the_full_table(ctx, store):
    chat = Chat(ctx, "goal")
    out = await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    assert "is_error" not in out and "Nothing is saved yet" in text(out)
    assert "| Quota for each goal | 20 fit roles |" in text(out) and "| Total spend limit | $10.00 |" in text(out)
    assert "| Next goal | Thu 8 Oct 2026 12:00 |" in text(out)  # the first goal starts at once
    assert (await store.get_goal(ctx.goal_id))["home_of"] is None


async def test_confirm_needs_a_new_message_from_the_user(ctx, store):
    chat = Chat(ctx, "goal")
    refused = await chat("confirm_mandate")
    assert refused["is_error"] and "propose_mandate" in text(refused)
    await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    refused = await chat("confirm_mandate")  # in the same turn: the user did not answer yet
    assert refused["is_error"] and "Ask the user" in text(refused)
    assert (await store.get_goal(ctx.goal_id))["home_of"] is None
    await say(ctx, "yes")
    out = await chat("confirm_mandate")
    assert "is_error" not in out and "| Quota for each goal | 20 fit roles |" in text(out)
    assert "This chat now manages the mandate" in text(out) and "home chat" not in text(out)
    assert (await store.get_goal(ctx.goal_id))["home_of"] is not None and ctx.changed == [True]
    again = await chat("confirm_mandate")
    assert again["is_error"]  # the proposal is used


async def test_a_new_proposal_replaces_the_old_one(ctx, pool):
    chat = Chat(ctx, "goal")
    await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    await say(ctx, "make it 5")
    await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 5})
    await say(ctx, "yes")
    await chat("confirm_mandate")
    assert (await MandateStore(pool).by_home(ctx.goal_id))["jobs_per_run"] == 5


async def test_a_bad_value_comes_back_as_an_error(ctx):
    out = await Chat(ctx, "goal")("propose_mandate", {"instruction": "x", "jobs_per_run": 1, "every": "30h"})
    assert out["is_error"] and "from 1 to 23" in text(out)


async def make_home(ctx):
    chat = Chat(ctx, "goal")
    await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    await say(ctx, "yes")
    await chat("confirm_mandate")
    return Chat(ctx, "home")


async def test_a_proposal_is_refused_in_a_run_and_in_a_home_chat(ctx, pool):
    await make_home(ctx)
    out = await Chat(ctx, "goal")("propose_mandate", {"instruction": "x", "jobs_per_run": 1})
    assert out["is_error"] and "already" in text(out)
    m = await MandateStore(pool).by_home(ctx.goal_id)
    run = await MandateStore(pool).create_run(m, NOW, 2.0, 1)
    out = await Chat(replace(ctx, goal_id=run["id"]), "goal")("propose_mandate", {"instruction": "y", "jobs_per_run": 1})
    assert out["is_error"] and "scheduled goal" in text(out)


async def test_home_changes_wait_for_a_yes(ctx, pool):
    home = await make_home(ctx)
    out = await home("propose_mandate", {"every": "6h", "jobs_per_run": 100, "window_end": "21:00"})
    assert "Nothing is saved yet" in text(out) and "| Quota for each goal | 20 fit roles | 100 fit roles |" in text(out)
    assert (await MandateStore(pool).by_home(ctx.goal_id))["jobs_per_run"] == 20
    await say(ctx, "yes")
    out = await home("confirm_mandate")
    assert "is_error" not in out and "| Interval | every 6 hours |" in text(out)
    assert "| Quota for each goal | 100 fit roles |" in text(out) and "| No goals after | 21:00 |" in text(out)
    assert "To change, pause, resume or end the mandate, ask here in this chat." in text(out)


async def test_pause_resume_start_now_and_end(ctx, pool):
    home = await make_home(ctx)
    ms = MandateStore(pool)

    async def change(args):
        await home("propose_mandate", args)
        await say(ctx, "yes")
        return await home("confirm_mandate")

    assert "| Status | paused |" in text(await change({"status": "paused"}))
    assert "| Status | active |" in text(await change({"status": "active"}))
    await ms.update((await ms.by_home(ctx.goal_id))["id"], {"next_run_at": datetime(2030, 1, 1, tzinfo=UTC)})
    await change({"start_now": True})
    assert (await ms.by_home(ctx.goal_id))["next_run_at"] == NOW
    assert "No goals yet." == text(await home("list_runs"))
    assert "| Status | ended |" in text(await change({"status": "ended"}))
    refused = await home("propose_mandate", {"jobs_per_run": 5})
    assert refused["is_error"] and "ended" in text(refused)
    assert "| Status | ended |" in text(await home("get_mandate"))


async def test_a_proposal_gives_the_buttons_for_the_answer(ctx, pool):
    out = await Chat(ctx, "goal")("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    assert text(out).endswith("[[Start mandate | Change settings]]")
    home = await make_home(ctx)
    buttons = {"[[Save change | Discard]]": {"jobs_per_run": 5},
               "[[Pause mandate | Keep running]]": {"status": "paused"},
               "[[!End mandate | Keep mandate]]": {"status": "ended"},
               "[[Start goal now | Cancel]]": {"start_now": True}}
    for line, args in buttons.items():
        assert text(await home("propose_mandate", args)).endswith(line), line
    assert text(await home("propose_mandate", {"status": "paused", "jobs_per_run": 5})).endswith(
        "[[Save change | Discard]]")
    ms = MandateStore(pool)
    await ms.update((await ms.by_home(ctx.goal_id))["id"], {"status": "paused"})
    assert text(await home("propose_mandate", {"status": "active"})).endswith("[[Resume mandate | Keep paused]]")


async def test_a_mandate_takes_the_ready_profile_of_its_chat_and_each_goal_copies_it(ctx, store):
    chat = Chat(ctx, "goal")
    new = {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20}
    await store.set_profile(ctx.goal_id, PROFILE, ready=False)
    assert "not ready" in text(await chat("propose_mandate", new))
    await store.set_profile(ctx.goal_id, PROFILE, ready=True)  # what the candidate-profile subagent saves
    assert "| Profile | Titles: Backend Engineer. Places: Pune |" in text(await chat("propose_mandate", new))
    await say(ctx, "Yes")
    await chat("confirm_mandate")
    m = await MandateStore(store.pool).by_home(ctx.goal_id)
    run = await MandateStore(store.pool).create_run(m, NOW, 1.5, 1)
    assert (run["profile"], run["profile_ready"]) == (PROFILE, True)
    # a change of the profile: the subagent saves a new one in the chat, and new_profile takes it
    home = Chat(ctx, "home")
    assert "no new profile" in text(await home("propose_mandate", {"new_profile": True}))
    await store.set_profile(ctx.goal_id, NEXT_PROFILE, ready=True)
    assert "| Profile | Titles: Backend Engineer. Places: Pune | Titles: Backend Engineer. Places: Pune, Remote |" in text(
        await home("propose_mandate", {"new_profile": True}))
    await say(ctx, "Yes")
    await home("confirm_mandate")
    assert (await MandateStore(store.pool).by_home(ctx.goal_id))["profile"] == NEXT_PROFILE


async def test_a_proposal_uses_the_time_zone_of_the_user(ctx, store):
    await store.set_timezone(ctx.goal_id, "America/New_York")
    chat = Chat(ctx, "goal")
    out = await chat("propose_mandate", {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20})
    assert "| Time zone | America/New_York |" in text(out)
    assert "| Next goal | Thu 8 Oct 2026 02:30 |" in text(out)  # 06:30 UTC


async def test_the_time_limit_of_each_goal_is_a_setting_that_waits_for_a_yes(ctx, pool):
    home = await make_home(ctx)
    out = await home("propose_mandate", {"time_limit_minutes": "3h", "grace_minutes": 15})
    assert "| Time limit for each goal | 2h | 3h |" in text(out) and "| Extra time to finish | 30m | 15m |" in text(out)
    await say(ctx, "yes")
    out = await home("confirm_mandate")
    assert "| Time limit for each goal | 3h |" in text(out) and "| Extra time to finish | 15m |" in text(out)
    m = await MandateStore(pool).by_home(ctx.goal_id)
    assert (m["time_limit_minutes"], m["grace_minutes"]) == (180, 15)
    bad = await home("propose_mandate", {"time_limit_minutes": "4m"})
    assert bad["is_error"] and "5 minutes or more" in text(bad)
    ok = await home("propose_mandate", {"time_limit_minutes": "5m"})
    assert "is_error" not in ok


def test_the_tool_schema_takes_the_time_limit_as_minutes_or_as_text():
    from job_finder.tools.mandates import SETTINGS
    for key in ("time_limit_minutes", "grace_minutes"):
        assert SETTINGS[key]["type"] == ["integer", "string"] and "2h" in SETTINGS["time_limit_minutes"]["description"]
