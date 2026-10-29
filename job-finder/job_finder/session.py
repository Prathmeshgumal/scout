"""One open goal: a long-lived Agent SDK session, its subagent tasks, its spend and its status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ProcessError,
    ResultMessage,
    TaskNotificationMessage,
    TaskProgressMessage,
    TaskStartedMessage,
    ToolResultBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from .artifact_store import ArtifactStore
from .career_lens import CareerLens
from .config import Settings
from .events import Recorder
from .guard import SubagentGuard
from .options import build_hooks, build_options, goal_kind, home_prompt, prompt_sha, session_exists, subagent_spec
from .page_reader import PageReader
from .recap import build_recap
from .store import GoalNotFound, Store
from .time_limit import TIME_LIMIT_PROMPT, TIME_REASON, past_hard_limit, run_seconds, soft_notice
from .tools import ToolContext
from .translate import Translator, summary_line, task_status

log = logging.getLogger(__name__)
STOP_EXIT_CODES = {130, 143, -2, -15}  # Claude Code ended by SIGINT or SIGTERM
WRAP_UP_USD = 0.20  # kept back from the goal's cap for the final short answer
WRAP_UP_TURNS = 3
WRAP_UP_PROMPT = ("You've reached this goal's limits. Write the best answer you can from what you have, "
                  "with sources, and list what's missing.")
SETTLED = {"done", "capped", "failed", "stopped"}
WATCHER_TRIGGERS = {"scrapes_finished", "wait_expired"}  # messages from the scrape watcher
TRANSIENT_HTTP = {408, 409, 429, 500, 502, 503, 504, 529}
# AssistantMessage.error values that retrying cannot fix; any other value is treated as temporary.
PERSISTENT_ASSISTANT_ERRORS = {
    "authentication_failed": "Claude authentication failed: check ANTHROPIC_API_KEY.",
    "billing_error": "Claude billing error: check the Anthropic account.",
    "invalid_request": "Claude rejected the request (invalid request).",
}
TOKEN_AUTH_FAILED = ("Claude authentication failed: the subscription token may have expired. "
                     "Run `job-finder login`.")
USAGE_LIMIT = "Claude usage limit reached; a subscription limit resets on the plan's schedule"
CONTINUE_DELAY_S = 5.0  # a finished subagent can start a new response just after the goal looks idle
WAKE_S = 10.0  # a subagent's notification starts a main response a moment later: the goal does not look done meanwhile
RESTARTED = "You were interrupted by an app restart."
RETRIED = "Your work stopped because of a temporary error."


def restart_note(first: str | None, interrupted: list[dict], resumable: bool) -> str | None:
    """What to tell the main agent when its work stopped: why, and which subagents did not finish."""
    parts = [first] if first else []
    if interrupted and resumable:
        names = ", ".join(f'{t["name"]} (agentId {t["task_id"]}, task "{t["description"]}")' for t in interrupted)
        parts.append(f"These subagents did not finish: {names}. Resume each one with SendMessage (to: its "
                     "agentId) to continue its work, or start new work if it is no longer needed.")
    elif interrupted:
        parts.append("Earlier subagents did not finish and cannot be resumed. Their saved work is in Postgres: "
                     "use list_jobs and read_job.")
    if parts and first:
        parts.append("Continue working toward the goal.")
    return " ".join(parts) or None


def assistant_error(error: str, auth_mode: str) -> tuple[str, bool]:
    """The message for an AssistantMessage error, and True when retrying cannot fix it."""
    if error == "authentication_failed" and auth_mode == "oauth_token":
        return TOKEN_AUTH_FAILED, True
    if error in PERSISTENT_ASSISTANT_ERRORS:
        return PERSISTENT_ASSISTANT_ERRORS[error], True
    if error == "rate_limit" and auth_mode == "oauth_token":
        return USAGE_LIMIT, False
    return f"Claude API error ({error}).", False


class GoalSession:
    """Owns the SDK client of one goal while its chat is open. See docs/architecture.md."""

    def __init__(self, settings: Settings, store: Store, recorder: Recorder, career_lens: CareerLens,
                 goal_id: str, *, client_factory=ClaudeSDKClient, sleep=asyncio.sleep):
        self.settings, self.store, self.recorder, self.career_lens = settings, store, recorder, career_lens
        self.goal_id = goal_id
        self._client_factory, self._sleep = client_factory, sleep
        self.client = None
        self.translator = Translator()
        self.main_busy = False
        self.tasks: dict[str, dict] = {}  # running subagent tasks: task_id -> name, description, started, last_tool
        self.waiting: set[str] = set()  # career-lens requests that a tool call waits on now: a stop cancels them
        self.guard = SubagentGuard(lambda: [task["name"] for task in self.tasks.values()])  # one run of each type
        self.last_cost = 0.0  # the SDK session's cumulative cost at its last result
        self._pending_queries = 0  # messages sent that have no result yet
        self._interrupting = False
        self._stray = False  # after a stop, a main response that nobody asked for: it is stopped and not shown
        self._closing = False
        self._fixed: tuple[str, str | None] | None = None  # capped, failed or stopped: kept until the next send
        self._capped: str | None = None  # the reason, once the spend cap is reached: answers are wrap-ups
        self._retried = False  # one reconnect after a temporary error, until a response succeeds
        self._assistant_error: str | None = None
        self._prefix: str | None = None  # a recap to put before the next message, when the SDK session is gone
        self._reader: asyncio.Task | None = None
        self._query_lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._continuing: asyncio.Task | None = None  # a "continue toward the target" message about to be sent
        self.needs_reopen = False  # the goal became a home chat: the next message opens a session with the home tools
        self._waking: asyncio.Task | None = None  # a subagent finished: the main agent answers its notification soon

    # Opening and closing

    async def open(self, *, recover: bool = True) -> None:
        """Connect. With recover, tell the main agent what stopped since the last open."""
        goal = await self.store.get_goal(self.goal_id)
        if goal is None:
            raise GoalNotFound(self.goal_id)
        subagent_spec(goal)  # an unknown subagent fails here, before a client starts
        session_id = goal["sdk_session_id"]
        resumable = bool(session_id) and session_exists(self.settings.claude_config_dir, session_id)
        if session_id and not resumable:
            self._prefix = build_recap(await self.store.list_events(self.goal_id),
                                       await self.store.list_scrapes(self.goal_id))
            await self._notice("Earlier context lost, continuing from a summary.")
            await self.store.set_sdk_session(self.goal_id, None, 0.0)
            goal["sdk_session_id"], goal["sdk_cost_usd"] = None, 0.0
        if resumable and self._needs_home_prompt(goal):
            await self.store.set_sdk_session(self.goal_id, None, 0.0)
            goal["sdk_session_id"], goal["sdk_cost_usd"] = None, 0.0
            resumable = False
        if self.budget(goal) <= 0:
            self._capped = "Spend limit reached."  # the next message gets a wrap-up answer
            return
        if (goal["status"] in SETTLED and goal["soft_limit_at"] is not None
                and past_hard_limit(goal, await run_seconds(self.store, goal))):
            self._capped = TIME_REASON  # the next message gets a wrap-up answer; Continue gives the goal more time
            return
        await self._connect(goal, budget=self.budget(goal), max_turns=self.settings.max_turns)
        if recover:
            await self.store.interrupt_tasks(self.goal_id)  # rows of a process that stopped without a close
            interrupted = await self.store.list_tasks(self.goal_id, "interrupted")
            note = restart_note(RESTARTED if goal["status"] == "running" else None, interrupted, resumable)
            if note:
                await self.send(note, "restart", user=False)
            for task in interrupted:  # listed once; a resume through SendMessage makes the task run again
                await self.store.finish_task(task["task_id"], "stopped", "Interrupted. The main agent was told.")

    def _needs_home_prompt(self, goal: dict) -> bool:
        """True when the goal is a home chat and its SDK session started with an other prompt. A resumed session
        keeps its first system prompt, so the home chat must start a new session to get the home prompt."""
        if goal["agent"] or goal_kind(goal) != "home":
            return False
        return goal["session_prompt_sha"] not in (None, prompt_sha(home_prompt(self.goal_id)))

    @staticmethod
    def budget(goal: dict) -> float:
        return round(goal["max_spend_usd"] - goal["spend_usd"] - WRAP_UP_USD, 4)

    async def _connect(self, goal: dict, *, budget: float, max_turns: int, closed: bool = False,
                       resume: str | None = None) -> None:
        ctx = ToolContext(self.goal_id, self.store, self.recorder, self.career_lens,
                          research_max_age_days=self.settings.research_max_age_days,
                          poll_timeout_s=self.settings.web_wait_minutes * 60,
                          scrape_wait_s=self.settings.scrape_wait_minutes * 60,
                          reader=PageReader.for_settings(self.settings), settings=self.settings,
                          on_change=self._mark_reopen, waiting=self.waiting)
        hooks = build_hooks(goal, self.store, self._notice, closed=closed, guard=self.guard)
        resume = resume if resume is not None else goal["sdk_session_id"]
        options = build_options(self.settings, goal, ctx, hooks, budget=budget, max_turns=max_turns, resume=resume)
        self.client = self._client_factory(options)
        await self.client.connect()
        if not resume:
            await self.store.set_session_prompt(self.goal_id, prompt_sha(options.system_prompt))
        # A resumed SDK session reports its cost from its earlier total on; a new session starts from 0.
        self.last_cost = goal["sdk_cost_usd"] if resume and resume == goal["sdk_session_id"] else 0.0
        self.translator.seed(await self.store.list_tasks(self.goal_id))
        self._reader = asyncio.create_task(self._read(self.client))

    def _artifacts(self) -> ArtifactStore:
        return ArtifactStore(self.store.pool, self.settings.research_max_age_days)

    def _mark_reopen(self) -> None:
        self.needs_reopen = True

    async def _disconnect(self) -> None:
        reader, client = self._reader, self.client
        self._reader = self.client = None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
        if reader is not None and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)

    async def close(self) -> None:
        """Close the chat. Running subagents become interrupted, so that the next open can resume them."""
        self._closing = True
        for task in (self._continuing, self._waking):
            if task is not None:
                task.cancel()
        await self.store.interrupt_tasks(self.goal_id)
        self.tasks.clear()
        await self._disconnect()

    # Messages in

    async def send(self, text: str, trigger: str, *, user: bool = True, **data) -> None:
        """Give the main agent a message. If it is busy, the SDK queues the message for its next response."""
        if user:
            await self.recorder.record(self.goal_id, "user_message", text=text, trigger=trigger)
        else:
            await self.recorder.record(self.goal_id, "notice", text=text, trigger=trigger, **data)
        if self._capped and trigger != "wrap_up":
            if not user and trigger in WATCHER_TRIGGERS:
                return  # the goal is at its limit: finished scrapes start no new work
            await self._wrap_up(self._capped, user_message=text if user else None)
            return
        self._fixed = None
        self._pending_queries += 1
        self.main_busy = True
        await self._refresh()
        if self._prefix:
            text, self._prefix = f"{self._prefix}\n\n{text}", None
        async with self._query_lock:
            await self.client.query(text)

    async def interrupt(self) -> bool:
        """Stop the main agent's current response. The SDK stops running subagents too. False when idle."""
        if not self.main_busy:
            return False
        self._interrupting = True
        await self.client.interrupt()
        return True

    async def stop(self) -> None:
        """Stop all work on the goal, also in career-lens. Stopped subagents cannot be resumed."""
        waiting = list(self.waiting)  # before the stops: a stopped tool call forgets its request
        for task_id in list(self.tasks):
            with contextlib.suppress(Exception):
                await self.client.stop_task(task_id)
            await self.store.finish_task(task_id, "stopped", None)
        self.tasks.clear()
        if self.main_busy:
            await self.interrupt()
        await self._cancel_scrapes(waiting)
        await self._fix("stopped", "Stopped by the user")

    async def halt(self, reason: str) -> None:
        """career-lens is down: stop the work with a clear reason. Retry continues it. The scrapes stay pending in
        career-lens: they can still finish there."""
        if self._closing or self._fixed is not None or not (self.main_busy or self.tasks):
            return
        for task_id in list(self.tasks):
            with contextlib.suppress(Exception):
                await self.client.stop_task(task_id)
            await self.store.finish_task(task_id, "stopped", reason)
        self.tasks.clear()
        if self.main_busy:
            await self.interrupt()
        await self._notice(reason)
        await self._fix("stopped", reason)

    async def _cancel_scrapes(self, waiting: list[str], *, not_read: bool = False) -> None:
        """Cancel the pending scrapes of the goal, and the requests that its tool calls wait on, in career-lens too.
        A request that another goal still waits on stays. not_read: the time limit ended the wait, so a company whose
        jobs did not come counts as not read."""
        scrapes = await self.store.cancel_pending_scrapes(self.goal_id)
        answers = await self.career_lens.cancel_all(
            await self.store.unwanted([*(scrape["request_id"] for scrape in scrapes), *waiting]))
        for scrape in scrapes:
            await self.recorder.record(self.goal_id, "scrape_finished", request_id=scrape["request_id"],
                                       status="cancelled", failure_kind=None,
                                       answer=answers.get(scrape["request_id"]))
            if not_read and scrape["wants_jobs"]:
                await self._artifacts().record_job_read(self.goal_id, scrape["target"], None, "failed")

    async def reach_time_limit(self, *, hard: bool) -> None:
        """The goal is at its time limit. Soft: no new work starts (the hooks refuse it), and the main agent reports
        when the running subagents finish. Hard: stop all work and get a final answer, as at the spend cap."""
        if self._closing or (self._capped and self.client is not None):
            return  # a final answer is on its way
        # A goal at its spend cap with no client waits for a message. It can wait on scrapes: the hard step ends it.
        first = await self.store.set_soft_limit(self.goal_id)
        if self._continuing is not None:
            self._continuing.cancel()
            self._continuing = None
        if hard:
            waiting = list(self.waiting)
            await self._cancel_scrapes(waiting, not_read=True)
            await self._wrap_up(TIME_REASON)
            return
        if first:
            goal = await self.store.get_goal(self.goal_id)
            await self.send(f"{soft_notice(goal)} {TIME_LIMIT_PROMPT}", "time_limit", user=False)

    # Messages out

    async def _read(self, client) -> None:
        try:
            async for message in client.receive_messages():
                await self._handle(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("goal %s: the SDK stream failed: %s", self.goal_id, exc)
            if not self._closing and self.client is client:
                # A stop signal (Ctrl+C or a stop of the server) reaches Claude Code before the app closes the
                # session. That is not a problem to show to the user.
                quiet = isinstance(exc, ProcessError) and exc.exit_code in STOP_EXIT_CODES
                await self._trouble(str(exc) or type(exc).__name__, transient=True, quiet=quiet)
            return
        if not self._closing and self.client is client:  # the stream ended, but nobody closed it
            await self._trouble("The Claude Code process stopped.", transient=True)

    async def _handle(self, message) -> None:
        changed = False
        if self._stopped_and_idle() and isinstance(message, (AssistantMessage, StreamEvent, UserMessage)):
            # A stopped subagent's notification woke the main agent. The user stopped all work: stop this response.
            if isinstance(message, AssistantMessage) and not self._stray:
                self._stray = True
                await self.client.interrupt()
            return
        if isinstance(message, AssistantMessage) and message.parent_tool_use_id is None:
            if message.error:
                self._assistant_error = str(message.error)
            if not self.main_busy:
                self.main_busy = True  # a response that a task notification started, with no send
                changed = True
            if self._waking is not None:
                self._waking.cancel()
                self._waking = None
        emits = self.translator.translate(message)
        for emit in emits:
            if emit.stored:
                await self.recorder.record(self.goal_id, emit.type, emit.detail, **emit.data)
            else:
                self.recorder.live(self.goal_id, emit.type, **emit.data)
        if isinstance(message, TaskStartedMessage):
            name = self.translator.task_name(message.task_id)
            await self.store.start_task(self.goal_id, message.task_id, name, message.description, message.tool_use_id)
            self.tasks[message.task_id] = {"task_id": message.task_id, "name": name,
                                           "description": message.description, "started": time.monotonic(),
                                           "last_tool": None}
            self.guard.started(name)
            changed = True
        elif (status := task_status(message)) is not None:
            # TaskUpdated ends a task with no summary. The TaskNotification after it carries the summary.
            summary = summary_line((message.summary or "").strip()) if isinstance(message, TaskNotificationMessage) else None
            await self.store.finish_task(message.task_id, status, summary or None)
            ended = self.tasks.pop(message.task_id, None) is not None
            if ended and not self.main_busy and self._waking is None:  # its notification starts a main response
                self._waking = asyncio.create_task(self._wake_wait())
            changed = ended or changed
        elif isinstance(message, TaskProgressMessage) and message.task_id in self.tasks:
            self.tasks[message.task_id]["last_tool"] = message.last_tool_name
        elif isinstance(message, ResultMessage):
            await self._result(message)
            changed = True
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:  # a failed Agent call must not keep its subagent type busy
                if isinstance(block, ToolResultBlock) and block.is_error:
                    self.guard.failed(block.tool_use_id)
        if changed:
            await self._refresh()

    async def _result(self, message: ResultMessage) -> None:
        cost = message.total_cost_usd or 0.0
        delta = max(cost - self.last_cost, 0.0)
        self.last_cost = max(cost, self.last_cost)
        spend = await self.store.add_spend(self.goal_id, delta)
        await self.recorder.record(self.goal_id, "response_finished", result=message.subtype,
                                   cost_usd=round(delta, 4), turns=message.num_turns, spend_usd=round(spend, 4))
        if message.session_id:
            await self.store.set_sdk_session(self.goal_id, message.session_id, self.last_cost)
        self._pending_queries = max(self._pending_queries - 1, 0)
        self.main_busy = self._pending_queries > 0
        interrupted, self._interrupting = self._interrupting, False
        error, self._assistant_error = self._assistant_error, None
        if self._stray:  # the response after a stop ended: no notice, the goal stays stopped
            self._stray = False
        elif interrupted:
            await self._notice("Stopped the reply.")
        elif self._capped:
            await self._fix("capped", self._capped)
        elif error:
            reason, persistent = assistant_error(error, self.settings.auth_mode)
            await self._trouble(reason, transient=not persistent)
        elif message.subtype == "error_max_budget_usd":
            await self._wrap_up("Spend limit reached.")
        elif message.subtype == "error_max_turns":
            await self._notice("Turn limit for one response reached.")
        elif message.subtype == "error_during_execution":
            status = message.api_error_status
            reason = "; ".join(message.errors or []) or (f"API error {status}" if status else "execution error")
            await self._trouble(reason, transient=status is None or status in TRANSIENT_HTTP)
        elif message.subtype == "success":
            self._retried = False
            # The SDK budget sees only the main agent's cost. read_page adds its small model to the goal too.
            if spend >= (await self.store.get_goal(self.goal_id))["max_spend_usd"] - WRAP_UP_USD:
                await self._wrap_up("Spend limit reached.")

    # Limits and errors

    async def _wrap_up(self, reason: str, *, user_message: str | None = None) -> None:
        """The spend cap or the hard time limit is reached: stop the subagents and get one short answer from a small
        new budget."""
        note = "Stopped at the time limit." if reason == TIME_REASON else "Stopped at the spend limit."
        for task_id in list(self.tasks):
            await self.store.finish_task(task_id, "stopped", note)
        self.tasks.clear()
        self._capped = reason
        await self._notice(f"{reason} Writing the best answer from what I have.")
        await self._disconnect()
        self._pending_queries, self.main_busy = 0, False
        goal = await self.store.get_goal(self.goal_id)
        await self._connect(goal, budget=WRAP_UP_USD, max_turns=WRAP_UP_TURNS, closed=True)
        prompt = WRAP_UP_PROMPT if user_message is None else (
            f"{WRAP_UP_PROMPT}\n\nThe user's latest message: {user_message}")
        await self.send(prompt, "wrap_up", user=False)

    async def _trouble(self, reason: str, *, transient: bool, quiet: bool = False) -> None:
        """A temporary error gets one reconnect, with a note about the subagents that stopped. Else: failed.
        quiet: do not tell the user."""
        if not transient or self._retried:
            await self._failed(reason)
            return
        self._retried = True
        delay = self.settings.retry_delay_s
        if not quiet:
            await self._notice(f"Temporary problem ({reason}). Retrying in {delay:g} seconds.")
        await self._disconnect()
        self._pending_queries, self.main_busy = 0, False
        interrupted = await self.store.interrupt_tasks(self.goal_id)
        self.tasks.clear()
        await self._sleep(delay)
        goal = await self.store.get_goal(self.goal_id)
        await self._connect(goal, budget=self.budget(goal), max_turns=self.settings.max_turns)
        await self.send(restart_note(RETRIED, interrupted, resumable=True), "retry", user=False)

    # Status

    async def _notice(self, text: str) -> None:
        await self.recorder.record(self.goal_id, "notice", text=text)

    async def _fix(self, status: str, reason: str | None) -> None:
        self._fixed = (status, reason)
        await self._refresh()

    async def _failed(self, reason: str) -> None:
        self._pending_queries, self.main_busy = 0, False
        await self._fix("failed", reason)

    async def _refresh(self) -> None:
        if self._fixed is not None:
            status, reason = self._fixed
        elif self.main_busy or self.tasks or self._waking is not None:
            status, reason = "running", None
        elif pending := await self.store.count_pending(self.goal_id):
            status, reason = "waiting", f"Waiting on {pending} scrape{'' if pending == 1 else 's'}"
        elif await self.store.count_unreported(self.goal_id):
            status, reason = "waiting", "Scrapes finished; resuming"  # the watcher resumes the goal
        elif (ended := await self._time_limit_end()) is not None:
            status, reason = ended
        elif self._continuing is not None or await self._continue_toward_target():
            status, reason = "running", None
        else:
            status, reason = "done", None
        previous = await self.store.set_status(self.goal_id, status, reason)
        if previous != status:
            await self.recorder.record(self.goal_id, "status_changed", **{"from": previous, "to": status,
                                                                         "reason": reason})
        self._changed.set()

    # The target

    async def _continue_toward_target(self) -> bool:
        """The work stopped before the brief's target of fit roles. Send the main agent on, unless the last such
        message (since the user's last message) gave no new fit role. True when a message is on its way."""
        if self._capped or self._closing:
            return False
        goal = await self.store.get_goal(self.goal_id)
        target = (goal.get("brief") or {}).get("target_count")
        if goal["agent"] or goal.get("home_of") or not target or self.budget(goal) <= 0:
            return False
        if await self.store.finished_since_user(self.goal_id):
            return False  # the main agent decided to stop, and said why
        p = await self._artifacts().pipeline(self.goal_id)
        last = await self.store.last_continue_fit(self.goal_id)
        if p["fit"] >= target or (last is not None and p["fit"] <= last):
            return False
        text = (f"{p['fit']} of {target} roles fit so far ({p['jobs']} found, {p['researched']} researched, "
                f"{p['partial']} partial, {p['unsure']} unsure, {p['not_fit']} not a fit). Continue toward the "
                "target: find, research and check more roles. If more work will not help, call finish_goal with the "
                "reason, then report.")
        self._continuing = asyncio.create_task(self._continue_later(text, p["fit"]))
        return True

    async def _time_limit_end(self) -> tuple[str, str | None] | None:
        """The end status of a goal after its soft limit, when its work ended: done with its quota, else capped.
        None before the soft limit."""
        goal = await self.store.get_goal(self.goal_id)
        if goal is None or goal["soft_limit_at"] is None:
            return None
        target = (goal.get("brief") or {}).get("target_count")
        if target:
            fit = (await self._artifacts().pipeline(self.goal_id))["fit"]
            if fit >= target:
                return "done", None
        return "capped", TIME_REASON

    def _stopped_and_idle(self) -> bool:
        return self._fixed is not None and self._fixed[0] == "stopped" and self._pending_queries == 0

    async def _wake_wait(self) -> None:
        """No main response came after a subagent's notification: decide the status again."""
        try:
            await self._sleep(WAKE_S)
        finally:
            self._waking = None
        await self._refresh()

    async def _continue_later(self, text: str, fit: int) -> None:
        try:
            await self._sleep(CONTINUE_DELAY_S)
        finally:
            self._continuing = None
        if self.main_busy or self.tasks or self._fixed or self._capped or self._closing:
            await self._refresh()  # other work started: decide again when it stops
            return
        try:
            await self.send(text, "continue", user=False, fit=fit)
        except Exception as exc:
            log.warning("goal %s: could not continue toward the target: %s", self.goal_id, exc)
            await self._refresh()

    def running_tasks(self) -> list[dict]:
        now = time.monotonic()
        return [{**task, "elapsed_s": round(now - task["started"])} for task in self.tasks.values()]

    async def settled(self) -> dict:
        """Wait until the goal is not running and does not wait for scrapes. Return the goal."""
        while True:
            self._changed.clear()
            goal = await self.store.get_goal(self.goal_id)
            if goal["status"] in SETTLED and not self.main_busy and not self.tasks:
                return goal
            await self._changed.wait()
