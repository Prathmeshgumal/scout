"""Keeping a rolling week of shift plans ahead of every enabled schedule."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from sqlalchemy import text

from career_lens.fleet.schedules import (
    HORIZON_DAYS, materialise_all, read_schedule, write_schedule,
)

# A Friday, mid-morning in London.
NOW = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
TODAY = date(2026, 8, 14)
TOMORROW = date(2026, 8, 15)


def _week(*, start=time(9, 0), end=time(17, 30), working=True):
    return [{
        "weekday": weekday, "working": working, "start_local": start,
        "end_local": end, "lunch_minutes": 45, "break_count": 2,
        "break_minutes": 12,
    } for weekday in range(1, 8)]


def _account(connection, account="la-a"):
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES (:id, :id, :username, 'enabled', 10, 60, :now, :now)
    """), {"id": account, "username": f"{account}@example.com", "now": NOW})
    return account


def _schedule(connection, account="la-a", *, enabled=True,
              timezone="Europe/London", days=None):
    _account(connection, account)
    write_schedule(connection, account, enabled=enabled, timezone=timezone,
                   start_jitter_minutes=15, end_jitter_minutes=20,
                   days=days or _week(), now=NOW)
    return account


def _blocks(connection, account="la-a", on=None):
    clause = "AND day.local_date = :on" if on else ""
    return connection.execute(text(f"""
        SELECT block.kind, block.starts_at, block.ends_at, block.pace
        FROM account_shift_block block
        JOIN account_shift_day day ON day.id = block.shift_day_id
        WHERE block.account_id = :account {clause}
        ORDER BY block.starts_at
    """), {"account": account, **({"on": on} if on else {})}).all()


def _days(connection, account="la-a"):
    return connection.execute(text("""
        SELECT count(*) FROM account_shift_day WHERE account_id = :account
    """), {"account": account}).scalar_one()


def test_a_rolling_week_of_plans_is_created(app):
    _schedule(app)
    assert materialise_all(app, now=NOW) == {}      # write_schedule planned it
    assert _days(app) == HORIZON_DAYS


def test_running_twice_creates_nothing_further(app):
    _schedule(app)
    before = _blocks(app)
    assert materialise_all(app, now=NOW) == {}
    assert _blocks(app) == before


def test_a_disabled_schedule_is_not_planned(app):
    _schedule(app, enabled=False)
    assert _days(app) == 0
    assert materialise_all(app, now=NOW) == {}


def test_a_day_off_is_planned_as_a_day_with_no_blocks(app):
    """Planned-and-empty is not the same as never planned.

    Eligibility distinguishes them: an unplanned day is a fault that needs an
    operator, a planned day with no work is Sunday.
    """
    _schedule(app, days=_week(working=False, start=None, end=None))
    assert _days(app) == HORIZON_DAYS
    assert _blocks(app) == []


def test_the_account_timezone_decides_the_instant(app):
    _schedule(app, account="la-a", timezone="Europe/London")
    _schedule(app, account="la-b", timezone="Asia/Kolkata")
    first = _blocks(app, "la-a", TOMORROW)[0].starts_at
    second = _blocks(app, "la-b", TOMORROW)[0].starts_at
    assert first != second


