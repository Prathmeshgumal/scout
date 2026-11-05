"""The job-finder command line."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Annotated

import psycopg
import sqlalchemy.exc
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .agents import REGISTRY
from .config import NO_LOGIN, Settings, env_file
from .deletion import Deleter, MandateChat
from .evals.runner import SUITES, EvalRunner
from .evals.store import EvalStore
from .login import TerminalOutput
from .login import login as save_login
from .mandate_store import MandateStore
from .mandates import describe
from .page_reader import PageReader
from .render import EventPrinter, job_table, notes_view, pipeline_line, render_event, research_view, status_line
from .schedule import every_text, local_zone, slot_text
from .services import GoalLocked, Services
from .store import goal_title

app = typer.Typer(help="A chat agent that finds jobs for one person, on the Claude Agent SDK.", no_args_is_help=True,
                  add_completion=False)
console = Console()
ENV_FILE = env_file()
GOAL_PREFIX = re.compile(r"^[0-9a-f-]{4,36}$")


class CliError(Exception):
    """An error to show to the user as one line."""


async def resolve_goal(store, text: str) -> dict:
    text = text.strip().lower()
    if not GOAL_PREFIX.match(text):
        raise CliError(f"Not a goal id: {text}")
    ids = await store.goals_by_prefix(text)
    if not ids:
        raise CliError(f"No goal starts with {text}.")
    if len(ids) > 1:
        raise CliError(f"More than one goal starts with {text}. Give more characters.")
    return await store.get_goal(ids[0])


async def new_goal(services: Services, text: str, agent: str | None = None) -> dict:
    s = services.settings
    return await services.store.create_goal(goal_title(text), max_fresh_scrapes=s.max_fresh_scrapes,
                                            max_spend_usd=s.max_spend_usd, agent=agent)


def need_key(settings: Settings) -> None:
    if settings.auth_mode == "none":
        raise CliError(NO_LOGIN)


# Commands. Each takes the services and the console first, so tests can call it directly.

async def list_goals(services: Services, out: Console) -> None:
    goals = await services.store.list_goals()
    if not goals:
        out.print("No goals yet. Start one with: job-finder chat")
        return
    table = Table("goal", "status", "title", "roles", "fit", "notes", "agent")
    for goal in goals:
        p = await services.artifacts.pipeline(goal["id"])
        table.add_row(goal["id"][:8], goal["status"], goal["title"], str(p["jobs"]), str(p["fit"]),
                      str(p["noted"]), goal["agent"] or "main")
    out.print(table)


async def show_goal(services: Services, out: Console, goal_text: str, jobs: bool = False, notes: bool = False,
                    events: bool = False, research: bool = False) -> None:
    goal = await resolve_goal(services.store, goal_text)
    goal_id = goal["id"]
    if events:
        for event in await services.store.list_events(goal_id):
            view = render_event(event)
            if view is not None:
                out.print(view)
        return
    if notes or research:
        rows = await services.artifacts.list_jobs(goal_id)
        details = [await services.artifacts.get_job(goal_id, row["id"]) for row in rows]
        # The times show in the user's zone, else in this machine's.
        out.print(notes_view(details) if notes else research_view(details, goal["timezone"] or local_zone()))
        return
    if not jobs:
        out.print(Text(f"{goal['title']}  ({goal_id[:8]}, {goal['agent'] or 'main'})", style="bold"))
        out.print(f"Status: {status_line(goal)}")
        brief = goal["brief"] or {}
        if brief:
            out.print(f"Brief: {brief.get('target_count')} fit roles. {brief.get('criteria', '')}")
        out.print(pipeline_line(await services.artifacts.pipeline(goal_id)))
    out.print(job_table(await services.artifacts.list_jobs(goal_id)))


async def run_agent(services: Services, out: Console, name: str, task: str, goal_text: str | None = None,
                    jobs_from: str | None = None) -> None:
    if name not in REGISTRY:
        raise CliError(f"Unknown subagent {name}. Use one of: {', '.join(REGISTRY)}.")
    need_key(services.settings)
    store = services.store
    if goal_text:
        goal = await resolve_goal(store, goal_text)
        if goal["agent"] != name:
            raise CliError(f"Goal {goal['id'][:8]} is not a {name} goal.")
    else:
        goal = await new_goal(services, task, agent=name)
        out.print(f"New {name} goal {goal['id'][:8]}.")
    goal_id = goal["id"]
    if jobs_from:
        source = await resolve_goal(store, jobs_from)
        copied = await services.artifacts.copy_jobs(source["id"], goal_id, name)
        out.print(f"Copied {copied} roles from goal {source['id'][:8]}.")
    events = await store.list_events(goal_id)
    printer = EventPrinter(store, services.hub, out, goal_id, after=events[-1]["seq"] if events else 0)
    async with services.drive(goal_id, recover=bool(goal_text)) as session:
        live = asyncio.create_task(printer.run())
        try:
            await session.send(task, "follow_up" if goal_text else "user")
            final = await session.settled()
        finally:
            live.cancel()
            await asyncio.gather(live, return_exceptions=True)
        await printer.flush()
    out.print(f"Goal {goal_id[:8]}: {status_line(final)}")


async def run_evals(services: Services, out: Console, suites: list[str], keep: bool = False,
                    runner_class=EvalRunner) -> list:
    """Run the eval suites, save each run and print a table. The agent suites use the Claude login and cost money."""
    names = list(suites) or list(SUITES)
    if unknown := [name for name in names if name not in SUITES]:
        raise CliError(f"Unknown suite: {', '.join(unknown)}. Use one of: {', '.join(SUITES)}.")
    need_key(services.settings)
    s = services.settings
    deleter = Deleter(s, services.store, MandateStore(services.store.pool), services.recorder,
                      career_lens=services.career_lens, files=services.files)

    async def drive(goal_id: str, task: str) -> None:
        async with services.drive(goal_id, recover=False) as session:
            await session.send(task, "user")
            await session.settled()

    async def cleanup(goal_id: str) -> None:
        if keep:
            out.print(f"Kept eval goal {goal_id[:8]}.")
        else:
            await deleter.delete_goal(goal_id)

    runner = runner_class(s, services.store, services.artifacts, PageReader.for_settings(s),
                          PageReader.for_settings(s, model=s.model), drive, cleanup, log=out.print)
    store, runs = EvalStore(services.store.pool), []
    for name in names:
        out.print(f"Running {name}...")
        run = await runner.run(name)
        await store.save(run)
        runs.append(run)
        for case in run.cases:
            if not case.get("passed"):
                parts = [f"expected {case['expect']}, got {case.get('got')}"] if "expect" in case else []
                parts += [str(why) for why in (case.get("error"), case.get("severity"), case.get("obeyed"),
                                               case.get("missing"), case.get("unsupported")) if why]
                out.print(f"  failed {case['id']}: {'; '.join(parts)}")
    table = Table("Suite", "Model", "Passed", "Score", "Cost", "Time", "Prompt")
    for run in runs:
        table.add_row(run.suite, run.model, f"{run.passed}/{run.total}", f"{run.score:.0%}", f"${run.cost_usd:.3f}",
                      f"{run.duration_s:.0f}s", run.prompt_sha)
    out.print(table)
    return runs


async def resolve_mandate(ms: MandateStore, text: str) -> dict:
    text = text.strip().lower()
    if not GOAL_PREFIX.match(text):
        raise CliError(f"Not a mandate id: {text}")
    ids = await ms.by_prefix(text)
    if not ids:
        raise CliError(f"No mandate starts with {text}.")
    if len(ids) > 1:
        raise CliError(f"More than one mandate starts with {text}. Give more characters.")
    return await ms.get(ids[0])


async def list_mandates(services: Services, out: Console) -> None:
    rows = await MandateStore(services.store.pool).list()
    if not rows:
        out.print("No mandates yet. Ask for work that repeats in a chat, for example \"20 fit roles each weekday\".")
        return
    table = Table("mandate", "status", "title", "every", "quota", "next goal")
    for m in rows:
        nxt = slot_text(m["next_run_at"], m["timezone"]) if m["status"] == "active" and m["next_run_at"] else "-"
        table.add_row(m["id"][:8], m["status"], m["title"], every_text(m["every_unit"], m["every_count"]), str(m["jobs_per_run"]), nxt)
    out.print(table)


async def show_mandate(services: Services, out: Console, text: str) -> None:
    ms = MandateStore(services.store.pool)
    m = await resolve_mandate(ms, text)
    out.print(describe(m))
    for run in await ms.runs(m["id"]):
        out.print(f"Goal {run['number']}, {slot_text(run['run_slot'], m['timezone'])}: {run['status']}, "
                  f"${run['spend_usd']:.2f}, goal {run['id'][:8]}")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _contents(p: dict) -> str:
    text = f"{_plural(p['jobs'], 'role')} and {_plural(p['notes'], 'application note')}."
    return text + (" It works now: it stops first." if p["working"] else "")


async def delete_goal(services: Services, out: Console, text: str, yes: bool, ask=typer.confirm) -> None:
    goal = await resolve_goal(services.store, text)
    if goal["home_of"]:
        raise CliError("This chat belongs to a mandate. Delete the mandate: "
                       f"job-finder mandate delete {goal['home_of'][:8]}")
    deleter = Deleter(services.settings, services.store, MandateStore(services.store.pool), services.recorder,
                      career_lens=services.career_lens, files=services.files)
    out.print(f"Goal {goal['id'][:8]}, {goal['title']}: {_contents(await deleter.goal_preview(goal['id']))}")
    if not yes and not ask("Delete the goal and all its data? You cannot undo this."):
        out.print("Nothing is deleted.")
        return
    await deleter.delete_goal(goal["id"])
    out.print("Deleted.")


async def delete_mandate(services: Services, out: Console, text: str, yes: bool, ask=typer.confirm) -> None:
    ms = MandateStore(services.store.pool)
    m = await resolve_mandate(ms, text)
    deleter = Deleter(services.settings, services.store, ms, services.recorder, career_lens=services.career_lens, files=services.files)
    p = await deleter.mandate_preview(m["id"])
    out.print(f"Mandate {m['id'][:8]}, {m['title']} ({m['status']}): its chat, {_plural(p['goals'], 'goal')}, "
              f"{_contents(p)}")
    if m["status"] != "ended":
        out.print("The mandate is not ended. To stop it and keep its data, ask its chat to end it.")
    if not yes and not ask("Delete the mandate and all its data? You cannot undo this."):
        out.print("Nothing is deleted.")
        return
    await deleter.delete_mandate(m["id"])
    out.print("Deleted.")


# Typer wiring

async def _with_services(settings: Settings, command, *args) -> None:
    services = await Services.open(settings)
    try:
        await command(services, console, *args)
    finally:
        await services.close()


def _run(command, *args) -> None:
    try:
        asyncio.run(_with_services(Settings.from_env(), command, *args))
    except (CliError, GoalLocked, MandateChat) as exc:
        console.print(Text(str(exc), style="red"))
        raise typer.Exit(1) from None
    except (psycopg.OperationalError, sqlalchemy.exc.OperationalError):
        console.print(Text("Cannot connect to Postgres. Check DATABASE_URL, or start it with: "
                           "docker compose up -d --wait postgres", style="red"))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("Interrupted.")
        raise typer.Exit(130) from None


GoalArg = Annotated[str, typer.Argument(help="Goal id, or a unique start of it.")]


@app.command()
def chat(goal: Annotated[str | None, typer.Argument(help="Goal id, or a unique start of it. "
                                                         "Leave it out to start a new goal.")] = None) -> None:
    """Chat with the main agent about a new goal or an existing goal."""
    from .chat import run_chat

    _run(run_chat, goal)


@app.command()
def goals() -> None:
    """List the goals."""
    _run(list_goals)


@app.command()
def show(goal: GoalArg,
         jobs: Annotated[bool, typer.Option("--jobs", help="Show only the table of roles.")] = False,
         notes: Annotated[bool, typer.Option("--notes", help="Show the application notes.")] = False,
         events: Annotated[bool, typer.Option("--events", help="Show the events of the goal.")] = False,
         research: Annotated[bool, typer.Option("--research",
                                                help="Show the job text and the company facts of each role.")] = False) -> None:
    """Show a goal: its brief, pipeline and roles."""
    _run(show_goal, goal, jobs, notes, events, research)


@app.command()
def delete(goal: GoalArg,
           yes: Annotated[bool, typer.Option("--yes", help="Do not ask first.")] = False) -> None:
    """Delete a goal and all its data. You cannot undo this."""
    _run(delete_goal, goal, yes)


@app.command()
def agent(name: Annotated[str, typer.Argument(help=f"One of: {', '.join(REGISTRY)}.")],
          task: Annotated[str, typer.Argument(help="The task for the subagent.")],
          goal: Annotated[str | None, typer.Option("--goal", help="Continue an earlier run of this subagent.")] = None,
          jobs_from: Annotated[str | None, typer.Option("--jobs-from",
                                                         help="Copy the roles of another goal first.")] = None) -> None:
    """Run one subagent alone. Use it to build and test a subagent."""
    _run(run_agent, name, task, goal, jobs_from)


@app.command("eval")
def eval_(suites: Annotated[list[str] | None, typer.Argument(help=f"Suites to run: {', '.join(SUITES)}. "
                                                                   "Leave it out to run all.")] = None,
          keep: Annotated[bool, typer.Option("--keep", help="Keep the eval goals, to read them.")] = False) -> None:
    """Run the evals on made-up cases and save the scores. The metrics page shows them. Costs money."""
    _run(run_evals, suites or [], keep)


mandate_app = typer.Typer(help="Mandates: work that repeats on a schedule. Create and change them in a chat.",
                          no_args_is_help=True)
app.add_typer(mandate_app, name="mandate")
MandateArg = Annotated[str, typer.Argument(help="Mandate id, or a unique start of it.")]


@mandate_app.command("list")
def mandate_list() -> None:
    """List the mandates."""
    _run(list_mandates)


@mandate_app.command("show")
def mandate_show(mandate: MandateArg) -> None:
    """Show the settings and the goals of a mandate."""
    _run(show_mandate, mandate)


@mandate_app.command("delete")
def mandate_delete(mandate: MandateArg,
                   yes: Annotated[bool, typer.Option("--yes", help="Do not ask first.")] = False) -> None:
    """Delete a mandate, its chat and all its goals, with all their data. You cannot undo this."""
    _run(delete_mandate, mandate, yes)


@app.command()
def login(api_key: Annotated[bool, typer.Option("--api-key", help="Save an Anthropic API key, not a token.")] = False,
          account_label: Annotated[str | None, typer.Option(help="An account label that you provide. Ownership is not verified.")] = None
          ) -> None:
    """Save a Claude login to .env: a subscription token (opens the browser) or an API key."""
    ask = lambda text: typer.prompt(text, default="", show_default=False, hide_input=True, prompt_suffix=" ")  # noqa: E731
    terminal = TerminalOutput(console)
    def ask_label() -> str:
        return account_label if account_label is not None else typer.prompt(
            "Account label (optional, user-provided)", default="", show_default=False)
    raise typer.Exit(save_login(ENV_FILE, api_key=api_key, ask=ask, ask_label=ask_label,
                               out=terminal.out, code=terminal.code))


@app.command()
def serve() -> None:
    """Start the web app. Build the UI first: cd web && npm install && npm run build."""
    import uvicorn

    from .web.app import STATIC_DIR, create_app

    logging.getLogger().setLevel(logging.INFO)
    settings = Settings.from_env()
    if not STATIC_DIR.is_dir():
        console.print(Text("No built UI in web/dist: only the API runs.", style="yellow"))
    # An open browser stream would otherwise keep the server from stopping. Goals continue on the next start.
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, timeout_graceful_shutdown=5)


def main() -> None:
    load_dotenv(ENV_FILE)
    logging.basicConfig(level=logging.WARNING)
    app()
