"""Mandates: the one update path, used by the tools of the chat, and the text that describes a mandate."""

from __future__ import annotations

import re
from datetime import date, datetime, time
from typing import Any

from .config import Settings
from .events import Recorder
from .mandate_store import FIELDS, MandateStore
from .schedule import UNITS, Rule, check_every, every_text, known_zone, local_zone, next_slot, parse_every, slot_text
from .store import goal_title

SCHEDULE = {"every_unit", "every_count", "window_days", "window_start", "window_end", "timezone"}
STATUSES = ("active", "paused", "ended")
CONFLICT_TRIES = 5
DAY_NAMES = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}
SOURCES = {"agent": "the agent"}  # only the agent of the chat changes a mandate
LABELS = {"title": "title", "instruction": "instruction", "profile": "profile", "profile_ready": "profile",
          "every_unit": "interval", "every_count": "interval", "jobs_per_run": "quota for each goal",
          "window_days": "days",
          "window_start": "start time", "window_end": "end time",
          "max_spend_per_run": "spend cap for each goal", "max_scrapes_per_run": "scrape cap for each goal",
          "max_total_spend": "total spend limit", "ends_on": "end date", "timezone": "time zone",
          "time_limit_minutes": "time limit for each goal", "grace_minutes": "extra time to finish"}
MIN_TIME_LIMIT = 5  # minutes: a shorter goal cannot do the work of one batch
DURATION = re.compile(r"^(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+)\s*m(?:in)?)?$")


class MandateError(ValueError):
    """A refused value or action. The text goes to the user as it is."""


def _int(value: Any, name: str, low: int) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise MandateError(f"{LABELS[name]} must be a whole number.") from None
    if number < low:
        raise MandateError(f"{LABELS[name]} must be {low} or more.")
    return number


def _money(value: Any, name: str) -> float:
    try:
        number = round(float(str(value).strip().lstrip("$")), 2)
    except ValueError:
        raise MandateError(f"{LABELS[name]} must be an amount in USD, for example 2.00.") from None
    if number <= 0:
        raise MandateError(f"{LABELS[name]} must be more than 0.")
    return number


def _minutes(value: Any, name: str, low: int) -> int:
    """Minutes from a number of minutes ("90"), or from hours and minutes ("2h", "1.5h", "1h 30m", "45m")."""
    text = str(value if value is not None else "").strip().lower()
    match = None if text.isdigit() else DURATION.match(text)
    if text.isdigit():
        number = int(text)
    elif match and any(match.groups()):
        number = round(float(match[1] or 0) * 60) + int(match[2] or 0)
    else:
        raise MandateError(f"{LABELS[name]} must be a time such as 90 (minutes), 2h or 1h 30m.")
    if number < low:
        raise MandateError(f"{LABELS[name]} must be {low} minutes or more.")
    return number


def _time(value: Any, name: str) -> time:
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError:
        raise MandateError(f"{LABELS[name]} must be a time such as 09:00.") from None


def _days(value: Any) -> list[int]:
    items = value.split(",") if isinstance(value, str) else value
    try:
        days = sorted({int(str(item).strip()) for item in items or []})
    except ValueError:
        raise MandateError("days must be numbers from 1 (Monday) to 7 (Sunday).") from None
    if not days or not set(days) <= set(DAY_NAMES):
        raise MandateError("days must be one or more numbers from 1 (Monday) to 7 (Sunday).")
    return days