def test_one_account_failing_does_not_stop_the_others(app):
    _schedule(app, account="la-a")
    _account(app, "la-b")
    app.execute(text("""
        INSERT INTO account_schedule
            (account_id, enabled, timezone, seed, start_jitter_minutes,
             end_jitter_minutes, created_at, updated_at)
        VALUES ('la-b', TRUE, 'Mars/Olympus', 5, 15, 20, :now, :now)
    """), {"now": NOW})
    app.execute(text("""
        INSERT INTO account_schedule_day
            (account_id, weekday, working, start_local, end_local,
             lunch_minutes, break_count, break_minutes)
        SELECT 'la-b', generate_series(1, 7), TRUE, TIME '09:00',
               TIME '17:30', 45, 2, 12
    """))
    app.execute(text("DELETE FROM account_shift_day WHERE account_id='la-a'"))

    result = materialise_all(app, now=NOW)

    assert result["la-a"] == HORIZON_DAYS
    assert "la-b" not in result
    assert _days(app, "la-b") == 0
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event
        WHERE account_id='la-b' AND kind='schedule_generation_failed'
    """)).scalar_one() == 1


def _edit(connection, *, start, end, now=NOW, enabled=True, jitter=15):
    write_schedule(connection, "la-a", enabled=enabled, timezone="Europe/London",
                   start_jitter_minutes=jitter, end_jitter_minutes=jitter,
                   days=_week(start=start, end=end), now=now)


def _works_at(connection, at):
    return connection.execute(text("""
        SELECT count(*) FROM account_shift_block
        WHERE account_id = 'la-a' AND kind = 'work' AND :at >= starts_at AND :at < ends_at
    """), {"at": at}).scalar_one() > 0


def test_an_edit_changes_today_and_tomorrow(app):
    _schedule(app)
    tomorrow = _blocks(app, on=TOMORROW)
    _edit(app, start=time(13, 0), end=time(19, 0), jitter=0)
    assert _blocks(app, on=TODAY)[0].starts_at.astimezone(UTC).time() == time(12, 0)  # 13:00 London in August
    assert _blocks(app, on=TOMORROW) != tomorrow


def test_today_uses_the_exact_saved_hours_and_later_days_keep_the_jitter(app):
    _schedule(app)
    _edit(app, start=time(10, 0), end=time(18, 0))
    today = _blocks(app, on=TODAY)
    assert today[0].starts_at == datetime(2026, 8, 14, 9, 0, tzinfo=UTC)    # 10:00 London
    assert today[-1].ends_at == datetime(2026, 8, 14, 17, 0, tzinfo=UTC)    # 18:00 London
    openings = {_blocks(app, on=date(2026, 8, day))[0].starts_at.time() for day in range(15, 20)}
    assert len(openings) > 1


def test_a_shift_saved_during_its_hours_works_at_once(app):
    _schedule(app, days=_week(start=time(14, 0), end=time(22, 0)))
    assert not _works_at(app, NOW)
    _edit(app, start=time(10, 0), end=time(18, 0))      # now is 10:00 London
    assert _works_at(app, NOW)


def test_a_shift_that_ended_stops_at_once(app):
    _schedule(app)
    _edit(app, start=time(14, 0), end=time(22, 0))
    assert not _works_at(app, NOW)


def test_turning_the_schedule_off_removes_today(app):
    _schedule(app)
    _edit(app, start=time(10, 0), end=time(18, 0), enabled=False)
    assert _blocks(app, on=TODAY) == []


def test_an_edit_ends_a_shift_of_yesterday_that_runs_past_midnight(app):
    _schedule(app, days=_week(start=time(9, 0), end=time(23, 45)))
    app.execute(text("""
        INSERT INTO account_shift_block (shift_day_id, account_id, ordinal, kind, starts_at, ends_at, pace)
        SELECT id, account_id, 90, 'work', :start, :end, 1 FROM account_shift_day
        WHERE account_id = 'la-a' AND local_date = :on
    """), {"start": datetime(2026, 8, 13, 20, 0, tzinfo=UTC), "end": datetime(2026, 8, 14, 23, 30, tzinfo=UTC),
           "on": TODAY})
    after = datetime(2026, 8, 14, 23, 10, tzinfo=UTC)        # 00:10 London on the 15th
    assert _works_at(app, after)
    _edit(app, start=time(9, 0), end=time(17, 0), now=after)
    assert not _works_at(app, after)


def test_a_schedule_round_trips(app):
    _schedule(app)
    value = read_schedule(app, "la-a")
    assert value["enabled"] is True
    assert value["timezone"] == "Europe/London"
    assert [day["weekday"] for day in value["days"]] == [1, 2, 3, 4, 5, 6, 7]
    assert value["days"][0]["start_local"] == time(9, 0)


def test_an_account_without_a_schedule_reads_as_none(app):
    _account(app)
    assert read_schedule(app, "la-a") is None


def test_the_seed_survives_an_edit(app):
    """Otherwise every edit would reshuffle days the operator never touched."""
    _schedule(app)
    seed = read_schedule(app, "la-a")["seed"]
    write_schedule(app, "la-a", enabled=True, timezone="Europe/London",
                   start_jitter_minutes=5, end_jitter_minutes=5,
                   days=_week(), now=NOW)
    assert read_schedule(app, "la-a")["seed"] == seed
