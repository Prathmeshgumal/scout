"""Show goal events, roles, research and application notes in the terminal."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from rich.console import Console, Group, RenderableType
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .career_lens import wait_text
from .events import Hub, follow
from .store import Store

LABEL_WIDTH = 17
COLORS = {"you": "white", "main": "cyan", "candidate-profile": "white", "job-discovery": "magenta",
          "job-research": "green", "fit-check": "yellow", "application-prep": "blue"}


def _row(agent: str | None, content: str | RenderableType, style: str = "") -> Table:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=LABEL_WIDTH, no_wrap=True)
    grid.add_column(ratio=1)
    label = Text(agent or "", style=f"bold {COLORS.get(agent or '', 'white')}")
    grid.add_row(label, Text(content, style=style) if isinstance(content, str) else content)
    return grid


def _status(event: dict) -> Table | None:
    to, reason = event.get("to"), event.get("reason") or ""
    if to == "waiting":
        text = f"{reason}. I continue when they finish." if reason.startswith("Waiting on") else reason
        return _row(None, text, "yellow")
    if to == "failed":
        return _row(None, f"Failed: {reason}", "red")
    if to in ("capped", "stopped", "queued"):
        return _row(None, reason or to.capitalize(), "yellow")
    return None


def render_event(event: dict) -> RenderableType | None:
    """One goal event as terminal output, or None when the event is not shown."""
    kind, agent = event["type"], event.get("agent")
    if kind == "user_message":
        return _row("you", event["text"], "bold")
    if kind == "text":
        if agent in (None, "main"):
            return _row("main", Markdown(event["text"]))
        return _row(agent, event["text"], "dim")
    if kind == "tool_started":
        return _row(agent or "main", f"· {event['label']}", "dim")
    if kind == "tool_finished":
        if event.get("saved"):
            return _row(agent or "main", f"✓ {event['saved']}", "green")
        return None if event.get("ok") else _row(agent or "main", f"✗ {event['tool']}: {event['summary']}", "red")
    if kind == "agent_started":
        return _row(agent or "main", f"→ {event['name']}: {event['task']}")
    if kind == "agent_finished":
        return _row(agent or "main", f"← {event['name']}: {event['summary']}", "" if event.get("ok") else "red")
    if kind == "scrape_queued":
        return _row(None, f"⧗ Scrape queued: {event['kind']} {event['target']}", "dim")
    if kind == "scrape_status":
        wait = f" ({wait_text(event['wait_reason'])})" if event.get("wait_reason") else ""
        return _row(None, f"⧗ {event['request_id']}: {event['status']}{wait}", "dim")
    if kind == "scrape_finished":
        cancel = f" (career-lens: {event['answer']})" if event.get("answer") else ""
        return _row(None, f"⧗ {event['request_id']}: {event['status']}{cancel}", "dim")
    if kind == "notice":
        return _row(None, event["text"], "yellow")
    if kind == "status_changed":
        return _status(event)
    if kind == "response_finished" and event.get("cost_usd") is not None:
        return _row(None, f"Response {event['result']}: ${event['cost_usd']:.2f}, {event['turns']} turns "
                          f"(mission total ${event['spend_usd']:.2f})", "dim")
    return None


def status_line(goal: dict) -> str:
    return goal["status"] + (f": {goal['status_reason']}" if goal.get("status_reason") else "")


def pipeline_line(p: dict) -> str:
    target = f" · target {p['target']}" if p.get("target") else ""
    return (f"{p['jobs']} roles · {p['researched']} researched · {p['fit']} fit · {p['partial']} partial · "
            f"{p['not_fit']} not fit · {p['unsure']} unsure · {p['noted']} notes{target}")


def _yes(value: Any) -> str:
    return "✓" if value else ""


def job_table(rows: list[dict]) -> Table:
    table = Table("role id", "title", "company", "place", "evidence", "researched", "fit", "note")
    for row in rows:
        table.add_row(row["id"][:8], row["title"], row["company_name"], row["location"] or "", row["evidence_level"],
                      _yes(row["researched"]), row["fit"] or "", _yes(row["noted"]))
    return table


def notes_view(jobs: list[dict]) -> RenderableType:
    """The application notes of the fit roles, from ArtifactStore.get_job."""
    noted = [job for job in jobs if job.get("note")]
    if not noted:
        return Text("No application notes yet.")
    return Group(*(Panel(f"[bold]Why this role matches[/bold]\n{job['note']['why_match']}\n\n"
                         f"[bold]Resume points to put first[/bold]\n{job['note']['resume_points']}\n\n"
                         f"[bold]Gaps to prepare for[/bold]\n{job['note']['gaps']}",
                         title=f"{job['title']}, {job['company_name']}", title_align="left") for job in noted))


def _value(value: Any) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key}: {_value(item)}" for key, item in value.items() if item not in (None, "", [], {}))
    if isinstance(value, list):
        return "; ".join(_value(item) for item in value)
    return str(value)


def _day(when: datetime | str, zone: str) -> str:
    """The date of a time in the time zone of the user."""
    if isinstance(when, str):
        when = datetime.fromisoformat(when)
    return when.astimezone(ZoneInfo(zone)).date().isoformat()


def _research_lines(row: dict, zone: str) -> Text:
    """One research row. The data shape is open, so show each field that the agent saved."""
    text = Text()
    text.append(row["kind"], style="bold")
    text.append(f"  ({row['created_by']}, {_day(row['created_at'], zone)})\n", style="dim")
    for key, value in row["data"].items():
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            text.append(f"  {key}:\n")
            for item in value:
                text.append(f"    - {_value(item)}\n")
        else:
            text.append(f"  {key}: {_value(value) if value not in (None, '', [], {}) else '—'}\n")
    if row["sources"]:
        text.append(f"  sources: {', '.join(row['sources'])}\n", style="dim")
    return text


def research_view(jobs: list[dict], zone: str) -> RenderableType:
    """The saved job text and company facts of each role, from ArtifactStore.get_job. zone: the time zone of the user."""
    if not jobs:
        return Text("No roles yet.")
    panels = []
    for job in jobs:
        parts: list[RenderableType] = [Text(f"Evidence: {job['evidence_level']}", style="dim")]
        if job["description_text"]:
            parts.append(Text(job["description_text"]))
        parts += [_research_lines(row, zone) for row in job["research"]]
        panels.append(Panel(Group(*parts), title=f"{job['title']}, {job['company_name']}", title_align="left"))
    return Group(*panels)


class EventPrinter:
    """Prints the events of one goal: stored ones with flush(), then live ones with run()."""

    def __init__(self, store: Store, hub: Hub, out: Console, goal_id: str, after: int = 0):
        self.store, self.hub, self.out, self.goal_id = store, hub, out, goal_id
        self.last = after
        self.echo_user = True  # False in chat: the user's own line is on the screen already

    def show(self, event: dict) -> None:
        if event.get("seq") is not None:
            if event["seq"] <= self.last:
                return
            self.last = event["seq"]
        if event["type"] == "user_message" and not self.echo_user:
            return
        view = render_event(event)
        if view is not None:
            self.out.print(view)

    async def flush(self) -> None:
        for event in await self.store.list_events(self.goal_id, self.last):
            self.show(event)

    async def run(self) -> None:
        async for event in follow(self.store, self.hub, self.goal_id, self.last):
            self.show(event)
