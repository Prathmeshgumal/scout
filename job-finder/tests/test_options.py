from types import SimpleNamespace

from job_finder.agents import REGISTRY
from job_finder.guard import SubagentGuard
from job_finder.hooks import (
    GUARD_MATCHER,
    JOBS_MATCHER,
    MAIN_THREAD_MATCHER,
    PROFILE_MATCHER,
    QUEUE_MATCHER,
    SCRAPE_MATCHER,
)
from job_finder.options import build_hooks, build_options, retry_message
from job_finder.time_limit import TIME_MATCHER
from job_finder.tools import ToolContext


def goal(agent=None, **extra):
    return {"id": "00000000-0000-0000-0000-000000000001", "agent": agent, "max_fresh_scrapes": 15,
            "sdk_session_id": None, **extra}


def options_for(app_settings, g, **kwargs):
    ctx = ToolContext(g["id"], SimpleNamespace(pool=None), None, None)

    async def notify(text):
        return None

    hooks = build_hooks(g, None, notify, closed=False)
    return build_options(app_settings, g, ctx, hooks, budget=1.8, max_turns=40, resume="sess-1", **kwargs)


def options(app_settings, agent=None, profile=None, **kwargs):
    g = goal(agent, profile=profile)
    ctx = ToolContext(g["id"], SimpleNamespace(pool=None), None, None)

    async def notify(text):
        return None

    hooks = build_hooks(g, None, notify, closed=False, guard=SubagentGuard(lambda: []))
    return build_options(app_settings, g, ctx, hooks, budget=1.8, max_turns=40, resume="sess-1", **kwargs)


