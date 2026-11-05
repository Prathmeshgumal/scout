"""Turn Claude Agent SDK messages into goal events."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    TERMINAL_TASK_STATUSES,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from .tools.career_lens import failure_kind


@dataclass
class Emit:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    stored: bool = True
    detail: dict[str, Any] | None = None  # the input of a search or a page read: stored, not pushed live


def _count(items: Any) -> str:
    """The number of items, e.g. ": 2 items". Empty when the list is not readable. It can come as a JSON string."""
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except ValueError:
            return ""
    return f": {len(items)} item{'' if len(items) == 1 else 's'}" if isinstance(items, list) else ""


def _rejected(items: Any) -> str:
    """", 2 rejected" when job-discovery also saves the roles that it rejected."""
    return f", {len(items)} rejected" if isinstance(items, list) and items else ""


LABELS = {
    "google_search": lambda i: f"Searching Google: {i.get('query', '')}",
    "read_page": lambda i: f"Reading {i.get('url', '')}",
    "get_company": lambda i: f"Reading the saved company: {i.get('linkedin_url', '')}",
    "scrape_company": lambda i: f"Requesting a fresh company scrape: {i.get('linkedin_url', '')}",
    "search_linkedin_jobs": lambda i: f"Searching LinkedIn jobs: {i.get('keywords', '')}"
                                      + (f" in {i['location']}" if i.get("location") else ""),
    "get_job_search": lambda i: "Reading the LinkedIn job search results",
    "get_linkedin_job": lambda i: f"Reading the saved LinkedIn job: {i.get('url', '')}",
    "scrape_linkedin_job": lambda i: f"Requesting a LinkedIn job read: {i.get('url', '')}",
    "SendMessage": lambda i: "Resuming a subagent",
    "set_brief": lambda i: f"Saving the brief: {i.get('target_count', '?')} "
                           f"role{'' if str(i.get('target_count')) == '1' else 's'}",
    "save_profile": lambda i: "Saving the candidate profile",
    "get_brief": lambda i: "Reading the brief",
    "list_files": lambda i: "Listing the files",
    "read_file": lambda i: "Reading a file",
    "get_pipeline": lambda i: "Counting the roles at each stage",
    "get_search_history": lambda i: "Reading the earlier searches and refused roles",
    "finish_goal": lambda i: f"Finishing the mission: {i.get('reason', '')}",
    "list_jobs": lambda i: f"Listing the roles: {i.get('filter') or 'all'}",
    "read_job": lambda i: "Reading everything saved for a role",
    "save_jobs": lambda i: f"Saving roles{_count(i.get('jobs'))}{_rejected(i.get('rejected'))}",
    "save_job_research": lambda i: f"Saving what research found: {i.get('evidence_level', '')}",
    "save_company": lambda i: "Saving company facts",
    "save_fit": lambda i: f"Saving a verdict: {i.get('verdict', '')}",
    "save_application_note": lambda i: "Saving an application note",
    "save_notes": lambda i: f"Saving gaps and notes for the report{_count(i.get('items'))}",
    "wait_for_scrapes": lambda i: f"Waiting for scrapes: {', '.join(map(str, i.get('request_ids') or []))}",
}


AGENT_TOOLS = {"Agent", "Task"}  # "Task" in the system:init list and in older SDKs
# The first block of a save tool's reply says what it saved (see tools.artifacts.saved).
SAVE_TOOLS = {"set_brief", "save_profile", "save_jobs", "save_job_research", "save_company", "save_fit",
              "save_application_note", "save_notes", "finish_goal"}
# The events keep the input of these tools only: get_search_history reads it. Other inputs can hold resume text.
WEB_TOOLS = {"google_search", "read_page"}
# The tools whose input and output the Activity tab shows. They hold web data, roles and companies only. A tool
# that is not here keeps nothing, so a new tool keeps nothing until someone checks it.
DETAIL_TOOLS = {"google_search", "read_page", "search_linkedin_jobs", "get_job_search", "scrape_company",
                "get_company", "scrape_linkedin_job", "get_linkedin_job", "read_job", "wait_for_scrapes",
                "list_jobs", "read_goal_jobs", "save_jobs", "save_job_research", "save_company", "save_fit",
                "get_search_history", "list_files", "list_goals", "list_runs"}
# Tools that can hold resume text or the user's own words. The Activity tab says why it shows nothing for them.
PRIVATE_TOOLS = {"read_file", "save_profile", "save_notes", "set_brief", "get_brief", "save_application_note",
                 "propose_mandate", "confirm_mandate", "get_mandate", "finish_goal", "get_pipeline", "SendMessage"}
MAX_DETAIL = 20_000  # the longest input or output that a step keeps, in characters
# A career-lens request that a call queued, e.g. "queued as rq-1". The log shows its result with the call.
QUEUED = re.compile(r"queued as (rq-[A-Za-z0-9_-]+)")


def _input_detail(tool: str, tool_input: dict) -> dict | None:
    if tool not in DETAIL_TOOLS:
        return None
    text = json.dumps(tool_input, ensure_ascii=False)
    return {"input": text[:MAX_DETAIL], "truncated": True} if len(text) > MAX_DETAIL else {"input": tool_input}


def _output_detail(tool: str, content: Any) -> dict | None:
    if tool not in DETAIL_TOOLS:
        return None
    text = _joined(content)
    return {"output": text[:MAX_DETAIL], "truncated": True} if len(text) > MAX_DETAIL else {"output": text}


def short_name(name: str) -> str:
    return name.split("__")[-1]


def tool_label(name: str, tool_input: dict) -> str:
    short = short_name(name)
    label = LABELS.get(short)
    return label(tool_input) if label else short


def _joined(content: Any) -> str:
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return str(content or "")


def _first_block(content: Any) -> str:
    if isinstance(content, list):
        content = next((block.get("text", "") for block in content if isinstance(block, dict)), "")
    return str(content or "").strip()


def _first_line(content: Any) -> str:
    """One line about a tool result. For JSON, its size, e.g. "3 items"; for text, its first line."""
    text = _joined(content).strip()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, list):
        return f"{len(data)} item" + ("" if len(data) == 1 else "s")
    if isinstance(data, dict):
        lists = any(isinstance(v, list) and v for v in data.values())
        return summary_line(text) if lists else ", ".join(list(data)[:4])[:160]
    return text.splitlines()[0][:160] if text else ""


def _report_line(content: Any) -> str:
    """The first line of a subagent's report. The SDK frames the report: a hand-back note at column zero,
    the report with each line indented by two spaces, then agentId and usage lines."""
    lines = _joined(content).splitlines()
    indented = [line.strip() for line in lines if line.startswith("  ") and line.strip()]
    if indented:
        return indented[0][:160]
    plain = [line.strip() for line in lines if line.strip() and not line.startswith("[Subagent hand-back]")]
    return plain[0][:160] if plain else ""


def task_status(message: Any) -> str | None:
    """Our terminal status for a task message (completed, failed or stopped), or None while the task runs."""
    if isinstance(message, TaskNotificationMessage):
        raw = message.status
    elif isinstance(message, TaskUpdatedMessage):
        raw = message.status or (message.patch or {}).get("status")
    else:
        return None
    if raw not in TERMINAL_TASK_STATUSES:
        return None
    return "stopped" if raw == "killed" else raw


MAX_REPORT = 20000  # characters of a subagent report that the event keeps


def summary_line(report: str) -> str:
    """One line about a report. For a JSON report, its summary, or else the size of each list in it,
    e.g. "2 saved, 1 missing"."""
    try:
        data = json.loads(report)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("summary"), str) and data["summary"].strip():
            return data["summary"].strip().splitlines()[0][:160]
        counts = [f"{len(value)} {key.replace('_', ' ')}" for key, value in data.items()
                  if isinstance(value, list) and value]
        if counts:
            return ", ".join(counts)
    return report.splitlines()[0][:160] if report else ""


def _report(content: Any) -> str:
    """The whole report of a subagent, without the SDK frame (see _report_line)."""
    if isinstance(content, list):
        content = "\n".join(block.get("text", "") for block in content if isinstance(block, dict))
    lines = str(content or "").splitlines()
    marks = [i for i, line in enumerate(lines) if line.startswith("  ") and line.strip()]
    if marks:
        body = [line[2:] if line.startswith("  ") else line for line in lines[marks[0]:marks[-1] + 1]]
    else:
        frame = ("[Subagent hand-back]", "agentId:", "<usage>", "subagent_tokens:", "tool_uses:", "duration_ms:")
        body = [line for line in lines if not line.strip().startswith(frame)]
    return "\n".join(body).strip()[:MAX_REPORT]


def _task(tool_input: dict) -> str:
    """The short description of a subagent call. Not its prompt: that can hold resume text."""
    text = str(tool_input.get("description") or "").strip()
    return text.splitlines()[0][:160] if text else ""


class Translator:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_text = ""  # the latest top-level assistant text block
        self._tools: dict[str, tuple[str, str]] = {}  # tool_use_id -> (tool, agent that called it)
        self._subagents: dict[str, str] = {}  # Agent tool_use_id -> subagent name
        self._parents: dict[str, str] = {}  # tool_use_id of a subagent's tool call -> the Agent call of that subagent
        self._task_names: dict[str, str] = {}  # task_id (= agentId) -> subagent name
        self._task_calls: dict[str, str] = {}  # task_id -> the tool_use_id that started or resumed it
        self._first_calls: dict[str, str] = {}  # task_id -> the Agent call that started it first
        self._finished: set[str] = set()  # task ids already reported as finished
        # A task ends with TaskUpdated, then usually TaskNotification with the summary. Hold the first one
        # until the next message, so that the event can carry the summary.
        self._pending: dict[str, str] = {}  # task_id -> terminal status

    def seed(self, tasks: list[dict]) -> None:
        """Learn the subagent tasks of earlier sessions, so that a resumed subagent keeps its name."""
        for task in tasks:
            self._task_names[task["task_id"]] = task["name"]
            if task["status"] in ("completed", "failed", "stopped"):
                self._finished.add(task["task_id"])  # a late notification after a reopen is not shown again
            if task.get("tool_use_id"):
                self._subagents[task["tool_use_id"]] = task["name"]
                self._first_calls[task["task_id"]] = task["tool_use_id"]

    def task_name(self, task_id: str) -> str:
        return self._task_names.get(task_id, "subagent")

    def _finish(self, task_id: str, status: str, summary: str) -> Emit:
        self._finished.add(task_id)
        self._pending.pop(task_id, None)
        call, first = self._task_calls.get(task_id), self._first_calls.get(task_id)
        # A resumed task finishes its SendMessage call. call_id names the agent call that it continues.
        resumed = {"call_id": first} if first and first != call else {}
        return Emit("agent_finished", {"tool_use_id": call, "agent": "main", "name": self.task_name(task_id),
                                       "ok": status == "completed", "status": status, "summary": summary,
                                       **resumed})

    def _flush(self, keep: str | None = None) -> list[Emit]:
        return [self._finish(task_id, status, "") for task_id, status in list(self._pending.items())
                if task_id != keep]

    def _agent(self, parent_tool_use_id: str | None) -> str:
        if parent_tool_use_id is None:
            return "main"
        return self._subagents.get(parent_tool_use_id, "subagent")

    def translate(self, message: Any) -> list[Emit]:
        keep = message.task_id if isinstance(message, TaskNotificationMessage) else None
        return self._flush(keep) + self._translate(message)

    def _translate(self, message: Any) -> list[Emit]:
        if isinstance(message, TaskStartedMessage):
            name = self._subagents.get(message.tool_use_id or "") or self.task_name(message.task_id)
            self._task_names[message.task_id] = name
            self._task_calls[message.task_id] = message.tool_use_id or self._task_calls.get(message.task_id, "")
            if message.tool_use_id:
                self._first_calls.setdefault(message.task_id, message.tool_use_id)
            self._finished.discard(message.task_id)  # a resumed task can finish again
            return []
        status = task_status(message)
        if status is not None:
            if message.task_id in self._finished:
                return []
            if isinstance(message, TaskNotificationMessage):
                # For a background subagent, the notification's summary is its whole report.
                # Keep one line only: a report can repeat resume text.
                return [self._finish(message.task_id, status, summary_line(message.summary or ""))]
            self._pending[message.task_id] = status
            return []
        if isinstance(message, StreamEvent):
            event = message.event
            delta = event.get("delta") or {}
            if message.parent_tool_use_id:  # a subagent types: only the main agent's typing is shown live
                return []
            if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                return [Emit("text_delta", {"text": delta["text"]}, stored=False)]
            return []
        if isinstance(message, SystemMessage):
            if message.subtype == "init" and message.data.get("session_id"):
                self.session_id = message.data["session_id"]
            return []
        if isinstance(message, ResultMessage):
            self.session_id = message.session_id or self.session_id
            return []
        if isinstance(message, AssistantMessage):
            if message.error:  # e.g. "Not logged in": the session reports it, it is not an answer
                return []
            agent = self._agent(message.parent_tool_use_id)
            # The Agent call that started this subagent. Two subagents of one kind can run at the same time.
            call = {"call_id": message.parent_tool_use_id} if message.parent_tool_use_id else {}
            out = []
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    if agent == "main":  # the narration of a subagent is not kept: it can repeat resume text
                        self.last_text = block.text
                        out.append(Emit("text", {"text": block.text, "agent": agent}))
                elif isinstance(block, ToolUseBlock) and block.name in AGENT_TOOLS:
                    name = str(block.input.get("subagent_type") or "subagent")
                    self._subagents[block.id] = name
                    self._tools[block.id] = (name, agent)
                    out.append(Emit("agent_started", {"tool_use_id": block.id, "agent": agent, "name": name,
                                                      "task": _task(block.input)}))
                elif isinstance(block, ToolUseBlock):
                    short = short_name(block.name)
                    self._tools[block.id] = (short, agent)
                    if call:
                        self._parents[block.id] = call["call_id"]
                    resumed = self._task_names.get(str(block.input.get("to"))) if block.name == "SendMessage" else None
                    out.append(Emit("tool_started", {"tool_use_id": block.id, "tool": short,
                                                     "label": f"Resuming {resumed}" if resumed
                                                     else tool_label(block.name, block.input),
                                                     "agent": agent, **call},
                                    detail=_input_detail(short, block.input)))
            return out
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            out = []
            for block in message.content:
                if not isinstance(block, ToolResultBlock) or block.tool_use_id not in self._tools:
                    continue
                tool, agent = self._tools[block.tool_use_id]
                data = {"tool_use_id": block.tool_use_id, "agent": agent, "ok": not block.is_error,
                        "summary": _first_line(block.content)}
                if block.tool_use_id in self._subagents:
                    if "Async agent launched" in _joined(block.content):
                        continue  # a background start: its task messages report the end
                    # A foreground call: its result is the report. Its task messages give one finish only.
                    tasks = [t for t, call in self._task_calls.items() if call == block.tool_use_id]
                    if tasks and all(t in self._finished for t in tasks):
                        continue  # the task's notification came first and was shown
                    self._finished.update(tasks)
                    for task in tasks:
                        self._pending.pop(task, None)
                    data["summary"] = summary_line(_report(block.content)) or _report_line(block.content)
                    out.append(Emit("agent_finished", {**data, "name": tool}))
                else:
                    if parent := self._parents.pop(block.tool_use_id, None):
                        data["call_id"] = parent
                    if tool in SAVE_TOOLS and not block.is_error:
                        data["saved"] = data["summary"] = _first_block(block.content)
                    if request_ids := QUEUED.findall(_joined(block.content)):
                        data["request_ids"] = request_ids
                    # Why a career-lens request failed, e.g. "not_found": the report keeps a missing source apart.
                    if block.is_error and (kind := failure_kind(tool, _joined(block.content))):
                        data["failure_kind"] = kind
                    out.append(Emit("tool_finished", {**data, "tool": tool}, detail=_output_detail(tool, block.content)))
            return out
        return []
