"""The day planner is pure, and now belongs to no provider."""

from __future__ import annotations

from datetime import date as Date, time
from zoneinfo import ZoneInfo

from career_lens.fleet import dayplan, schedules


def _template():
    return dayplan.DayTemplate(
        weekday=1,
        working=True,
        start_local=time(9, 0),
        end_local=time(17, 30),
        lunch_minutes=45,
        break_count=2,
        break_minutes=10,
    )


def test_the_same_seed_and_date_plan_the_same_day():
    on, where = Date(2026, 8, 24), ZoneInfo("Asia/Kolkata")
    first = dayplan.plan_day(
        _template(),
        date=on,
        tz=where,
        rng=dayplan.day_rng(7, on),
        start_jitter_minutes=15,
        end_jitter_minutes=20,
    )
    second = dayplan.plan_day(
        _template(),
        date=on,
        tz=where,
        rng=dayplan.day_rng(7, on),
        start_jitter_minutes=15,
        end_jitter_minutes=20,
    )
    assert [(b.kind, b.starts_at, b.ends_at) for b in first] == [
        (b.kind, b.starts_at, b.ends_at) for b in second
    ]


def test_the_planner_touches_no_database_and_no_clock():
    with open(dayplan.__file__, encoding="utf-8") as handle:
        text = handle.read()
    assert "sqlalchemy" not in text
    assert "datetime.now" not in text


def test_schedules_still_exports_every_name_it_used_to():
    for name in (
        "ScheduleError",
        "DayTemplate",
        "Block",
        "day_rng",
        "plan_day",
        "required_minutes",
        "zone",
        "local_today",
        "HORIZON_DAYS",
    ):
        assert getattr(schedules, name) is getattr(dayplan, name)
