"""When the goals of a mandate start. Times in the database are UTC. The rule (days, "First goal at", "No goals after")
is clock time in the time zone of the mandate, which is the time zone of this machine.

Hours: on each active day, goals start at window_start and then every N hours, before window_end. On the day of a goal
that started out of the pattern (the first goal, or "Start a goal now"), the rest of that day counts from that goal.
Days and months: one goal at window_start on each planned day. The planned days count from the day of the first goal.
A planned day that is not active moves to the next active day. The count does not move.
"""

from __future__ import annotations

import calendar
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOCALTIME = Path("/etc/localtime")
UNITS = ("hour", "day", "month")
LIMITS = {"hour": 23, "day": 365, "month": 12}
EVERY = re.compile(r"^\s*([0-9]+)\s*([hdwm]?)\s*$", re.IGNORECASE)


def local_zone(env: Mapping[str, str] | None = None, localtime: Path = LOCALTIME) -> str:
    """The time zone of this machine, for example Asia/Kolkata: TZ, else the zone that /etc/localtime links to.
    UTC when neither gives a known zone."""
    names = [(os.environ if env is None else env).get("TZ", "").removeprefix(":")]
    try:
        names.append(str(localtime.resolve()).partition("zoneinfo/")[2])
    except OSError:
        pass
    for name in names:
        try:
            if name:
                ZoneInfo(name)
                return name
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return "UTC"


def known_zone(name: str | None) -> str | None:
    """The name when it is a zone of the time zone database, for example Asia/Kolkata. Else None."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def check_every(unit: str, count: int) -> None:
    if unit not in UNITS:
        raise ValueError(f"The interval unit must be hour, day or month. Not {unit!r}.")
    if not 1 <= count <= LIMITS[unit]:
        raise ValueError(f"An interval in {unit}s must be from 1 to {LIMITS[unit]}. Not {count}.")


def parse_every(value: str | int) -> tuple[str, int]:
    """The interval as (unit, count). "6h" is 6 hours, "24h" and "1d" are 1 day, "2w" is 14 days, "1m" is 1 month.
    A number without a unit is hours."""
    match = EVERY.match(str(value))
    if not match:
        raise ValueError(f"Give the interval as a number and h, d, w or m, for example 6h, 1d, 1w or 1m. "
                         f"Not {value!r}.")
    number, letter = int(match.group(1)), (match.group(2) or "h").lower()
    if letter == "h" and number and number % 24 == 0:
        letter, number = "d", number // 24
    units = {"h": ("hour", number), "d": ("day", number), "w": ("day", number * 7), "m": ("month", number)}
    unit, count = units[letter]
    check_every(unit, count)
    return unit, count


def every_text(unit: str, count: int) -> str:
    """"every hour", "every 6 hours", "each day", "every 2 weeks", "each month"."""
    if unit == "day" and count % 7 == 0:
        unit, count = "week", count // 7
    if count == 1:
        return "every hour" if unit == "hour" else f"each {unit}"
    return f"every {count} {unit}s"


@dataclass(frozen=True)
class Rule:
    unit: str
    count: int
    days: list[int]
    start: time
    end: time | None
    tz: str

    @classmethod
    def of(cls, m: dict) -> Rule:
        return cls(m["every_unit"], m["every_count"], m["window_days"], m["window_start"], m["window_end"],
                   m["timezone"])

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def at(self, day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, self.zone)

    def day_end(self, day: date) -> datetime:
        """The end of the window of a day: window_end, else midnight."""
        return self.at(day, self.end) if self.end else self.at(day + timedelta(days=1), time(0))

    def in_window(self, when: datetime) -> bool:
        here = when.astimezone(self.zone)
        return here.isoweekday() in self.days and self.at(here.date(), self.start) <= here < self.day_end(here.date())


def _add_months(day: date, months: int) -> date:
    """The same day number some months later. A day that the month does not have becomes its last day."""
    index = day.month - 1 + months
    year, month = day.year + index // 12, index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _hours_slot(rule: Rule, last: datetime, after: datetime) -> datetime:
    step = timedelta(hours=rule.count)
    day = last.astimezone(rule.zone).date()
    slot = last + step  # the rest of the day of the newest goal counts from it
    while slot.astimezone(rule.zone).date() == day and slot < rule.day_end(day):
        if slot > after and rule.in_window(slot):
            return slot
        slot += step
    first_day = max(day + timedelta(days=1), after.astimezone(rule.zone).date())
    for offset in range(8):
        day = first_day + timedelta(days=offset)
        if day.isoweekday() not in rule.days:
            continue
        slot = rule.at(day, rule.start)
        while slot < rule.day_end(day):
            if slot > after:
                return slot
            slot += step
    raise ValueError("The schedule has no active day.")


def _days_slot(rule: Rule, first: datetime, after: datetime) -> datetime:
    anchor = first.astimezone(rule.zone).date()
    if rule.unit == "day":
        planned = lambda k: anchor + timedelta(days=rule.count * k)  # noqa: E731
        guess = (after.astimezone(rule.zone).date() - anchor).days // rule.count - 1
    else:
        planned = lambda k: _add_months(anchor, rule.count * k)  # noqa: E731
        here = after.astimezone(rule.zone)
        guess = ((here.year - anchor.year) * 12 + here.month - anchor.month) // rule.count - 1
    for k in range(max(1, guess), max(1, guess) + 400):
        day = planned(k)
        for _ in range(7):  # a planned day that is not active moves to the next active day
            if day.isoweekday() in rule.days:
                break
            day += timedelta(days=1)
        else:
            raise ValueError("The schedule has no active day.")
        slot = rule.at(day, rule.start)
        if slot > after:
            return slot
    raise ValueError("The schedule has no next mission.")


def next_slot(rule: Rule, first: datetime, last: datetime, after: datetime) -> datetime:
    """The first planned time after `after`, in UTC. `first` and `last` are the slots of the first and the newest
    goal."""
    if not rule.days:
        raise ValueError("The schedule has no active day.")
    slot = _hours_slot(rule, last, after) if rule.unit == "hour" else _days_slot(rule, first, after)
    return slot.astimezone(UTC)


def newest_due(rule: Rule, first: datetime, last: datetime, slot: datetime, now: datetime) -> datetime:
    """The newest planned time that is not after now, from a slot that is late. Older missed times are skipped."""
    for _ in range(10_000):
        later = next_slot(rule, first, last, slot)
        if later > now:
            return slot
        slot = later
    return slot


def can_start_late(rule: Rule, slot: datetime, now: datetime) -> bool:
    """A late goal starts only on the same day as its slot, inside the window of that day. A goal in days or months
    has no window_end: the end of its day is midnight."""
    day = slot.astimezone(rule.zone).date()
    if now.astimezone(rule.zone).date() != day:
        return False
    return rule.unit != "hour" or now < rule.day_end(day)


def slot_text(when: datetime, tz: str) -> str:
    """A time as the user reads it, in the time zone of the mandate: "Thu 8 Oct 2026 09:00"."""
    here = when.astimezone(ZoneInfo(tz))
    return f"{here:%a} {here.day} {here:%b %Y %H:%M}"
