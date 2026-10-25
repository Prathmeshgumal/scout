"""Store LinkedIn working hours, using the provider-neutral day planner.

The pure planner lives in :mod:`career_lens.fleet.dayplan`. Its public names
are re-exported here.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

from .dayplan import (  # noqa: F401 -- re-exported, see the docstring
    EDGE_EXCLUSION_MINUTES,
    HORIZON_DAYS,
    LUNCH_WINDOW_MINUTES,
    MIN_WORK_MINUTES,
    PACE_MAX,
    PACE_MIN,
    Block,
    DayTemplate,
    ScheduleError,
    day_rng,
    local_today,
    plan_day,
    required_minutes,
    zone,
)

__all__ = [
    "Block", "DayTemplate", "EDGE_EXCLUSION_MINUTES", "HORIZON_DAYS",
    "LUNCH_WINDOW_MINUTES", "MIN_WORK_MINUTES", "PACE_MAX", "PACE_MIN",
    "ScheduleError", "day_rng", "ensure_plans", "local_today",
    "materialise_all", "materialise_pending", "plan_day", "read_schedule",
    "required_minutes",
    "regenerate_future", "write_schedule",
]


# Generation happens entirely before any write, which is what makes one
# account's bad template harmless to the others: there is nothing to roll back,
# because nothing was written. No savepoints, no partial plans.


def read_schedule(connection: Connection, account_id: str) -> dict | None:
    """The operator's settings, or None when the account has no schedule."""
    row = connection.execute(text("""
        SELECT account_id, enabled, timezone, seed, start_jitter_minutes,
               end_jitter_minutes
        FROM account_schedule WHERE account_id = :id
    """), {"id": account_id}).one_or_none()
    if row is None:
        return None
    days = connection.execute(text("""
        SELECT weekday, working, start_local, end_local, lunch_minutes,
               break_count, break_minutes
        FROM account_schedule_day WHERE account_id = :id ORDER BY weekday
    """), {"id": account_id}).mappings().all()
    return {**dict(row._mapping), "days": [dict(day) for day in days]}


def _templates(connection: Connection,
               account_id: str) -> dict[int, DayTemplate]:
    rows = connection.execute(text("""
        SELECT weekday, working, start_local, end_local, lunch_minutes,
               break_count, break_minutes
        FROM account_schedule_day WHERE account_id = :id
    """), {"id": account_id}).all()
    return {row.weekday: DayTemplate(**dict(row._mapping)) for row in rows}


def ensure_plans(connection: Connection, account_id: str, *, now: datetime,
                 horizon: int = HORIZON_DAYS) -> int:
    """Plan every local date in the horizon that is not planned already.

    Returns how many days were newly planned. A day the account does not work
    is still planned, as a day with no blocks: "planned and empty" is Sunday,
    while "not planned" is a fault, and eligibility tells them apart.
    """
    schedule = read_schedule(connection, account_id)
    if schedule is None or not schedule["enabled"]:
        return 0
    where = zone(schedule["timezone"])
    templates = _templates(connection, account_id)
    created = 0
    for offset in range(horizon):
        on = now.astimezone(where).date() + timedelta(days=offset)
        template = templates.get(on.isoweekday())
        if template is None:
            raise ScheduleError(f"no template for weekday {on.isoweekday()}")
        # Generate first: a template that cannot be satisfied must not leave a
        # half-written day behind.
        blocks = plan_day(
            template, date=on, tz=where, rng=day_rng(schedule["seed"], on),
            start_jitter_minutes=schedule["start_jitter_minutes"],
            end_jitter_minutes=schedule["end_jitter_minutes"])
        day_id = connection.execute(text("""
            INSERT INTO account_shift_day (account_id, local_date, generated_at)
            VALUES (:account, :on, :now)
            ON CONFLICT (account_id, local_date) DO NOTHING
            RETURNING id
        """), {"account": account_id, "on": on, "now": now}).scalar_one_or_none()
        if day_id is None:
            continue        # already planned; leave the day exactly as it is
        created += 1
        if blocks:
            connection.execute(text("""
                INSERT INTO account_shift_block
                    (shift_day_id, account_id, ordinal, kind, starts_at,
                     ends_at, pace)
                VALUES (:day, :account, :ordinal, :kind, :starts_at, :ends_at,
                        :pace)
            """), [{
                "day": day_id, "account": account_id, "ordinal": ordinal,
                "kind": block.kind, "starts_at": block.starts_at,
                "ends_at": block.ends_at, "pace": block.pace,
            } for ordinal, block in enumerate(blocks)])
    return created


