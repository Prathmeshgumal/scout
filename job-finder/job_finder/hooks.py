"""Hooks the SDK calls during a run: the fresh-scrape cap, the main-thread tool block, the scrape owner, the
one-run-of-each-subagent guard and the profile-first rule."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from .agents.candidate_profile.agent import SPEC as PROFILE_SPEC
from .guard import SubagentGuard
from .store import Store
from .tools.career_lens import SERVER_NAME

# The calls that ask for a fresh LinkedIn read: each one counts to the goal's fresh-scrape cap.
SCRAPE_MATCHER = f"mcp__{SERVER_NAME}__(scrape_company|scrape_linkedin_job|search_linkedin_jobs)"
CLOSED_REASON = "This goal has reached its limits, so no more scrapes. Answer from the data you have."
# Tools that only subagents may use. The main agent gives this work to a subagent.
MAIN_THREAD_MATCHER = f"mcp__{SERVER_NAME}__.*"
MAIN_THREAD_REASON = "Give career-lens and web work to a subagent. The main agent does not use these tools."
# Tools that can queue a request in career-lens. Their reply says "queued as <request id>."
QUEUE_MATCHER = f"mcp__{SERVER_NAME}__(scrape_company|scrape_linkedin_job|search_linkedin_jobs|google_search)"
QUEUED = re.compile(r"queued as (\S+?)\.(?=\s|$)")

# Calls that start or resume a subagent. One run of each subagent type works in a goal at a time.
GUARD_MATCHER = "Agent|Task|SendMessage"

Hook = Callable[[dict, str | None, Any], Awaitable[dict]]


def requested_scrapes(tool_input: dict) -> int:
    return 1 if any(str(tool_input.get(key) or "").strip() for key in ("linkedin_url", "url", "keywords")) else 0


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def scrape_cap_hook(store: Store, goal_id: str, cap: int, notify: Callable[[str], Awaitable[Any]], *,
                    closed: bool = False) -> Hook:
    """PreToolUse: refuse a scrape call that would take the goal past its fresh-scrape cap."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        if closed:
            return _deny(CLOSED_REASON)
        used = (await store.usage(goal_id))["fresh_scrapes"]
        if used + requested_scrapes(hook_input.get("tool_input") or {}) <= cap:
            return {}
        reason = (f"Scrape cap reached ({used}/{cap}). Use get_company or get_linkedin_job for saved data, "
                  "or finish with what you have.")
        await notify(reason)
        return _deny(reason)

    return hook


def main_thread_hook() -> Hook:
    """PreToolUse: refuse career-lens and web tools on the main thread. Subagent calls carry an agent_id."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        if hook_input.get("agent_id"):
            return {}
        return _deny(MAIN_THREAD_REASON)

    return hook


def busy_reason(name: str) -> str:
    return (f"A {name} run is already working on this goal. It reports when it finishes. "
            f"Start the next {name} run then.")


def subagent_guard_hook(guard: SubagentGuard, store: Store, goal_id: str) -> Hook:
    """PreToolUse: deny a start or a resume of a subagent type that already has a run in this goal."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        if hook_input.get("agent_id"):
            return {}
        tool_input = hook_input.get("tool_input") or {}
        if hook_input.get("tool_name") == "SendMessage":
            name = await store.task_name(goal_id, str(tool_input.get("to") or ""))
        else:
            name = tool_input.get("subagent_type")
        if not name or guard.try_start(str(name), tool_use_id or ""):
            return {}
        return _deny(busy_reason(str(name)))

    return hook


PROFILE = PROFILE_SPEC.name
PROFILE_MATCHER = "Agent|Task"
PROFILE_REASON = ("Build the candidate profile first with the candidate-profile subagent. Then start the other "
                  "subagents.")


def profile_first_hook(store: Store, goal_id: str) -> Hook:
    """PreToolUse on Agent: until the candidate profile of the goal is ready, only its subagent can start."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        name = (hook_input.get("tool_input") or {}).get("subagent_type")
        if hook_input.get("agent_id") or name is None or name == PROFILE:
            return {}
        goal = await store.get_goal(goal_id) or {}
        return {} if goal.get("profile_ready") else _deny(PROFILE_REASON)

    return hook


HOME_AGENT_REASON = ("This chat now manages a mandate. It does no job search. The goals that the mandate "
                     "starts do the work.")


def home_agent_hook(store: Store, goal_id: str) -> Hook:
    """PreToolUse on Agent: refuse it once create_mandate made this goal a home chat, in the same session. The
    candidate-profile subagent can still run: the chat of a mandate builds a new profile with it."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        if (hook_input.get("tool_input") or {}).get("subagent_type") == PROFILE:
            return {}
        goal = await store.get_goal(goal_id) or {}
        return _deny(HOME_AGENT_REASON) if goal.get("home_of") else {}

    return hook


# The save tool of every artifacts server: the main agent's and each subagent's own.
JOBS_MATCHER = r"mcp__artifacts\w*__save_jobs"
CEILING_FACTOR = 4  # many roles do not fit, and the target counts only fit roles
CEILING_REASON = "This goal has enough roles. Research and check the roles that you have."


def job_ceiling_hook(store: Store, goal_id: str, notify: Callable[[str], Awaitable[Any]]) -> Hook:
    """PreToolUse: refuse save_jobs when a run has CEILING_FACTOR times its quota of roles. A hook allows or
    refuses the whole call, so the last allowed batch can go a little past the ceiling."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        goal = await store.get_goal(goal_id) or {}
        quota = (goal.get("brief") or {}).get("target_count")
        if not quota or await store.count_jobs(goal_id) < CEILING_FACTOR * int(quota):
            return {}
        await notify(CEILING_REASON)
        return _deny(CEILING_REASON)

    return hook


def response_text(response: Any) -> str:
    """The text of a tool reply in a PostToolUse hook: content blocks, a dict with content, or a string."""
    if isinstance(response, dict):
        response = response.get("content")
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return "\n".join(block.get("text", "") for block in response if isinstance(block, dict))
    return ""


def scrape_agent_hook(store: Store, goal_id: str) -> Hook:
    """PostToolUse: write the subagent's agent_id on the scrapes it queued, so the result goes back to it."""

    async def hook(hook_input: dict, tool_use_id: str | None, context: Any) -> dict:
        agent_id = hook_input.get("agent_id")
        request_ids = QUEUED.findall(response_text(hook_input.get("tool_response")))
        if agent_id and request_ids:
            await store.set_scrape_agent(goal_id, request_ids, agent_id)
        return {}

    return hook
