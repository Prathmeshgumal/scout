"""Agent SDK options for a goal: prompts, tool servers, hooks and SDK settings."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from .agents import REGISTRY, SubagentSpec
from .agents.candidate_profile.agent import SPEC as PROFILE
from .agents.spec import SCOPE
from .config import Settings, claude_env
from .guard import SubagentGuard
from .hooks import (
    GUARD_MATCHER,
    JOBS_MATCHER,
    MAIN_THREAD_MATCHER,
    PROFILE_MATCHER,
    QUEUE_MATCHER,
    SCRAPE_MATCHER,
    home_agent_hook,
    job_ceiling_hook,
    main_thread_hook,
    profile_first_hook,
    scrape_agent_hook,
    scrape_cap_hook,
    subagent_guard_hook,
)
from .store import Store
from .time_limit import TIME_MATCHER, time_limit_hook
from .tools import ToolContext
from .tools import artifacts as artifact_tools
from .tools import career_lens as career_lens_tools
from .tools import mandates as mandate_tools

MAIN_PROMPT = (Path(__file__).parent / "main_agent.md").read_text()
HOME_PROMPT = (Path(__file__).parent / "mandate_home.md").read_text()
RUN_SECTION = (Path(__file__).parent / "mandate_run.md").read_text()
# Agent SDK settings for every session: only our subagents, and no nesting. A long MCP tool call stays a
# blocking call inside its subagent; the subagent itself can run in the background.
# Prompt cache lifetime: main waits long for subagents, so it keeps 1h. Subagent calls are seconds apart, so
# 5m is cheaper for them. Set both: an API key or usage credits use 5m for main by default.
SUBAGENT_ENV = {"CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
                "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS": "0",
                "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h", "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "5m"}
MAIN_BUILTIN_TOOLS = ["Agent", "SendMessage"]
STOPPED_PROMPT = "The user stopped you earlier and has now asked you to continue. Continue working toward the goal."


def goal_kind(goal: dict) -> str:
    """run: one run of a mandate. home: the home chat of a mandate. goal: a normal goal."""
    if goal.get("mandate_id"):
        return "run"
    return "home" if goal.get("home_of") else "goal"


def main_prompt(goal_id: str, kind: str = "goal") -> str:
    """The prompt of the main agent. It does not hold the candidate profile: only two subagents read that."""
    parts = [MAIN_PROMPT.rstrip(), SCOPE, f"The id of this goal is {goal_id}."]
    if kind == "run":
        parts.append(RUN_SECTION)
    return "\n\n".join(parts)


def prompt_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def home_prompt(goal_id: str) -> str:
    return f"{HOME_PROMPT.rstrip()}\n\n{SCOPE}\n\nThe id of this goal is {goal_id}."


def subagent_spec(goal: dict) -> SubagentSpec | None:
    """The subagent that drives this goal alone (job-finder agent), or None for the main agent."""
    name = goal.get("agent")
    if not name:
        return None
    if name not in REGISTRY:
        raise ValueError(f"Unknown subagent {name!r}")
    return REGISTRY[name]


def build_servers(ctx: ToolContext, spec: SubagentSpec | None, kind: str = "goal") -> dict:
    if kind == "home":  # the chat of a mandate prepares a new profile with its subagent
        return {mandate_tools.SERVER_NAME: mandate_tools.build_server(ctx, "home"),
                career_lens_tools.SERVER_NAME: career_lens_tools.build_server(ctx),
                PROFILE.server: artifact_tools.build_server(ctx, PROFILE.name, name=PROFILE.server)}
    own = artifact_tools.build_server(ctx, spec.name) if spec else artifact_tools.build_main_server(ctx)
    servers = {career_lens_tools.SERVER_NAME: career_lens_tools.build_server(ctx), artifact_tools.SERVER_NAME: own}
    if spec is None:  # each subagent saves through its own server, so its rows record its name
        for sub in REGISTRY.values():
            servers[sub.server] = artifact_tools.build_server(ctx, sub.name, name=sub.server)
        if kind == "goal":
            servers[mandate_tools.SERVER_NAME] = mandate_tools.build_server(ctx, "goal")
    return servers


def build_hooks(goal: dict, store: Store, notify: Callable[[str], Awaitable[Any]], *, closed: bool,
                guard: SubagentGuard | None = None) -> dict:
    cap = scrape_cap_hook(store, goal["id"], goal["max_fresh_scrapes"], notify, closed=closed)
    pre = [HookMatcher(matcher=SCRAPE_MATCHER, hooks=[cap])]
    if not goal["agent"]:
        pre.append(HookMatcher(matcher=MAIN_THREAD_MATCHER, hooks=[main_thread_hook()]))
        if guard is not None:
            pre.append(HookMatcher(matcher=GUARD_MATCHER, hooks=[subagent_guard_hook(guard, store, goal["id"])]))
        pre.append(HookMatcher(matcher=PROFILE_MATCHER, hooks=[profile_first_hook(store, goal["id"])]))
        if goal_kind(goal) == "goal":  # create_mandate can make it a home chat during this session
            pre.append(HookMatcher(matcher="Agent", hooks=[home_agent_hook(store, goal["id"])]))
    if goal_kind(goal) == "run":
        pre.append(HookMatcher(matcher=JOBS_MATCHER, hooks=[job_ceiling_hook(store, goal["id"], notify)]))
        pre.append(HookMatcher(matcher=TIME_MATCHER, hooks=[time_limit_hook(store, goal["id"])]))
    post = [HookMatcher(matcher=QUEUE_MATCHER, hooks=[scrape_agent_hook(store, goal["id"])])]
    return {"PreToolUse": pre, "PostToolUse": post}


def session_exists(config_dir: Path, session_id: str) -> bool:
    return any(config_dir.glob(f"projects/*/{session_id}.jsonl"))


def remove_transcripts(config_dir: Path, session_ids: list[str]) -> None:
    """Delete the SDK's files of these sessions: the conversation, and the folder of its subagents."""
    for session_id in session_ids:
        try:
            uuid.UUID(session_id)  # a session id is a UUID; anything else must not become a path
        except ValueError:
            continue
        for project in config_dir.glob("projects/*"):
            (project / f"{session_id}.jsonl").unlink(missing_ok=True)
            shutil.rmtree(project / session_id, ignore_errors=True)


