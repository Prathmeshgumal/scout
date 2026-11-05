"""Mandate tools. The chat is the only place that creates or changes a mandate. Each change has two steps:
propose_mandate shows the change and saves nothing. confirm_mandate saves it, only after a new message from the user.
A new goal gets the two tools to create a mandate. A home chat gets them to change its mandate, and the tools that
read it. The agents of a run get none of them."""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, create_sdk_mcp_server, tool

from ..mandate_store import MandateStore
from ..mandates import (
    CHANGE_NOTE,
    CREATED_NOTE,
    MandateError,
    change_table,
    config_table,
    create_mandate,
    new_settings,
    preview_change,
    set_mandate_status,
    start_goal_now,
    update_mandate,
)
from ..schedule import slot_text
from . import ToolContext, result

SERVER_NAME = "mandates"
SETTINGS = {
    "title": {"type": "string", "description": "A short name for the schedule, fewer than 15 words."},
    "instruction": {"type": "string", "description": "What to find, word for word from the user: the criteria."},
    "every": {"type": "string", "description": "The interval: 1h to 23h, or a number of days (2d), weeks (1w) or "
                                               "months (1m). Default: 1d."},
    "jobs_per_run": {"type": "integer", "minimum": 1, "description": "The quota of fit roles for each mission."},
    "window_days": {"type": "array", "items": {"type": "integer", "minimum": 1, "maximum": 7},
                    "description": "Days with missions: 1 is Monday, 7 is Sunday. Default: 1 to 7. A planned day "
                                   "that is not in the list moves to the next day in the list."},
    "window_start": {"type": "string", "description": "First mission at: the time of the first mission of each day, e.g. "
                                     "09:00. For days, weeks and months: the time of each mission."},
    "window_end": {"type": "string", "description": "No missions after: no missions start from this time on, e.g. "
                                   "18:00. Set it only when the user asks for an end time. Only for intervals "
                                   "in hours."},
    "max_spend_per_run": {"type": "number", "description": "The spend cap of each mission, in USD."},
    "max_scrapes_per_run": {"type": "integer", "minimum": 0, "description": "The fresh-scrape cap of each mission."},
    "time_limit_minutes": {"type": ["integer", "string"],
                           "description": "The time limit of each mission: minutes (90), or hours and minutes (2h, "
                                          "1h 30m). At the limit, the mission starts no new work. Default: 2h."},
    "grace_minutes": {"type": ["integer", "string"],
                      "description": "Extra time to finish after the time limit: minutes (30) or 1h. The running "
                                     "subagents finish their work in it. Then all work of the mission stops. Default: 30m."},
    "max_total_spend": {"type": "number", "description": "The schedule ends at this total spend, in USD. Default: the default total spend of the app."},
    "ends_on": {"type": "string", "description": "Optional. The last day of the schedule, e.g. 2026-12-31."},
    "timezone": {"type": "string", "description": "The time zone of the clock times, e.g. Asia/Kolkata. Default: "
                                                  "the time zone of the user. Set it only when the user asks."},
}
ACTIONS = {
    "status": {"type": "string", "enum": ["active", "paused", "ended"],
               "description": "paused: no new missions start. active: resume from the next scheduled time. "
                              "ended: end the schedule for good."},
    "start_now": {"type": "boolean", "description": "true: a new mission starts within a minute. The set times after "
                                                    "it does not change."},
    "new_profile": {"type": "boolean", "description": "true: the schedule takes the profile of this chat, which the "
                                                    "candidate-profile subagent saved. Use it when the user changes the profile."},
}
CREATE_SCHEMA = {"type": "object", "properties": SETTINGS, "required": ["instruction", "jobs_per_run"]}
CHANGE_SCHEMA = {"type": "object", "properties": {**SETTINGS, **ACTIONS}}
EMPTY = {"type": "object", "properties": {}}
SHOW = "Show this table to the user exactly as it is, at the end of your reply."
ASK = ("Show this table to the user exactly as it is and ask: \"{question}\" Call confirm_mandate only after the "
       "user says yes in a new message. End your reply with the line below, exactly as it is. The user sees it as "
       "buttons, and the first button means yes. A \"!\" marks a red button for a change that cannot be undone.\n\n{buttons}")