def test_main_agent_can_run_and_resume_background_subagents(app_settings):
    o = options(app_settings)
    assert o.tools == ["Agent", "SendMessage"]
    assert not {"WebSearch", "WebFetch"} & set(o.allowed_tools)
    assert {"Agent", "SendMessage", "mcp__artifacts__*", "mcp__artifacts_job_discovery__*"} <= set(o.allowed_tools)
    assert set(o.agents) == set(REGISTRY) and o.resume == "sess-1" and o.max_budget_usd == 1.8
    assert o.env["CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"] == "0"
    assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" not in o.env
    # Main waits long for subagents. Subagent calls are seconds apart. Also with an API key.
    assert o.env["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert o.env["CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL"] == "5m"
    assert [m.matcher for m in o.hooks["PreToolUse"]] == [SCRAPE_MATCHER, MAIN_THREAD_MATCHER, GUARD_MATCHER,
                                                       PROFILE_MATCHER, "Agent"]
    assert [m.matcher for m in o.hooks["PostToolUse"]] == [QUEUE_MATCHER]


def test_a_new_goal_can_create_a_mandate(app_settings):
    o = options_for(app_settings, goal())
    assert "mcp__mandates__*" in o.allowed_tools and "Work that repeats" in o.system_prompt
    assert [m.matcher for m in o.hooks["PreToolUse"]] == [SCRAPE_MATCHER, MAIN_THREAD_MATCHER, PROFILE_MATCHER, "Agent"]


def test_a_home_chat_has_the_mandate_tools_and_only_the_profile_subagent(app_settings):
    o = options_for(app_settings, goal(home_of="m-1"))
    assert o.tools == ["Agent"] and list(o.agents) == ["candidate-profile"]
    assert set(o.mcp_servers) == {"mandates", "career_lens", "artifacts_candidate_profile"}
    assert o.system_prompt.startswith("This chat manages a schedule in job-finder")


def test_a_run_has_the_normal_tools_and_no_mandate_tools(app_settings):
    o = options_for(app_settings, goal(mandate_id="m-1"))
    assert o.tools == ["Agent", "SendMessage"] and "mandates" not in o.mcp_servers
    assert not any(name.startswith("mcp__mandates") for name in o.allowed_tools)
    assert "This mission was started by a schedule" in o.system_prompt
    assert [m.matcher for m in o.hooks["PreToolUse"]] == [SCRAPE_MATCHER, MAIN_THREAD_MATCHER, PROFILE_MATCHER,
                                                       JOBS_MATCHER, TIME_MATCHER]


def test_the_main_server_has_only_the_main_tools():
    from job_finder.tools import artifacts
    ctx = ToolContext("g", SimpleNamespace(pool=None), None, None)
    names = {t.name for t in artifacts.build_main_tools(ctx)}
    assert names == artifacts.MAIN_TOOLS  # a typo in MAIN_TOOLS fails here
    assert names < {t.name for t in artifacts.build_tools(ctx, "main")}
    assert not names & {"save_profile", "save_fit", "save_jobs", "save_notes", "get_brief", "get_search_history"}


def test_foreground_flag_disables_background_tasks(app_settings):
    assert options(app_settings, background=False).env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_subagent_goal_options_do_not_change(app_settings):
    o = options(app_settings, agent="job-research")
    spec = REGISTRY["job-research"]
    assert o.agents is None and o.system_prompt == spec.prompt and o.allowed_tools == list(spec.tools)
    assert o.tools == spec.builtin_tools()
    assert "SendMessage" not in o.tools
    assert [m.matcher for m in o.hooks["PreToolUse"]] == [SCRAPE_MATCHER]


def test_retry_message_depends_on_how_the_goal_ended():
    failed = {"sdk_session_id": "s", "status": "failed", "status_reason": "Claude API error (overloaded).",
              "title": "t"}
    assert retry_message(failed, "Find X") == (
        "The previous attempt failed (Claude API error (overloaded).). Continue working toward the mission.")
    stopped = {**failed, "status": "stopped"}
    assert retry_message(stopped, "Find X").startswith("The user stopped you earlier")
    assert retry_message({**failed, "sdk_session_id": None}, "Find X") == "Find X"


def test_claude_code_gets_only_the_chosen_credential(app_settings):
    from dataclasses import replace
    token_only = replace(app_settings, anthropic_api_key="", claude_oauth_token="sk-ant-oat01-t")
    env = options(token_only).env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-t" and "ANTHROPIC_API_KEY" not in env
    both = replace(app_settings, anthropic_api_key="sk-ant-k", claude_oauth_token="sk-ant-oat01-t")
    env = options(both).env
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-k" and "CLAUDE_CODE_OAUTH_TOKEN" not in env


def test_a_goal_uses_its_own_model_and_effort_for_all_its_agents(app_settings):
    default = options_for(app_settings, goal())
    assert (default.model, default.effort) == (app_settings.model, app_settings.effort)  # the .env default
    chosen = options_for(app_settings, goal(model="claude-opus-5-5", effort="high"))
    assert (chosen.model, chosen.effort) == ("claude-opus-5-5", "high")
    assert {d.effort for d in chosen.agents.values()} == {"high"}  # the subagents work with the same effort
    assert {d.model for d in chosen.agents.values()} == {None}  # None: the model of the main agent


# Tools that reach past job-finder's own tools: the shell, files, the open web and other agents.
GENERAL_TOOLS = {"Bash", "BashOutput", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Glob", "Grep", "LS",
                 "WebFetch", "WebSearch", "Task", "TodoWrite", "KillShell", "Skill", "ExitPlanMode"}


def tool_names(o) -> set[str]:
    names = set(o.tools or []) | set(o.allowed_tools or [])
    for agent in (o.agents or {}).values():
        names |= set(agent.tools or [])
    return names


def test_no_agent_gets_a_general_tool(app_settings):
    """Agents use only job-finder's tools. A general tool here is a way past the site policy."""
    for g in (goal(), goal(home_of="m-1"), goal(mandate_id="m-1"), *(goal(name) for name in REGISTRY)):
        o = options_for(app_settings, g)
        assert not tool_names(o) & GENERAL_TOOLS, g
        assert all(name in ("Agent", "SendMessage") or name.startswith("mcp__") for name in tool_names(o)), g
        assert o.setting_sources == [] and o.strict_mcp_config is True  # no tools from settings or other servers


def test_every_agent_prompt_has_the_scope_rules(app_settings):
    from job_finder.agents.spec import SCOPE
    assert SCOPE.startswith("Scope and safety:")
    for g in (goal(), goal(home_of="m-1"), goal(mandate_id="m-1")):
        assert SCOPE in options_for(app_settings, g).system_prompt
    for spec in REGISTRY.values():
        assert SCOPE in spec.prompt, spec.name