def _blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def clean(changes: dict) -> dict:
    """Column values for the settings in `changes`. Accepts "every" ("6h", "1d") and values as text."""
    out: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "every":
            try:
                out["every_unit"], out["every_count"] = parse_every(value)
            except ValueError as exc:
                raise MandateError(str(exc)) from None
        elif key not in FIELDS:
            raise MandateError(f"Not a mandate setting: {key}.")
        elif key in ("title", "instruction"):
            text = " ".join(str(value or "").split())
            if not text:
                raise MandateError(f"Give the {LABELS[key]}.")
            out[key] = text
        elif key == "profile":
            out[key] = value if isinstance(value, dict) and value else None
        elif key == "profile_ready":
            out[key] = bool(value)
        elif key == "every_unit":
            out[key] = str(value or "").strip().lower()
            if out[key] not in UNITS:
                raise MandateError("The interval unit must be hour, day or month.")
        elif key == "every_count":
            out[key] = _int(value, key, 1)
        elif key == "jobs_per_run":
            out[key] = _int(value, key, 1)
        elif key == "max_scrapes_per_run":
            out[key] = _int(value, key, 0)
        elif key == "max_spend_per_run":
            out[key] = _money(value, key)
        elif key == "time_limit_minutes":
            out[key] = _minutes(value, key, MIN_TIME_LIMIT)
        elif key == "grace_minutes":
            out[key] = _minutes(value, key, 0)
        elif key == "max_total_spend":
            out[key] = None if _blank(value) else _money(value, key)
        elif key == "window_days":
            out[key] = _days(value)
        elif key == "window_start":
            out[key] = _time(value, key)
        elif key == "window_end":
            out[key] = None if _blank(value) else _time(value, key)
        elif key == "timezone":
            out[key] = known_zone(str(value or ""))
            if out[key] is None:
                raise MandateError("time zone must be a name such as Asia/Kolkata or Europe/Berlin.")
        elif key == "ends_on":
            try:
                out[key] = None if _blank(value) else date.fromisoformat(str(value).strip())
            except ValueError:
                raise MandateError("end date must be a date such as 2026-12-31.") from None
    return out


def _check(m: dict) -> None:
    try:
        check_every(m["every_unit"], m["every_count"])
    except ValueError as exc:
        raise MandateError(str(exc)) from None
    if m["window_end"] is not None and m["window_end"] <= m["window_start"]:
        raise MandateError("\"No goals after\" must be later than \"First goal at\".")


async def _next(ms: MandateStore, m: dict, now: datetime) -> datetime:
    """The next planned time after now. Now when the mandate has no goal yet: the first goal starts at once."""
    first, last = await ms.slots(m["id"])
    if first is None or last is None:
        return now
    return next_slot(Rule.of(m), first, last, now)


def defaults(settings: Settings, zone: str | None = None) -> dict:
    """zone: the time zone of the user. Without it, the time zone of this machine."""
    return {"every_unit": "day", "every_count": 1, "window_days": [1, 2, 3, 4, 5, 6, 7], "window_start": time(9),
            "window_end": None, "timezone": zone or local_zone(), "jobs_per_run": 10,
            "max_spend_per_run": round(settings.max_spend_usd, 2),
            "max_scrapes_per_run": settings.max_fresh_scrapes, "max_total_spend": settings.mandate_total_usd, "ends_on": None,
            "time_limit_minutes": settings.mandate_time_limit_minutes, "grace_minutes": settings.mandate_grace_minutes,
            "profile": None, "profile_ready": False}


def span(minutes: int) -> str:
    """A time in minutes as the user reads it: 2h, 1h 30m, 45m."""
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours}h {rest}m"
    return f"{hours}h" if hours else f"{rest}m"


def _shown(key: str, value: Any) -> str:
    if value is None:
        return "none"
    if key in ("time_limit_minutes", "grace_minutes"):
        return span(value)
    if key in ("max_spend_per_run", "max_total_spend"):
        return f"${value:.2f}"
    if key == "window_days" and len(value) == 7:
        return "every day"
    if key == "window_days":
        return ", ".join(DAY_NAMES[day] for day in value)
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return str(value)


def _plain(values: dict) -> dict:
    """Values that JSON can keep: times and dates as text."""
    return {key: value.isoformat() if isinstance(value, (time, date)) else value for key, value in values.items()}


# What the user reads when a mandate starts. The words "home chat" and "run" are for the code only: the user reads
# "this chat" and "goal".
CHANGE_NOTE = "To change, pause, resume or end the mandate, ask here in this chat."
CREATED_NOTE = ("This chat now manages the mandate. On its schedule, the mandate starts a new goal. Each goal shows "
                f"under the mandate in the Mandates tab of the left panel. The first goal starts now. {CHANGE_NOTE}")


