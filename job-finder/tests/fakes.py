"""Fakes shared by the tests: a streaming SDK client, SDK messages, and the app services around them."""

import asyncio

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from job_finder.artifact_store import ArtifactStore
from job_finder.career_lens import CareerLens
from job_finder.files import FileStore
from job_finder.services import Services


def result(subtype="success", *, is_error=False, api_error_status=None, cost=0.1, origin=None, session_id="sess-1",
           usage=None):
    """origin: {"kind": "task-notification"} for a response that the SDK started itself. usage: the tokens of the
    main agent's steps in this response."""
    return ResultMessage(subtype=subtype, duration_ms=5, duration_api_ms=5, is_error=is_error, num_turns=3,
                         session_id=session_id, total_cost_usd=cost, api_error_status=api_error_status,
                         origin=origin, usage=usage)


def init(session_id):
    return SystemMessage(subtype="init", data={"session_id": session_id})


async def _no_sleep(seconds):
    return None



class Cancels(CareerLens):
    """Records the requests that are cancelled in career_lens."""

    def __init__(self):
        super().__init__("", "")
        self.asked: list[str] = []

    async def cancel_all(self, request_ids):
        self.asked += request_ids
        return dict.fromkeys(request_ids, "cancelled")


class FakeObjects:
    """The file storage in memory. `down` makes each call fail."""

    def __init__(self):
        self.saved: dict[str, bytes] = {}
        self.down = False

    def _check(self) -> None:
        if self.down:
            raise ConnectionError("storage is down")

    async def put(self, key, data, content_type):
        self._check()
        self.saved[key] = data

    async def get(self, key):
        self._check()
        return self.saved[key]

    async def remove(self, key):
        self._check()
        del self.saved[key]


def make_services(settings, store, hub, recorder, factory) -> Services:
    return Services(settings, store, ArtifactStore(store.pool), hub, recorder, CareerLens("", ""),
                    client_factory=factory, sleep=_no_sleep, files=FileStore(store.pool, FakeObjects()))


def task_started(task_id, tool_use_id, description="task"):
    return TaskStartedMessage(subtype="task_started", data={}, task_id=task_id, description=description,
                              uuid="u", session_id="s", tool_use_id=tool_use_id, task_type="local_agent")


def task_updated(task_id, status):
    return TaskUpdatedMessage(subtype="task_updated", data={}, task_id=task_id, patch={"status": status},
                              status=status, session_id="s", uuid="u")


def task_notification(task_id, status="completed", summary="", tool_use_id=None, usage=None):
    return TaskNotificationMessage(subtype="task_notification", data={}, task_id=task_id, status=status,
                                   output_file="", summary=summary, uuid="u", session_id="s",
                                   tool_use_id=tool_use_id, usage=usage)


def task_progress(task_id, tokens, tool_uses, duration_ms, last_tool=None):
    return TaskProgressMessage(subtype="task_progress", data={}, task_id=task_id, description="task",
                               usage={"total_tokens": tokens, "tool_uses": tool_uses, "duration_ms": duration_ms},
                               uuid="u", session_id="s", last_tool_name=last_tool)


def tool_call(tool_use_id, name, tool_input=None, parent=None):
    """An agent calls a tool. parent: the Agent call of the subagent that calls it; None for the main agent."""
    return AssistantMessage(model="m", content=[ToolUseBlock(id=tool_use_id, name=name, input=tool_input or {})],
                            parent_tool_use_id=parent)


def tool_result(tool_use_id, text, *, is_error=False, parent=None):
    return UserMessage(content=[ToolResultBlock(tool_use_id=tool_use_id, content=[{"type": "text", "text": text}],
                                                is_error=is_error)], parent_tool_use_id=parent)


