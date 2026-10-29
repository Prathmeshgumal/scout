"""application-prep: writes a short application note for each fit role. It sends nothing."""

from pathlib import Path

from ..spec import READ_TOOLS, SubagentSpec, load_prompt

SPEC = SubagentSpec(
    name="application-prep",
    description=("Writes a short application note for each fit role: why it matches, which resume points to put first, "
                 "and the gaps to prepare for. It uses only facts from the profile, and it sends nothing. It finds the "
                 "fit roles with no note itself, so do not give it role ids."),
    prompt=load_prompt(Path(__file__).parent),
    tools=("mcp__artifacts__get_brief", *READ_TOOLS, "mcp__artifacts__save_application_note",
           "mcp__artifacts__save_notes"),
    max_turns=40,
)
