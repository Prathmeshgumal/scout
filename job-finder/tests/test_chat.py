import contextlib

import pytest
from rich.console import Console

from job_finder.chat import HELP, Chat, run_chat
from job_finder.cli import CliError

from .fakes import Streams, agent_call, make_services, replies, result, task_started


@pytest.fixture
def streams():
    return Streams()


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


@pytest.fixture
def out():
    return Console(record=True, width=120)


class FakePrompt:
    """Answers prompt_async from a list: text, an exception to raise, or an async callable that gives the text."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asked: list[str] = []

    async def prompt_async(self, message):
        self.asked.append(message)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = await answer()
        return answer


@contextlib.asynccontextmanager
async def chat_on(services, out, goal_id):
    async with services.drive(goal_id, recover=False) as session:
        yield Chat(services, session, out)


async def busy_with_job_gen(streams):
    streams.last.push(agent_call("call-1", "job-gen", "Find 5"), task_started("a1", "call-1", "Find 5"),
                      result(cost=0.02))
    await streams.last.flush()


async def test_new_goal_runs_and_shows_the_reply(services, out, store, streams):
    streams.on_query = replies("No jobs yet: job-gen is not built.")

    async def exit_when_settled():
        [goal] = await store.list_goals()
        await streams.last.flush()
        return "exit"

    await run_chat(services, out, prompt=FakePrompt("Find 20 jobs", exit_when_settled))
    [goal] = await store.list_goals()
    assert streams.last.queries == ["Find 20 jobs"] and goal["status"] == "done"
    text = out.export_text()
    assert "New goal" in text and "No jobs yet" in text


async def test_typing_while_the_main_agent_works_reaches_it_at_once(services, out, store, streams):
    # Review focus 5: no "Still working" reply, no lost message.
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        await chat.handle("Find jobs")
        assert chat.session.main_busy
        await chat.handle("skip Germany")
        assert streams.last.queries == ["Find jobs", "skip Germany"]


async def test_idle_message_has_the_pending_note(services, out, store, streams):
    goal = await store.create_goal("g")
    await store.add_scrape(goal["id"], "rq-1", "linkedin_company", "u", True)
    async with chat_on(services, out, goal["id"]) as chat:
        await chat.handle("10 more")
    assert streams.last.queries[0].startswith("10 more\n\n(Note: 1 scrape for this goal is still pending")


async def test_ctrl_c_stops_a_reply_and_closes_when_idle(services, out, store, streams):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        assert await chat.ctrl_c(FakePrompt()) is False  # nothing runs: Ctrl-C closes the chat
        await chat.handle("go")
        assert await chat.ctrl_c(FakePrompt()) is True and streams.last.interrupts == 1


async def test_ctrl_c_asks_before_it_stops_subagents(services, out, store, streams):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        await chat.handle("Find jobs")
        await busy_with_job_gen(streams)
        prompt = FakePrompt("n", "y")
        assert await chat.ctrl_c(prompt) is True and streams.last.stopped == []
        assert await chat.ctrl_c(prompt) is True and streams.last.stopped == ["a1"]
        assert prompt.asked[0] == "This also stops job-gen. Stop everything? [y/N] "
    assert (await store.get_goal(goal["id"]))["status"] == "stopped"


async def test_close_asks_while_subagents_work(services, out, store, streams):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        await chat.handle("Find jobs")
        await busy_with_job_gen(streams)
        prompt = FakePrompt("n", "y")
        assert await chat.confirm_close(prompt) is False
        assert await chat.confirm_close(prompt) is True
        assert prompt.asked[0].startswith("1 subagent is still working (job-gen). Exit now?")
    assert (await store.list_tasks(goal["id"]))[0]["status"] == "interrupted"


async def test_close_does_not_ask_when_nothing_runs(services, out, store):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        prompt = FakePrompt()
        assert await chat.confirm_close(prompt) is True and prompt.asked == []


async def test_status_lists_running_subagents_and_stop_ends_them(services, out, store, streams):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        await chat.handle("Find jobs")
        await busy_with_job_gen(streams)
        await chat.handle("/status")
        text = out.export_text()
        assert "Spend: $0.02 of $2.00" in text and "job-gen: Find 5 (0m 0" in text
        await chat.handle("/stop")
        assert streams.last.stopped == ["a1"]
    assert (await store.get_goal(goal["id"]))["status"] == "stopped"


async def test_retry_only_for_failed_or_stopped(services, out, store, streams):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        await store.set_status(goal["id"], "done")
        await chat.handle("/retry")
        assert "Only a failed or stopped goal" in out.export_text()
        await store.set_status(goal["id"], "failed", "boom")
        await store.set_sdk_session(goal["id"], "sess-1", 0.0)
        await chat.handle("/retry")
        assert streams.last.queries == ["The previous attempt failed (boom). Continue working toward the goal."]


async def test_commands_and_exit(services, out, store):
    goal = await store.create_goal("g")
    async with chat_on(services, out, goal["id"]) as chat:
        assert await chat.handle("   ") is True
        assert await chat.handle("/x") is True and HELP in out.export_text()
        assert await chat.handle("/status") is True and "0 roles" in out.export_text()
        assert await chat.handle("exit") is False


async def test_chat_refuses_a_subagent_goal(services, out, store):
    goal = await store.create_goal("dev", agent="job-research")
    with pytest.raises(CliError, match="is a job-research goal"):
        await run_chat(services, out, goal["id"][:8], prompt=FakePrompt())


async def test_reopen_shows_the_history(services, out, store, recorder):
    goal = await store.create_goal("g")
    await store.set_status(goal["id"], "done")
    await recorder.record(goal["id"], "user_message", text="Find 20 jobs")
    await recorder.record(goal["id"], "text", text="Here is what I found.", agent="main")
    await run_chat(services, out, goal["id"][:8], prompt=FakePrompt("exit"))
    text = out.export_text()
    assert "Find 20 jobs" in text and "Here is what I found." in text
