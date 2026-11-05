"""The time limit of a mandate goal. See docs/superpowers/specs/2026-11-03-mandate-time-limit-design.md.

The limit counts run time: the time that the goal is running or waiting, as the report shows it.
- At the soft limit (time_limit_s), no new subagent and no new web work starts. The running subagents finish their
  task. Then the main agent reports, and the goal ends.
- At the hard limit (time_limit_s + grace_s), all work stops, and the main agent writes a final answer.
The scheduler checks the goals each minute. A goal that the user starts has no limit."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from .hooks import Hook, _deny
from .mandate_store import MandateStore
from .mandates import span
from .report import goal_timing
from .store import Store
from .tools.career_lens import SERVER_NAME

log = logging.getLogger(__name__)

TIME_REASON = "Time limit reached."
# The calls that start new work: a subagent run, a scrape, a search and a page read. SendMessage stays open: it
# resumes a subagent whose scrapes finished, so that it can finish its task. Reads of stored data stay open.
TIME_MATCHER = f"Agent|Task|mcp__{SERVER_NAME}__(scrape_company|scrape_linkedin_job|search_linkedin_jobs|google_search|read_page)"
DENY_REASON = "The time limit of this mission is reached. Start no new work. Finish with the data that you have."
TIME_LIMIT_PROMPT = ("The time limit of this mission is reached. Do not start subagents. The running subagents finish "
                     "their tasks. When they finish, call finish_goal with the reason, then report.")


def limit_phase(goal: dict, seconds: float) -> str | None:
    """"hard", "soft", or None: the step that the goal needs now. A goal with no time limit needs none."""
    limit = goal.get("time_limit_s")
    if limit is None:
        return None
    if seconds >= limit + (goal.get("grace_s") or 0):
        return "hard"
    if seconds >= limit and goal.get("soft_limit_at") is None:
        return "soft"
    return None


def past_hard_limit(goal: dict, seconds: float) -> bool:
    limit = goal.get("time_limit_s")
    return limit is not None and seconds >= limit + (goal.get("grace_s") or 0)


def soft_notice(goal: dict) -> str:
    return (f"Time limit reached ({span(goal['time_limit_s'] // 60)}). The running subagents finish their tasks. "
            "No new work starts.")


async def run_seconds(store: Store, goal: dict, now: datetime | None = None) -> float:
    """The run time of the goal, as the report shows it."""
    events = await store.status_events(goal["id"])
    ended_at = goal["updated_at"] if goal["status"] in ("done", "stopped", "failed", "capped") else None
    return goal_timing(goal["created_at"], events, now or datetime.now(UTC), ended_at)["work_seconds"]


def time_limit_hook(store: Store, goal_id: str) -> Hook:
    """PreToolUse: after the soft limit, refuse a call that starts new work."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        goal = await store.get_goal(goal_id) or {}
        return _deny(DENY_REASON) if goal.get("soft_limit_at") is not None else {}

    return hook


async def check_time_limits(store: Store, mandates: MandateStore, port) -> None:
    """Give each mandate goal at a limit its step. port.time_limit(goal_id, hard) does the step."""
    for goal in await mandates.timed_runs():
        try:
            phase = limit_phase(goal, await run_seconds(store, goal))
            if phase is not None:
                await port.time_limit(goal["id"], phase == "hard")
        except Exception:  # one goal must not stop the check of the others
            log.exception("mission %s: could not apply its time limit", goal["id"])


class ContinueError(ValueError):
    """The goal cannot continue now. The text tells the user why."""


async def continue_check(store: Store, mandates: MandateStore, goal: dict) -> dict | None:
    """Can a goal that stopped at its time limit continue now? None for any other goal. ok is True when the mandate
    gives more time than the goal used, no other goal of the mandate works, and the goal has spend left."""
    from .session import WRAP_UP_USD  # the session imports this module

    if goal["status"] != "capped" or goal["status_reason"] != TIME_REASON or not goal["mandate_id"]:
        return None
    m = await mandates.get(goal["mandate_id"])
    if m is None:
        return None
    seconds = await run_seconds(store, goal)
    mandate_limit = m["time_limit_minutes"] * 60
    busy = [run for run in await mandates.runs(m["id"])
            if run["id"] != goal["id"] and run["status"] in ("running", "waiting")]
    reason = None
    if busy:
        reason = f"Mission {busy[0]['number']} of this schedule is in progress. Continue this mission when it stops."
    elif mandate_limit <= seconds and m["status"] == "ended":
        reason = "The schedule has ended, so its time limit cannot change. This mission cannot continue."
    elif mandate_limit <= seconds:
        reason = (f"Raise the time limit in the schedule chat first. This mission worked for {span(int(seconds) // 60)}. "
                  f"The time limit is {span(m['time_limit_minutes'])}.")
    elif goal["max_spend_usd"] - goal["spend_usd"] - WRAP_UP_USD <= 0:
        reason = "This mission reached its spend cap, so it cannot continue."
    return {"ok": reason is None, "reason": reason, "run_s": round(seconds), "limit_s": goal["time_limit_s"],
            "mandate_limit_s": mandate_limit}


async def continue_goal(store: Store, mandates: MandateStore, goal_id: str) -> str:
    """Give a goal that stopped at its time limit the new limit of its mandate. Return the message for the main
    agent. ContinueError when the goal cannot continue."""
    goal = await store.get_goal(goal_id)
    check = None if goal is None else await continue_check(store, mandates, goal)
    if check is None:
        raise ContinueError("Only a mission that stopped at its time limit can continue.")
    if not check["ok"]:
        raise ContinueError(check["reason"])
    m = await mandates.get(goal["mandate_id"])
    await store.set_time_limit(goal_id, m["time_limit_minutes"] * 60, m["grace_minutes"] * 60)
    left = (check["mandate_limit_s"] - check["run_s"]) // 60
    return (f"The user gave this mission more time. Its time limit is now {span(m['time_limit_minutes'])}: about "
            f"{span(max(left, 1))} more. Subagents that worked at the time limit were stopped. Their saved work "
            "stays. Call get_pipeline, then continue toward the mission.")