def describe(m: dict) -> str:
    start = _shown("window_start", m["window_start"])
    if m["every_unit"] != "hour":
        window = f"at {start}"
    else:
        window = f"{start} to {_shown('window_end', m['window_end'])}" if m["window_end"] else f"from {start}"
    every = f"One goal {every_text(m['every_unit'], m['every_count'])}"
    total = f"Total spend limit: ${m['max_total_spend']:.2f}." if m["max_total_spend"] else "No total spend limit."
    end = f"End date: {m['ends_on'].isoformat()}." if m["ends_on"] else "No end date."
    nxt = (f" Next goal: {slot_text(m['next_run_at'], m['timezone'])}."
           if m["status"] == "active" and m["next_run_at"] else "")
    days = "" if len(m["window_days"]) == 7 else f"{_shown('window_days', m['window_days'])}, "
    return (f"Mandate \"{m['title']}\": {m['instruction'].rstrip('.')}. {every}, {days}"
            f"{window}. Quota: {m['jobs_per_run']} fit roles for each goal. Caps for each goal: "
            f"${m['max_spend_per_run']:.2f} and {m['max_scrapes_per_run']} scrapes. Time limit for each goal: "
            f"{span(m['time_limit_minutes'])}, then {span(m['grace_minutes'])} to finish. {total} {end} "
            f"Status: {m['status']}.{nxt}")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _next_text(m: dict) -> str:
    if m["status"] != "active":
        return f"none: the mandate is {m['status']}"
    return slot_text(m["next_run_at"], m["timezone"]) if m.get("next_run_at") else "none"


def _shown_values(values: dict) -> dict:
    """The values for an event. The resume text stays out of events."""
    return {**values, "profile": profile_text(values["profile"])} if "profile" in values else values


def profile_text(profile: dict | None) -> str:
    """The profile in one line: for a table that the user reads. Never with the resume text."""
    if not profile:
        return "none"
    parts = []
    for key, label in (("target_titles", "Titles"), ("locations", "Places"), ("skills", "Skills")):
        value = (profile.get(key) or {}).get("value")
        if value:
            parts.append(f"{label}: {', '.join(map(str, value)) if isinstance(value, list) else value}")
    if profile.get("resume_text"):
        parts.append("Resume saved")
    return ". ".join(parts) or "saved"


def _rows(m: dict) -> list[tuple[str, str]]:
    """The settings of a mandate as the user reads them, one row each: what the mandate does, when its next goal
    runs, its schedule, and its limits."""
    rows = [("Title", m["title"]), ("Instruction", m["instruction"]),
            ("Profile", profile_text(m["profile"])),
            ("Status", m["status"]), ("Next goal", _next_text(m)), ("Time zone", m["timezone"]),
            ("Interval", every_text(m["every_unit"], m["every_count"])),
            ("Days", _shown("window_days", m["window_days"])),
            ("First goal at", _shown("window_start", m["window_start"])),
            ("No goals after", _shown("window_end", m["window_end"])),
            ("Quota for each goal", f"{m['jobs_per_run']} fit role{'' if m['jobs_per_run'] == 1 else 's'}"),
            ("Spend cap for each goal", _shown("max_spend_per_run", m["max_spend_per_run"])),
            ("Scrape cap for each goal", str(m["max_scrapes_per_run"])),
            ("Time limit for each goal", _shown("time_limit_minutes", m["time_limit_minutes"])),
            ("Extra time to finish", _shown("grace_minutes", m["grace_minutes"])),
            ("Total spend limit", _shown("max_total_spend", m["max_total_spend"])),
            ("End date", _shown("ends_on", m["ends_on"]))]
    return [(label, _cell(value)) for label, value in rows]


def config_table(m: dict) -> str:
    """All the settings of a mandate as a Markdown table. The agent shows it to the user as it is."""
    return "\n".join(["| Setting | Value |", "|---|---|", *(f"| {label} | {value} |" for label, value in _rows(m))])


