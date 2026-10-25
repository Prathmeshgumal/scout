"""Turn a weekly template into the concrete shape of one day, for any provider.

Pure by construction: a template, a date, a timezone and a seeded `Random` in,
blocks out. No clock, no database, no I/O. That is what makes "does a person's
day look like this?" a question a test can answer, and what makes a regenerated
plan identical to the one it replaced.

Randomness is seeded from the account's seed and the date together, through
blake2b rather than `hash()`, which Python salts per process. The same account
on the same date produces the same day in any process, forever.

Kept apart from `schedules.py`, which holds the database code.
Nothing about it changed in the move: `schedules.py` re-exports every name, so
LinkedIn's callers and tests are what prove the lift was mechanical.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date as Date, datetime, time, timedelta
from decimal import Decimal
from hashlib import blake2b
from random import Random
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..store.schema.fleet import (
    SHIFT_EDGE_MINUTES as EDGE_EXCLUSION_MINUTES,
    SHIFT_MIN_WORK_MINUTES as MIN_WORK_MINUTES,
)

# How far either side of the shift's midpoint lunch may fall. A fixed hour is
# the single most recognisable thing about an automated day.
LUNCH_WINDOW_MINUTES = 45

# The band a work block's pace is drawn from, as a multiplier on the account's
# min_interval_seconds. Above 1 is slower than the baseline, below is faster.
PACE_MIN = 0.7
PACE_MAX = 1.6

# How many local dates are kept planned ahead. A week, because a week is what
# the operator configures -- and because the console answers "next on shift"
# by reading rows rather than simulating them.
HORIZON_DAYS = 7

# Rejection sampling gives up rather than looping forever on a template whose
# breaks cannot be placed.
_PLACEMENT_ATTEMPTS = 200

__all__ = [
    "Block",
    "DayTemplate",
    "EDGE_EXCLUSION_MINUTES",
    "HORIZON_DAYS",
    "LUNCH_WINDOW_MINUTES",
    "MIN_WORK_MINUTES",
    "PACE_MAX",
    "PACE_MIN",
    "ScheduleError",
    "day_rng",
    "local_today",
    "plan_day",
    "required_minutes",
    "zone",
]


class ScheduleError(ValueError):
    """A template cannot be turned into a day."""


@dataclass(frozen=True)
class DayTemplate:
    """One weekday's settings, as the operator entered them."""

    weekday: int
    working: bool
    start_local: time | None
    end_local: time | None
    lunch_minutes: int
    break_count: int
    break_minutes: int


@dataclass(frozen=True)
class Block:
    """One stretch of a materialised day, in real instants."""

    kind: str
    starts_at: datetime
    ends_at: datetime
    pace: Decimal | None


def day_rng(seed: int, on: Date) -> Random:
    """Randomness for one account on one date.

    blake2b rather than `hash()`: Python salts string hashing per process, so
    `hash()` would give a different day every time the worker restarted.
    """
    digest = blake2b(f"{seed}:{on.isoformat()}".encode(), digest_size=8)
    return Random(int.from_bytes(digest.digest(), "big"))


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def required_minutes(day: DayTemplate) -> int:
    """The shortest shift that can hold this template's pauses.

    Mirrors the `breaks_fit_shift` check constraint, which is why both read
    their numbers from the same two constants.
    """
    pauses = day.break_count + (1 if day.lunch_minutes > 0 else 0)
    if pauses == 0:
        return 1
    return (
        day.lunch_minutes
        + day.break_count * day.break_minutes
        + 2 * EDGE_EXCLUSION_MINUTES
        + MIN_WORK_MINUTES * (pauses - 1)
    )