# The buttons for a change of one kind. Other changes get "Save change" and "Discard".
CHANGE_BUTTONS = {("status", "paused"): "[[Pause schedule | Keep running]]",
                  ("status", "active"): "[[Resume schedule | Keep paused]]",
                  ("status", "ended"): "[[!End schedule | Keep schedule]]",
                  ("start_now", True): "[[Start mission now | Cancel]]"}


def change_buttons(change: dict) -> str:
    """The line of buttons for the answer to a change of the mandate."""
    if len(change) == 1:
        [(key, value)] = change.items()
        return CHANGE_BUTTONS.get((key, value), "[[Save change | Discard]]")
    return "[[Save change | Discard]]"
NOTHING = "No change waits for a yes. Call propose_mandate first."
NOT_READY = ("The candidate profile of this chat is not ready. Run the candidate-profile subagent first. Then call "
             "propose_mandate again.")
NO_NEW_PROFILE = ("This chat has no new profile. Run the candidate-profile subagent first. Then call propose_mandate with new_profile "
                "again.")
NO_ANSWER = ("The user did not answer the proposal yet. Ask the user to confirm it. Call confirm_mandate after the "
             "user says yes.")


def build_tools(ctx: ToolContext, kind: str) -> list[SdkMcpTool]:
    read_only = ToolAnnotations(readOnlyHint=True)
    # The proposal that waits for the user's yes: the settings, and the newest user message when it was made.
    waiting: dict[str, Any] = {}

    def ms() -> MandateStore:
        return MandateStore(ctx.store.pool)

    async def home() -> dict | None:
        return await ms().by_home(ctx.goal_id)

    async def user_zone() -> str | None:
        """The time zone of the user, from the browser. None for a goal from the command line."""
        return (await ctx.store.get_goal(ctx.goal_id) or {}).get("timezone")

    def given(args: dict) -> dict:
        return {key: value for key, value in args.items() if key in SETTINGS and value is not None}

    async def with_profile(values: dict, mandate: dict | None = None) -> dict | str:
        """The settings with the profile of this chat. Else the text of the refusal. A new mandate takes the chat's
        profile. A change (`mandate`) takes it only with `new_profile`. The candidate-profile subagent must have saved it, ready."""
        values = dict(values)
        new = bool(values.pop("new_profile", False))
        if mandate is not None and not new:
            return values
        goal = await ctx.store.get_goal(ctx.goal_id) or {}
        profile = goal.get("profile")
        if mandate is not None and (not profile or profile == mandate["profile"]):
            return NO_NEW_PROFILE
        if not goal.get("profile_ready"):  # a mandate needs a profile: each of its goals uses it
            return NOT_READY
        return {**values, "profile": profile, "profile_ready": True}

    async def keep(args: dict) -> None:
        waiting.clear()
        waiting.update(args=args, seq=await ctx.store.last_user_message_seq(ctx.goal_id))

    async def take() -> dict | str:
        """The proposal, when the user wrote after it. Else the text of the error."""
        if not waiting:
            return NOTHING
        if await ctx.store.last_user_message_seq(ctx.goal_id) <= waiting["seq"]:
            return NO_ANSWER
        args = waiting["args"]
        waiting.clear()
        return args

    async def refusal() -> str | None:
        goal = await ctx.store.get_goal(ctx.goal_id) or {}
        if goal.get("mandate_id"):
            return "This chat is a scheduled mission of a schedule. It cannot create a schedule."
        if goal.get("home_of"):
            return "This chat has a schedule already. Change it with propose_mandate in its own chat."
        return None

    @tool("propose_mandate", "Check the settings of a new schedule: work that repeats at set times. It saves "
          "nothing. The reply gives all the settings, with the defaults, as a table.", CREATE_SCHEMA)
    async def propose_new(args: dict[str, Any]) -> dict[str, Any]:
        if error := await refusal():
            return result(error, error=True)
        settings = await with_profile(given(args))
        if isinstance(settings, str):
            return result(settings, error=True)
        try:
            values = new_settings(settings, ctx.settings, await user_zone())
        except MandateError as exc:
            return result(str(exc), error=True)
        await keep(settings)
        table = config_table({**values, "next_run_at": ctx.now()})
        return result(f"Nothing is saved yet.\n\n{table}\n\n{ASK.format(question='Start it?', buttons='[[Start schedule | Change settings]]')}")

    @tool("confirm_mandate", "Save the schedule of the last propose_mandate. Call it only after the user said yes.",
          EMPTY)
    async def confirm_new(args: dict[str, Any]) -> dict[str, Any]:
        if error := await refusal():
            return result(error, error=True)
        settings = await take()
        if isinstance(settings, str):
            return result(settings, error=True)
        try:
            m = await create_mandate(ms(), ctx.recorder, ctx.goal_id, settings, ctx.settings, ctx.now(),
                                     await user_zone())
        except MandateError as exc:
            return result(str(exc), error=True)
        if ctx.on_change:
            ctx.on_change()
        return result(f"Saved.\n\n{config_table(m)}\n\n{CREATED_NOTE}\n\n{SHOW}")

    @tool("get_mandate", "Read all the settings of the schedule, its status and the time of the next mission, as a table.",
          EMPTY, annotations=read_only)
    async def get(args: dict[str, Any]) -> dict[str, Any]:
        m = await home()
        if m is None:
            return result("This chat has no schedule.", error=True)
        return result(f"The settings of the schedule:\n\n{config_table(m)}\n\n{SHOW}")

    @tool("propose_mandate", "Check a change to the schedule: settings, status (pause, resume or end) or a mission "
          "that starts now. Give only what changes. It saves nothing. The reply gives the change as a table.",
          CHANGE_SCHEMA)
    async def propose_change(args: dict[str, Any]) -> dict[str, Any]:
        m = await home()
        if m is None:
            return result("This chat has no schedule.", error=True)
        change = await with_profile(
            {key: value for key, value in args.items() if (key in SETTINGS or key in ACTIONS) and value is not None}, m)
        if isinstance(change, str):
            return result(change, error=True)
        try:
            now, after = await preview_change(ms(), m["id"], change, ctx.now())
        except MandateError as exc:
            return result(str(exc), error=True)
        await keep(change)
        table = change_table(now, after)
        if change.get("start_now"):
            table += "\n\nA new mission starts within a minute."
        return result(f"Nothing is saved yet.\n\n{table}\n\n{ASK.format(question='Save this change?', buttons=change_buttons(change))}")

    @tool("confirm_mandate", "Save the change of the last propose_mandate. Call it only after the user said yes.",
          EMPTY)
    async def confirm_change(args: dict[str, Any]) -> dict[str, Any]:
        m = await home()
        if m is None:
            return result("This chat has no schedule.", error=True)
        change = await take()
        if isinstance(change, str):
            return result(change, error=True)
        status, start_now = change.pop("status", None), change.pop("start_now", False)
        try:
            if change:
                m = await update_mandate(ms(), ctx.recorder, m["id"], change, "agent", ctx.now())
            if status:
                m = await set_mandate_status(ms(), ctx.recorder, m["id"], status, "agent", ctx.now())
            if start_now:
                m = await start_goal_now(ms(), ctx.recorder, m["id"], "agent", ctx.now())
        except MandateError as exc:
            return result(str(exc), error=True)
        return result(f"Saved.\n\n{config_table(m)}\n\n{CHANGE_NOTE}\n\n{SHOW}")

    @tool("list_runs", "List the missions that the schedule started: time, status and spend, with the mission id of each.", EMPTY,
          annotations=read_only)
    async def list_runs(args: dict[str, Any]) -> dict[str, Any]:
        m = await home()
        if m is None:
            return result("This chat has no schedule.", error=True)
        rows = await ms().runs(m["id"])
        if not rows:
            return result("No missions yet.")
        lines = [f"Mission {r['number']}, {slot_text(r['run_slot'], m['timezone'])}: {r['status']}, "
                 f"${r['spend_usd']:.2f}. Mission id: {r['id']}." for r in rows]
        return result("\n".join(lines))

    if kind == "goal":
        return [propose_new, confirm_new]
    return [get, list_runs, propose_change, confirm_change]


def build_server(ctx: ToolContext, kind: str):
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=build_tools(ctx, kind))