def change_table(old: dict, new: dict) -> str:
    """The settings that a change gives a new value, as a Markdown table: the value now and after the change."""
    lines = [f"| {label} | {a} | {b} |" for (label, a), (_, b) in zip(_rows(old), _rows(new), strict=True) if a != b]
    return "\n".join(["| Setting | Now | After the change |", "|---|---|---|", *lines])


REASON_CHARS = 200  # each reason for not_fit in the kick-off message is one short line


def _one_line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= REASON_CHARS else text[: REASON_CHARS - 1].rstrip() + "…"


# The lines of the first message of a run that hold the instruction and the quota.
INSTRUCTION_LINE = "Instruction: "
QUOTA_LINE = "Quota for this goal: "


def kickoff_message(m: dict, number: int, slot: datetime, history: dict) -> str:
    """The first message of a run. Code writes it from Postgres. No LLM writes it. It does not grow with each
    goal: the system prompt tells the rule about earlier roles, and save_jobs applies it."""
    lines = [f"This is goal {number} of the mandate \"{m['title']}\", for the slot {slot_text(slot, m['timezone'])}.",
             f"{INSTRUCTION_LINE}{m['instruction']}",
             f"{QUOTA_LINE}{m['jobs_per_run']} fit roles. Spend cap: ${m['max_spend_per_run']:.2f}. "
             f"Scrape cap: {m['max_scrapes_per_run']}. Time limit: {span(m['time_limit_minutes'])}."]
    if not history["runs"]:
        return "\n".join([*lines, "This is the first goal of the mandate."])
    lines += [f"Earlier goals: {history['jobs']} roles found, {history['fit']} fit, {history['not_fit']} not a fit, "
              f"{history['unsure']} unsure."]
    if history["reasons"]:
        lines += ["Newest reasons for not_fit:", *(f"- {_one_line(reason)}" for reason in history["reasons"])]
    return "\n".join(lines)


def kickoff_instruction(text: str) -> str | None:
    """The instruction in the first message of a run: the instruction of the mandate when the run started."""
    start = text.find("\n" + INSTRUCTION_LINE)
    end = text.find("\n" + QUOTA_LINE, start + 1)
    if start < 0 or end < 0:
        return None
    return text[start + 1 + len(INSTRUCTION_LINE):end]


def new_settings(changes: dict, settings: Settings, zone: str | None = None) -> dict:
    """The values of a new mandate: the given settings and the defaults for the others. Nothing is saved.
    zone: the time zone of the user, for the clock times of the schedule."""
    values = {**defaults(settings, zone), **clean(changes)}
    if "instruction" not in values:
        raise MandateError("Give the instruction: what to find.")
    values.setdefault("title", goal_title(values["instruction"], 60))
    _check(values)
    return {**values, "status": "active"}


async def create_mandate(ms: MandateStore, recorder: Recorder, home_goal_id: str, changes: dict, settings: Settings,
                         now: datetime, zone: str | None = None) -> dict:
    values = new_settings(changes, settings, zone)
    m = await ms.create(home_goal_id, {**values, "next_run_at": now})
    await recorder.record(home_goal_id, "notice", trigger="mandate_created", mandate_id=m["id"],
                          text=f"Mandate created: {describe(m)}\n\n{CREATED_NOTE}")
    return m


async def _current(ms: MandateStore, mandate_id: str) -> dict:
    m = await ms.get(mandate_id)
    if m is None:
        raise MandateError("Unknown mandate.")
    if m["status"] == "ended":
        raise MandateError("This mandate has ended. It cannot change. Create a new mandate.")
    return m


async def preview_change(ms: MandateStore, mandate_id: str, changes: dict, now: datetime) -> tuple[dict, dict]:
    """The mandate now and after the changes, with the same checks as a save. Nothing is saved. Besides the settings,
    `changes` can give "status" (active, paused or ended) and "start_now" (a new goal starts at once)."""
    changes = dict(changes)
    status, start_now = changes.pop("status", None), bool(changes.pop("start_now", False))
    values = clean(changes)
    if not values and status is None and not start_now:
        raise MandateError("Give one or more changes.")
    current = await _current(ms, mandate_id)
    after = {**current, **values}
    _check(after)
    if status is not None:
        if status not in STATUSES:
            raise MandateError("The status must be active, paused or ended.")
        if status == current["status"]:
            raise MandateError(f"The mandate is {status} already.")
        after["status"] = status
    if after["status"] == "active" and (status or SCHEDULE & set(values)):
        after["next_run_at"] = await _next(ms, after, now)
    if start_now:
        if after["status"] != "active":
            raise MandateError("Only an active mandate can start a goal now.")
        after["next_run_at"] = now
    return current, after