class StreamingClient:
    """A fake ClaudeSDKClient in streaming mode. A test pushes SDK messages; the client records calls."""

    def __init__(self, options, on_query=None):
        self.options = options
        self.on_query = on_query  # on_query(client, text) can push the reply
        self.queries: list[str] = []
        self.stopped: list[str] = []
        self.interrupts = 0
        self.connected = self.closed = False
        self._inbox: asyncio.Queue = asyncio.Queue()

    def push(self, *messages):
        for message in messages:
            self._inbox.put_nowait(message)

    async def flush(self):
        """Return when the session has handled every message pushed before this call."""
        reached = asyncio.Event()
        self._inbox.put_nowait(reached)
        await asyncio.wait_for(reached.wait(), 2)

    async def connect(self, prompt=None):
        self.connected = True

    async def disconnect(self):
        self.closed = True
        self._inbox.put_nowait(None)

    async def query(self, text):
        self.queries.append(text)
        if self.on_query:
            self.on_query(self, text)

    async def interrupt(self):
        self.interrupts += 1

    async def stop_task(self, task_id):
        self.stopped.append(task_id)

    async def receive_messages(self):
        while True:
            message = await self._inbox.get()
            if message is None:
                return
            if isinstance(message, asyncio.Event):
                message.set()  # the generator gets here only after the session handled the message before
                continue
            if isinstance(message, Exception):
                raise message
            yield message


class Streams:
    """A client factory for StreamingClient. It keeps every client that it made."""

    def __init__(self, on_query=None):
        self.on_query = on_query
        self.clients: list[StreamingClient] = []

    def __call__(self, options):
        client = StreamingClient(options, self.on_query)
        self.clients.append(client)
        return client

    @property
    def last(self) -> StreamingClient:
        return self.clients[-1]


def assistant(text, parent=None, model="m"):
    return AssistantMessage(model=model, content=[TextBlock(text=text)], parent_tool_use_id=parent)


def _usage(input, output, cache_read, cache_write):
    return {"input_tokens": input, "output_tokens": output, "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write}


def step(message_id, parent, *, output, input=2, cache_read=0, cache_write=0):
    """One piece of a subagent's step, with the usage of the step so far: the SDK sends a step as several pieces
    with the same id, before the step ends."""
    return AssistantMessage(model="m", content=[TextBlock(text="...")], parent_tool_use_id=parent,
                            message_id=message_id, usage=_usage(input, output, cache_read, cache_write))


def agent_call(call_id, name, description="task"):
    return AssistantMessage(model="m", content=[ToolUseBlock(id=call_id, name="Agent", input={
        "subagent_type": name, "description": description, "prompt": description, "run_in_background": True})])


def replies(*texts):
    """An on_query callback: each query gets the next text as the main agent's answer, then a result."""
    answers = list(texts)

    def on_query(client, text):
        client.push(assistant(answers.pop(0) if answers else "OK."), result(cost=0.01 * len(client.queries)))
    return on_query


# The models that the SDK gave on 2026-10-09 (client.get_server_info()["models"]), for the model menu.
ALL_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
SDK_MODELS = [
    {"value": "default", "resolvedModel": "claude-sonnet-5-5", "displayName": "Default (recommended)",
     "description": "Sonnet 5.5 · Efficient for routine tasks", "supportsEffort": True,
     "supportedEffortLevels": ALL_EFFORTS},
    {"value": "sonnet", "resolvedModel": "claude-sonnet-5-5", "displayName": "Sonnet",
     "description": "Sonnet 5.5 · Efficient for routine tasks", "supportsEffort": True,
     "supportedEffortLevels": ALL_EFFORTS},
    {"value": "claude-fable-5-1", "resolvedModel": "claude-fable-5-1", "displayName": "Fable",
     "description": "Fable 5.1 · Most capable for your hardest and longest-running tasks · Requires usage credits",
     "supportsEffort": True, "supportedEffortLevels": ALL_EFFORTS},
    {"value": "opus", "resolvedModel": "claude-opus-5-5", "displayName": "Opus",
     "description": "Opus 5.5 · Best for everyday, complex tasks", "supportsEffort": True,
     "supportedEffortLevels": ALL_EFFORTS},
    {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001", "displayName": "Haiku",
     "description": "Haiku 4.5 · Fastest for quick answers"},
]


def roles(*companies: str) -> list[dict]:
    """One role at each of these companies, for tests that need saved roles."""
    return [{"url": f"https://jobs.lever.co/{name.lower()}/1", "title": "Backend Engineer", "company_name": name}
            for name in companies]


async def fit_roles(store, goal_id: str, names: list[str], verdict: str = "fit") -> None:
    """Save one role at each company, with snippet evidence and this verdict."""
    artifacts = ArtifactStore(store.pool)
    for row in await artifacts.save_jobs(goal_id, roles(*names), "job-discovery"):
        await artifacts.save_job_research(goal_id, row["job_id"], None, "snippet", None)
        await artifacts.set_fit(goal_id, row["job_id"], verdict, "reasons", [])
