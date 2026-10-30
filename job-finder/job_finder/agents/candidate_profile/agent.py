"""candidate-profile: builds the candidate profile from the user's chat text, and saves it."""

from pathlib import Path

from ..spec import SubagentSpec, load_prompt

SPEC = SubagentSpec(
    name="candidate-profile",
    description=("Builds the candidate profile from the user's text and saves it. Give it the user's text about "
                 "themselves and the roles they want, word for word, with any resume that they pasted. It says if "
                 "target titles or locations are missing. Then ask the user."),
    prompt=load_prompt(Path(__file__).parent),
    tools=("mcp__artifacts__get_brief", "mcp__artifacts__list_files", "mcp__artifacts__read_file",
           "mcp__artifacts__save_profile", "mcp__artifacts__save_notes"),
    max_turns=10,
)