async def update_mandate(ms: MandateStore, recorder: Recorder, mandate_id: str, changes: dict, source: str,
                         now: datetime) -> dict:
    """Change only the given settings. The write happens only when no other change came after the read. Else the
    mandate is read again, so two changes at the same time keep each other."""
    values = clean(changes)
    if not values:
        raise MandateError("Give one or more settings to change.")
    for _ in range(CONFLICT_TRIES):
        current = await _current(ms, mandate_id)
        merged = {**current, **values}
        _check(merged)
        write = dict(values)
        if SCHEDULE & set(values) and current["status"] == "active":
            write["next_run_at"] = await _next(ms, merged, now)
        m = await ms.update(mandate_id, write, seen=current["updated_at"])
        if m is not None:
            break
    else:
        raise MandateError("The mandate changed again and again. Try again.")
    if "jobs_per_run" in values:
        await ms.set_run_target(mandate_id, values["jobs_per_run"])
    if {"time_limit_minutes", "grace_minutes"} & set(values):
        await ms.set_run_time_limit(mandate_id, m["time_limit_minutes"], m["grace_minutes"])
    old = {key: current[key] for key in values if current[key] != values[key]}
    new = {key: values[key] for key in old}
    if old:
        shown = {key: f"{LABELS[key]} {_shown(key, old[key])} → {_shown(key, new[key])}" for key in old}
        if {"every_unit", "every_count"} & set(old):
            shown.pop("every_unit", None)
            shown["every_count"] = (f"interval {every_text(current['every_unit'], current['every_count'])} → "
                                    f"{every_text(merged['every_unit'], merged['every_count'])}")
        shown.pop("profile_ready", None)  # it changes with the profile
        parts = "; ".join(shown.values())
        await recorder.record(m["home_goal_id"], "notice", trigger="mandate_changed", source=source,
                              old=_plain(_shown_values(old)), new=_plain(_shown_values(new)),
                              text=f"Mandate changed by {SOURCES[source]}: {parts}.")
    return m


async def set_mandate_status(ms: MandateStore, recorder: Recorder, mandate_id: str, status: str, source: str,
                             now: datetime) -> dict:
    """Pause, resume or end. Resume does not start the goals that were missed: the next goal is the next planned
    time."""
    if status not in STATUSES:
        raise MandateError("The status must be active, paused or ended.")
    for _ in range(CONFLICT_TRIES):
        current = await _current(ms, mandate_id)
        write: dict[str, Any] = {"status": status}
        if status == "active":
            write["next_run_at"] = await _next(ms, current, now)
        m = await ms.update(mandate_id, write, seen=current["updated_at"])
        if m is not None:
            break
    else:
        raise MandateError("The mandate changed again and again. Try again.")
    word = {"active": "resumed", "paused": "paused", "ended": "ended"}[status]
    await recorder.record(m["home_goal_id"], "notice", trigger="mandate_changed", source=source,
                          old={"status": current["status"]}, new={"status": status},
                          text=f"Mandate {word} by {SOURCES[source]}.")
    return m


async def start_goal_now(ms: MandateStore, recorder: Recorder, mandate_id: str, source: str, now: datetime) -> dict:
    """The scheduler starts a new goal at its next check, within a minute. The schedule after it does not change."""
    current = await _current(ms, mandate_id)
    if current["status"] != "active":
        raise MandateError("Only an active mandate can start a goal now.")
    m = await ms.update(mandate_id, {"next_run_at": now})
    await recorder.record(m["home_goal_id"], "notice", trigger="mandate_changed", source=source,
                          text=f"Mandate changed by {SOURCES[source]}: a new goal starts within a minute.")
    return m
