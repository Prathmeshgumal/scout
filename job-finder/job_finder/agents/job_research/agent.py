"""job-research: finds the fullest job text and the company facts for each role, and saves them."""

from pathlib import Path

from ..spec import READ_TOOLS, SubagentSpec, load_prompt

SPEC = SubagentSpec(
    name="job-research",
    description=("Finds the fullest description that it can read for each role, and the company facts, and saves them. "
                 "It finds the roles that need research itself, so do not give it role ids. Give the number of roles "
                 "to research, for example 5."),
    prompt=load_prompt(Path(__file__).parent),
    tools=(
        *READ_TOOLS,
        "mcp__artifacts__save_job_research",
        "mcp__artifacts__save_company",
        "mcp__artifacts__save_notes",
        "mcp__career_lens__google_search",
        "mcp__career_lens__read_page",
        "mcp__career_lens__get_company",
        "mcp__career_lens__scrape_company",
        "mcp__career_lens__wait_for_scrapes",
        "mcp__career_lens__get_linkedin_job",
        "mcp__career_lens__scrape_linkedin_job",
    ),
    max_turns=40,
)
