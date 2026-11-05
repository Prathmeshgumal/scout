"""The shape of a subagent: its prompt, its tools and how the main agent sees it."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import AgentDefinition

from ..tools.artifacts import SERVER_NAME as ARTIFACTS

READ_TOOLS = ("mcp__artifacts__list_jobs", "mcp__artifacts__read_job")


# The writing rules that every subagent follows, at the end of its prompt.
WRITING = (Path(__file__).parent / "writing.md").read_text()
# What agents may do, in the prompt of every agent: the main agent, the mandate chat and each subagent.
SCOPE = (Path(__file__).parent.parent / "scope.md").read_text().rstrip()


def load_prompt(folder: Path) -> str:
    return f"{(folder / 'prompt.md').read_text().rstrip()}\n\n{SCOPE}\n\n{WRITING}"


@dataclass(frozen=True)
class SubagentSpec:
    name: str
    description: str          # tells the main agent when to use this subagent and what to give it
    prompt: str
    tools: tuple[str, ...]    # e.g. "mcp__career_lens__google_search", "mcp__artifacts__save_jobs"
    model: str | None = None  # None: the main model
    max_turns: int | None = None

    @property
    def server(self) -> str:
        """This subagent's own artifacts server, so the rows it saves record its name."""
        return f"{ARTIFACTS}_{self.name.replace('-', '_')}"

    def scoped(self, tool_name: str) -> str:
        prefix = f"mcp__{ARTIFACTS}__"
        if tool_name.startswith(prefix):
            return f"mcp__{self.server}__{tool_name.removeprefix(prefix)}"
        return tool_name

    def builtin_tools(self) -> list[str]:
        return [name for name in self.tools if not name.startswith("mcp__")]

    def definition(self, effort: str | None = None) -> AgentDefinition:
        """The SDK definition. effort: the goal's effort, so that the subagent works as the main agent does."""
        return AgentDefinition(description=self.description, prompt=self.prompt,
                               tools=[self.scoped(name) for name in self.tools], model=self.model,
                               maxTurns=self.max_turns, effort=effort)
