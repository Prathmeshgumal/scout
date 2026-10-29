"""job-discovery: finds job roles through LinkedIn job search, Google and the jobs sections of companies."""

from pathlib import Path

from ..spec import READ_TOOLS, SubagentSpec, load_prompt

SPEC = SubagentSpec(
    name="job-discovery",
    description=("Finds job roles through LinkedIn job search, Google and the LinkedIn jobs sections of companies, and saves "
                 "them. It reads "
                 "the brief, the profile without the resume, its earlier searches and the refused roles itself, so do "
                 "not give it lists of roles. Give the number of roles and, when useful, a direction, for example a "
                 "title, a place or a company. When fit-check rejects many roles for one reason, give the reason."),
    prompt=load_prompt(Path(__file__).parent),
    tools=(*READ_TOOLS, "mcp__artifacts__get_brief", "mcp__artifacts__save_jobs", "mcp__artifacts__save_notes",
           "mcp__artifacts__get_search_history", "mcp__career_lens__google_search", "mcp__career_lens__read_page",
           "mcp__career_lens__get_company", "mcp__career_lens__scrape_company", "mcp__career_lens__wait_for_scrapes",
           "mcp__career_lens__search_linkedin_jobs", "mcp__career_lens__get_job_search"),
    max_turns=40,
)