def _place_pauses(
    day: DayTemplate, span: int, rng: Random
) -> list[tuple[int, int, str]]:
    """Choose where the pauses fall, as (offset, length, kind) from the open.

    Lunch goes first and near the middle, because it anchors the day; breaks
    then fill in around it by rejection sampling, keeping a working stretch
    between every pair and away from both ends of the shift.
    """
    placed: list[tuple[int, int, str]] = []

    def free(start: int, length: int) -> bool:
        if start < EDGE_EXCLUSION_MINUTES:
            return False
        if start + length > span - EDGE_EXCLUSION_MINUTES:
            return False
        return all(
            start + length + MIN_WORK_MINUTES <= other
            or other + other_length + MIN_WORK_MINUTES <= start
            for other, other_length, _ in placed
        )

    if day.lunch_minutes > 0:
        middle = span // 2 - day.lunch_minutes // 2
        for _ in range(_PLACEMENT_ATTEMPTS):
            start = middle + rng.randint(-LUNCH_WINDOW_MINUTES, LUNCH_WINDOW_MINUTES)
            if free(start, day.lunch_minutes):
                placed.append((start, day.lunch_minutes, "lunch"))
                break
        else:
            raise ScheduleError("lunch does not fit inside the shift")

    for index in range(day.break_count):
        for _ in range(_PLACEMENT_ATTEMPTS):
            start = rng.randint(
                EDGE_EXCLUSION_MINUTES,
                max(EDGE_EXCLUSION_MINUTES, span - EDGE_EXCLUSION_MINUTES),
            )
            if free(start, day.break_minutes):
                placed.append((start, day.break_minutes, "break"))
                break
        else:
            raise ScheduleError(f"break {index + 1} does not fit in the shift")

    return sorted(placed)


def _pace(rng: Random, *, last: bool) -> Decimal:
    """A work block's spacing multiplier.

    The last stretch of a day draws from the slower half of the band: people
    finish more slowly than they start, and a flat pace across the day is the
    uniformity this feature exists to break up.
    """
    top = (PACE_MIN + PACE_MAX) / 2 if last else PACE_MAX
    return Decimal(f"{rng.uniform(PACE_MIN, top):.2f}")


def _instant(on: Date, minutes: int, tz: ZoneInfo) -> datetime:
    """Local wall-clock minutes past midnight, as a real instant.

    Daylight saving makes this less obvious than it looks. A local time inside
    a spring-forward gap does not exist at all, and `fold` cannot rescue it, so
    walking forward a minute at a time until the round trip agrees lands on the
    first real instant after the gap. An ambiguous autumn time keeps `fold=0`,
    the first of its two instants.
    """
    naive = datetime.combine(on, time()) + timedelta(minutes=minutes)
    for _ in range(24 * 60):
        candidate = naive.replace(tzinfo=tz)
        if candidate.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == naive:
            return candidate.astimezone(UTC)
        naive += timedelta(minutes=1)
    raise ScheduleError(f"no real instant near {naive} in {tz}")


def _tile(
    pauses: Sequence[tuple[int, int, str]],
    span: int,
    opening: int,
    on: Date,
    tz: ZoneInfo,
    rng: Random,
) -> list[Block]:
    """Walk the pauses, emitting the work between them and the pauses too."""
    blocks: list[Block] = []
    cursor = 0

    def instant(offset: int) -> datetime:
        return _instant(on, opening + offset, tz)

    for start, length, kind in pauses:
        blocks.append(
            Block("work", instant(cursor), instant(start), _pace(rng, last=False))
        )
        blocks.append(Block(kind, instant(start), instant(start + length), None))
        cursor = start + length
    blocks.append(Block("work", instant(cursor), instant(span), _pace(rng, last=True)))
    return blocks


def plan_day(
    day: DayTemplate,
    *,
    date: Date,
    tz: ZoneInfo,
    rng: Random,
    start_jitter_minutes: int,
    end_jitter_minutes: int,
) -> list[Block]:
    """Turn one weekday template into that date's concrete blocks."""
    if not day.working:
        return []
    if day.start_local is None or day.end_local is None:
        raise ScheduleError("a working day needs both a start and an end")

    opening = _minutes(day.start_local) + rng.randint(
        -start_jitter_minutes, start_jitter_minutes
    )
    closing = _minutes(day.end_local) + rng.randint(
        -end_jitter_minutes, end_jitter_minutes
    )
    span = closing - opening
    if span < required_minutes(day):
        raise ScheduleError(f"a {span}-minute shift cannot hold this day's breaks")

    return _tile(_place_pauses(day, span, rng), span, opening, date, tz, rng)


def zone(name: str) -> ZoneInfo:
    """Resolve an IANA name, as a ScheduleError rather than a lookup error."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError(f"unknown timezone {name!r}") from exc


def local_today(timezone: str, now: datetime) -> Date:
    """The account's own date, which is the only date its schedule knows."""
    return now.astimezone(zone(timezone)).date()
