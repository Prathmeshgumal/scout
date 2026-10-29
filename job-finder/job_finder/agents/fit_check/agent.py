"""fit-check: compares each researched role with the candidate profile, and saves a verdict."""

from pathlib import Path

from ..spec import READ_TOOLS, SubagentSpec, load_prompt

SPEC = SubagentSpec(
    name="fit-check",
    description=("Compares each researched role with the candidate profile, and saves a verdict: fit, partial, not_fit "
                 "or unsure. It reads the profile and finds the roles that need a verdict itself, so do not give it "
                 "role ids. Give only extra instructions."),
    prompt=load_prompt(Path(__file__).parent),
    tools=("mcp__artifacts__get_brief", *READ_TOOLS, "mcp__artifacts__save_fit", "mcp__artifacts__save_notes"),
    max_turns=60,
)