def regenerate_future(connection: Connection, account_id: str, *,
                      now: datetime) -> None:
    """Replan every date after today, leaving the running day untouched.

    A person's day is not rewritten at noon. The enable switch is the control
    for changing what happens now.
    """
    schedule = read_schedule(connection, account_id)
    if schedule is None:
        return
    connection.execute(text("""
        DELETE FROM account_shift_day
        WHERE account_id = :account AND local_date > :today
    """), {"account": account_id,
           "today": local_today(schedule["timezone"], now)})
    ensure_plans(connection, account_id, now=now)


def write_schedule(connection: Connection, account_id: str, *, enabled: bool,
                   timezone: str, start_jitter_minutes: int,
                   end_jitter_minutes: int, days: Sequence[dict],
                   now: datetime | None = None) -> None:
    """Replace an account's whole week, then replan the days ahead."""
    zone(timezone)
    weekdays = sorted(day["weekday"] for day in days)
    if weekdays != [1, 2, 3, 4, 5, 6, 7]:
        raise ScheduleError("a schedule needs each weekday from 1 to 7 once")
    changed_at = now or datetime.now(UTC)
    connection.execute(text("""
        INSERT INTO account_schedule
            (account_id, enabled, timezone, seed, start_jitter_minutes,
             end_jitter_minutes, created_at, updated_at)
        VALUES (:account, :enabled, :timezone, :seed, :start_jitter,
                :end_jitter, :now, :now)
        ON CONFLICT (account_id) DO UPDATE SET
            enabled = EXCLUDED.enabled, timezone = EXCLUDED.timezone,
            start_jitter_minutes = EXCLUDED.start_jitter_minutes,
            end_jitter_minutes = EXCLUDED.end_jitter_minutes,
            updated_at = EXCLUDED.updated_at
    """), {
        "account": account_id, "enabled": enabled, "timezone": timezone,
        # Only ever used on insert: an edit that reshuffled days the operator
        # never touched would make every save look like a new week.
        "seed": secrets.randbits(48), "start_jitter": start_jitter_minutes,
        "end_jitter": end_jitter_minutes, "now": changed_at,
    })
    connection.execute(text(
        "DELETE FROM account_schedule_day WHERE account_id = :account"),
        {"account": account_id})
    connection.execute(text("""
        INSERT INTO account_schedule_day
            (account_id, weekday, working, start_local, end_local,
             lunch_minutes, break_count, break_minutes)
        VALUES (:account, :weekday, :working, :start_local, :end_local,
                :lunch_minutes, :break_count, :break_minutes)
    """), [{"account": account_id, **day} for day in days])
    regenerate_future(connection, account_id, now=changed_at)


def materialise_all(connection: Connection, *,
                    now: datetime) -> dict[str, int]:
    """Top up every enabled schedule's horizon; return what each gained.

    One account's unsatisfiable template must not cost the rest of the fleet
    its plans, so each is generated independently and a failure is recorded
    against that account rather than raised.
    """
    accounts = connection.execute(text(
        "SELECT account_id FROM account_schedule WHERE enabled ORDER BY "
        "account_id")).scalars().all()
    created: dict[str, int] = {}
    for account_id in accounts:
        try:
            count = ensure_plans(connection, account_id, now=now)
        except ScheduleError as exc:
            connection.execute(text("""
                INSERT INTO linkedin_account_event
                    (account_id, kind, occurred_at, detail)
                VALUES (:account, 'schedule_generation_failed', :now,
                        CAST(:detail AS jsonb))
            """), {"account": account_id, "now": now,
                   "detail": json.dumps({"detail": str(exc)})})
            continue
        if count:
            created[account_id] = count
    return created


def materialise_pending(*, database: str | None = None,
                        now: datetime | None = None) -> dict[str, int]:
    """The task entry point: one transaction, every enabled schedule."""
    from ..store.db import transaction

    with transaction(database) as connection:
        return materialise_all(connection, now=now or datetime.now(UTC))