def retry_message(goal: dict, first_message: str | None) -> str:
    """What to tell Claude when the user retries a failed or stopped goal."""
    if not goal["sdk_session_id"]:
        return first_message or goal["title"]
    if goal["status"] == "stopped":
        return STOPPED_PROMPT
    return f"The previous attempt failed ({goal['status_reason'] or 'unknown error'}). Continue working toward the goal."


def build_options(settings: Settings, goal: dict, ctx: ToolContext, hooks: dict, *, budget: float, max_turns: int,
                  resume: str | None, background: bool = True) -> ClaudeAgentOptions:
    s = settings
    s.claude_config_dir.mkdir(parents=True, exist_ok=True)
    s.agent_cwd.mkdir(parents=True, exist_ok=True)
    spec = subagent_spec(goal)
    kind = goal_kind(goal) if spec is None else "goal"
    # The goal's choice, else .env. The API takes only an effort level that the SDK gives for the model.
    model, effort = goal.get("model") or s.model, goal.get("effort") or s.effort
    servers = build_servers(ctx, spec, kind)
    if spec is not None:
        prompt, tools, agents, allowed = spec.prompt, spec.builtin_tools(), None, list(spec.tools)
    elif kind == "home":
        prompt = home_prompt(goal["id"])
        tools, agents = ["Agent"], {PROFILE.name: PROFILE.definition()}
        # allowed covers the whole session, so subagents need their servers here. main_thread_hook stops the main agent.
        allowed = ["Agent", *(f"mcp__{name}__*" for name in servers)]
    else:
        prompt = main_prompt(goal["id"], kind)
        tools = list(MAIN_BUILTIN_TOOLS)
        agents = {sub.name: sub.definition(effort) for sub in REGISTRY.values()}
        allowed = ["Agent", "SendMessage", *(f"mcp__{name}__*" for name in servers)]
    env = {"CLAUDE_CONFIG_DIR": str(s.claude_config_dir), **claude_env(s), **SUBAGENT_ENV,
           "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": str(s.max_subagents)}
    if not background:
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    return ClaudeAgentOptions(
        system_prompt=prompt, model=model, effort=effort, mcp_servers=servers, tools=tools,
        allowed_tools=allowed, agents=agents, permission_mode="dontAsk", setting_sources=[],
        strict_mcp_config=True, hooks=hooks, resume=resume, max_turns=max_turns, max_budget_usd=budget,
        include_partial_messages=True, cwd=str(s.agent_cwd), env=env)
