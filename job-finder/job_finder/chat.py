"""The chat session: the user talks with the main agent about one goal, while subagents work."""

from __future__ import annotations

import asyncio
import contextlib

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from .cli import CliError, need_key, new_goal, resolve_goal
from .options import retry_message
from .render import EventPrinter, job_table, pipeline_line, status_line
from .services import Services, with_pending_note
from .session import GoalSession

HELP = "Commands: /status, /jobs, /stop, /retry, exit"


def _minutes(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60:02d}s"


class Chat:
    def __init__(self, services: Services, session: GoalSession, out: Console):
        self.s, self.session, self.out = services, session, out
        self.goal_id = session.goal_id
        self.printer = EventPrinter(services.store, services.hub, out, self.goal_id)

    async def send(self, text: str, trigger: str) -> None:
        await self.session.send(await with_pending_note(self.s.store, self.goal_id, text), trigger)

    async def handle(self, line: str) -> bool:
        """Act on one line from the user. False means: the user wants to close the chat."""
        text = line.strip()
        store = self.s.store
        if not text:
            return True
        if text.lower() in ("exit", "quit"):
            return False
        if text == "/status":
            goal = await store.get_goal(self.goal_id)
            self.out.print(f"Status: {status_line(goal)}")
            self.out.print(f"Spend: ${goal['spend_usd']:.2f} of ${goal['max_spend_usd']:.2f}")
            self.out.print(pipeline_line(await self.s.artifacts.pipeline(self.goal_id)))
            for task in self.session.running_tasks():
                last = f", last tool {task['last_tool']}" if task["last_tool"] else ""
                self.out.print(f"  {task['name']}: {task['description']} ({_minutes(task['elapsed_s'])}{last})")
        elif text == "/jobs":
            self.out.print(job_table(await self.s.artifacts.list_jobs(self.goal_id)))
        elif text == "/stop":
            await self.session.stop()
            self.out.print("Stopped all work on this mission.")
        elif text == "/retry":
            goal = await store.get_goal(self.goal_id)
            if self.session.main_busy or goal["status"] not in ("failed", "stopped"):
                self.out.print("Only a failed or stopped mission can be retried.")
            else:
                note = retry_message(goal, await store.first_user_message(self.goal_id))
                await self.session.send(note, "retry", user=False)
        elif text.startswith("/"):
            self.out.print(HELP)
        else:
            await self.send(text, "follow_up")
        return True

    async def ctrl_c(self, prompt) -> bool:
        """Ctrl-C stops the work that runs. The SDK cannot stop a reply alone: running subagents stop too, so
        ask first. False when nothing runs, which means: close the chat. To change direction, type instead."""
        tasks = self.session.running_tasks()
        if tasks:
            names = ", ".join(task["name"] for task in tasks)
            if await self._yes(prompt, f"This also stops {names}. Stop everything? [y/N] "):
                await self.session.stop()
                self.out.print("Stopped all work on this mission.")
            return True
        return await self.session.interrupt()

    @staticmethod
    async def _yes(prompt, question: str) -> bool:
        try:
            answer = await prompt.prompt_async(question)
        except KeyboardInterrupt:
            return False
        except EOFError:
            return True
        return answer.strip().lower() in ("y", "yes")

    async def confirm_close(self, prompt) -> bool:
        """True when nothing runs, or when the user agrees to leave the running subagents for later."""
        tasks = self.session.running_tasks()
        if not tasks:
            return True
        names = ", ".join(task["name"] for task in tasks)
        count = f"{len(tasks)} subagents are" if len(tasks) > 1 else "1 subagent is"
        return await self._yes(prompt, f"{count} still working ({names}). Exit now? "
                                       "You can resume them when you open this mission again. [y/N] ")


async def run_chat(services: Services, out: Console, goal_text: str | None = None, *, prompt=None) -> None:
    need_key(services.settings)
    reader = prompt or PromptSession()
    # patch_stdout prints events above the input line. A test gives its own prompt and needs no patch.
    with patch_stdout(raw=True) if prompt is None else contextlib.nullcontext():
        first = None
        if goal_text:
            goal = await resolve_goal(services.store, goal_text)
            if goal["agent"]:
                short = goal["id"][:8]
                raise CliError(f"Mission {short} is a {goal['agent']} mission. Use: "
                               f'job-finder agent {goal["agent"]} "<task>" --mission {short}')
        else:
            try:
                first = (await reader.prompt_async("mission> ")).strip()
            except (EOFError, KeyboardInterrupt):
                return
            if not first:
                return
            goal = await new_goal(services, first)
            out.print(f"New mission {goal['id'][:8]}. Type exit to leave. Open it again with: "
                      f"job-finder chat {goal['id'][:8]}")
        async with services.drive(goal["id"], recover=first is None) as session:
            chat = Chat(services, session, out)
            await chat.printer.flush()  # the history of an existing goal
            chat.printer.echo_user = False
            live = asyncio.create_task(chat.printer.run())
            try:
                if first is not None:
                    await chat.send(first, "user")
                while True:
                    try:
                        line = await reader.prompt_async("you> ")
                    except KeyboardInterrupt:
                        if await chat.ctrl_c(reader):
                            continue
                        break
                    except EOFError:
                        break
                    if not await chat.handle(line) and await chat.confirm_close(reader):
                        break
            finally:
                live.cancel()
                await asyncio.gather(live, return_exceptions=True)
