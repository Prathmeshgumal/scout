from types import SimpleNamespace

from claude_agent_sdk import AgentDefinition

from job_finder.agents import REGISTRY
from job_finder.options import MAIN_PROMPT, main_prompt
from job_finder.tools import ToolContext, artifacts, career_lens

WEB_TOOLS = {"mcp__career_lens__google_search", "mcp__career_lens__read_page"}


def known_tools() -> set[str]:
    ctx = ToolContext("g", SimpleNamespace(pool=None), None, None)
    names = {f"mcp__{career_lens.SERVER_NAME}__{t.name}" for t in career_lens.build_tools(ctx)}
    names |= {f"mcp__{artifacts.SERVER_NAME}__{t.name}" for t in artifacts.build_tools(ctx, "x")}
    return names


def test_registry_has_the_five_subagents():
    assert set(REGISTRY) == {"candidate-profile", "job-discovery", "job-research", "fit-check", "application-prep"}


def test_every_tool_in_a_spec_exists():
    known = known_tools()
    for spec in REGISTRY.values():
        assert set(spec.tools) <= known, (spec.name, set(spec.tools) - known)


def test_every_subagent_can_save_notes():
    for name, spec in REGISTRY.items():
        assert "mcp__artifacts__save_notes" in spec.tools, name


def test_definition_scopes_artifact_tools_to_the_subagent():
    spec = REGISTRY["job-discovery"]
    definition = spec.definition("high")
    assert isinstance(definition, AgentDefinition)
    assert "mcp__artifacts_job_discovery__save_jobs" in definition.tools
    assert "mcp__career_lens__search_linkedin_jobs" in definition.tools
    assert not any(name.startswith("mcp__artifacts__") for name in definition.tools)
    assert spec.server == "artifacts_job_discovery" and spec.builtin_tools() == []
    assert definition.effort == "high"


def test_discovery_searches_linkedin_jobs_and_saves_roles():
    tools = set(REGISTRY["job-discovery"].tools)
    assert {"mcp__career_lens__search_linkedin_jobs", "mcp__career_lens__get_job_search",
            "mcp__artifacts__save_jobs", "mcp__artifacts__get_search_history"} <= tools
    assert "mcp__career_lens__get_linkedin_job" not in tools


def test_research_reads_full_postings_and_saves_research():
    tools = set(REGISTRY["job-research"].tools)
    assert {"mcp__career_lens__get_linkedin_job", "mcp__career_lens__scrape_linkedin_job",
            "mcp__artifacts__save_job_research", "mcp__artifacts__save_company"} <= tools
    assert "mcp__artifacts__save_jobs" not in tools


def test_only_two_subagents_use_the_web():
    for name, spec in REGISTRY.items():
        uses_web = bool({t for t in spec.tools if t.startswith("mcp__career_lens__")})
        assert uses_web == (name in {"job-discovery", "job-research"}), name
    assert WEB_TOOLS <= set(REGISTRY["job-discovery"].tools)


def test_only_candidate_profile_saves_the_profile():
    for name, spec in REGISTRY.items():
        assert ("mcp__artifacts__save_profile" in spec.tools) == (name == "candidate-profile"), name


def test_fit_check_has_the_four_verdicts_and_the_evidence_rule():
    spec = REGISTRY["fit-check"]
    assert "mcp__artifacts__save_fit" in spec.tools
    for verdict in ("`fit`", "`partial`", "`not_fit`", "`unsure`"):
        assert verdict in spec.prompt
    assert "`listing_only`" in spec.prompt and "closed posting is `not_fit`" in spec.prompt


def test_application_prep_invents_nothing_and_sends_nothing():
    spec = REGISTRY["application-prep"]
    assert "mcp__artifacts__save_application_note" in spec.tools
    assert "Do not invent" in spec.prompt and "sends nothing" in spec.description


def test_every_subagent_follows_the_writing_rules():
    for spec in REGISTRY.values():
        for rule in ("How to write text", 'Do not join parts of a text with ";"', '"7 Oct 2026"',
                     "Never repeat the resume text"):
            assert rule in spec.prompt, (spec.name, rule)


def test_no_subagent_uses_the_built_in_web_tools():
    for spec in REGISTRY.values():
        assert not {"WebSearch", "WebFetch"} & set(spec.tools), spec.name
        assert "WebSearch" not in spec.prompt and "WebFetch" not in spec.prompt, spec.name


def test_built_subagents_reply_with_the_shared_json_report():
    for name, spec in REGISTRY.items():
        for key in ('"summary"', '"pending_scrapes"'):
            assert key in spec.prompt, (name, key)


def test_subagents_find_their_own_roles():
    for name in ("job-research", "fit-check", "application-prep"):
        assert "do not give it role ids" in REGISTRY[name].description, name


def test_main_prompt_names_every_subagent_and_the_goal():
    for name in REGISTRY:
        assert name in MAIN_PROMPT, name
    assert main_prompt("g-1").endswith("The id of this goal is g-1.")
    assert "candidate profile" not in main_prompt("g-1").split("The id of this goal")[1]


def test_each_agent_scrapes_only_the_company_section_that_it_reads():
    assert 'linkedin_sections: ["jobs"]' in REGISTRY["job-discovery"].prompt
    assert 'linkedin_sections: ["about"]' in REGISTRY["job-research"].prompt
