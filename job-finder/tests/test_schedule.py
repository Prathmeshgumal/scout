import zoneinfo
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from job_finder.schedule import (
    Rule,
    can_start_late,
    every_text,
    known_zone,
    local_zone,
    newest_due,
    next_slot,
    parse_every,
    slot_text,
)

KOL = "Asia/Kolkata"
WEEKDAYS = [1, 2, 3, 4, 5]  # 8 Oct 2026 is a Thursday


def at(text: str, tz: str = KOL) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(tz))


def local(when: datetime, tz: str = KOL) -> str:
    return when.astimezone(ZoneInfo(tz)).strftime("%a %d %H:%M")


def rule(unit="hour", count=1, days=WEEKDAYS, start=time(9), end=time(18), tz=KOL) -> Rule:
    return Rule(unit, count, days, start, end, tz)


def plan(r: Rule, first: str, n: int, tz: str = KOL) -> list[str]:
    """The next n goals after a first goal, when each goal starts on time."""
    first_at = last = at(first, tz)
    out = []
    for _ in range(n):
        last = next_slot(r, first_at, last, last)
        out.append(local(last, tz))
    return out


def test_parse_every():
    assert parse_every("6h") == ("hour", 6) and parse_every(3) == ("hour", 3)
    assert parse_every("1d") == ("day", 1) and parse_every(" 24H ") == ("day", 1) and parse_every("48h") == ("day", 2)
    assert parse_every("1w") == ("day", 7) and parse_every("1m") == ("month", 1)
    for bad in ("0h", "25h", "400d", "13m", "weekly", "", "1.5h"):
        with pytest.raises(ValueError):
            parse_every(bad)


def test_every_text():
    assert [every_text(*parse_every(v)) for v in ("1h", "6h", "1d", "2d", "1w", "2w", "1m", "3m")] == [
        "every hour", "every 6 hours", "each day", "every 2 days", "each week", "every 2 weeks", "each month",
        "every 3 months"]


def test_hourly_from_the_first_goal_then_from_first_goal_at_on_the_next_day():
    # The example of the user: created at 14:30, every hour, 09:00 to 18:00.
    assert plan(rule(), "2026-10-08T14:30", 6) == [
        "Thu 08 15:30", "Thu 08 16:30", "Thu 08 17:30", "Fri 09 09:00", "Fri 09 10:00", "Fri 09 11:00"]


def test_hourly_skips_the_days_that_are_not_active():
    assert plan(rule(), "2026-10-09T17:30", 1) == ["Mon 12 09:00"]


def test_every_five_hours_inside_the_window():
    assert plan(rule(count=5), "2026-10-08T14:30", 3) == ["Fri 09 09:00", "Fri 09 14:00", "Mon 12 09:00"]


def test_a_first_goal_before_the_window_waits_for_the_window():
    assert plan(rule(), "2026-10-08T07:30", 2) == ["Thu 08 09:30", "Thu 08 10:30"]


def test_no_window_end_means_midnight():
    assert plan(rule(end=None), "2026-10-08T22:30", 2) == ["Thu 08 23:30", "Fri 09 09:00"]


def test_start_a_goal_now_restarts_the_rest_of_the_day():
    r, first = rule(), at("2026-10-08T09:00")
    assert local(next_slot(r, first, at("2026-10-08T13:40"), at("2026-10-08T13:40"))) == "Thu 08 14:40"


def test_daily_at_first_goal_at_on_active_days():
    # 30 jobs each day: Thu now, then Fri, Mon, Tue at 09:00.
    assert plan(rule("day", 1), "2026-10-08T14:30", 3) == ["Fri 09 09:00", "Mon 12 09:00", "Tue 13 09:00"]


def test_every_two_days_moves_off_days_and_keeps_the_count():
    # Sat 10 moves to Mon 12. Then Wed 14, Fri 16. Sun 18 moves to Mon 19. Then Tue 20.
    assert plan(rule("day", 2), "2026-10-08T14:30", 5) == [
        "Mon 12 09:00", "Wed 14 09:00", "Fri 16 09:00", "Mon 19 09:00", "Tue 20 09:00"]


def test_weekly():
    assert plan(rule("day", 7), "2026-10-08T14:30", 2) == ["Thu 15 09:00", "Thu 22 09:00"]
    # Days = Mon only: each planned Thursday moves to the next Monday.
    assert plan(rule("day", 7, days=[1]), "2026-10-08T14:30", 2) == ["Mon 19 09:00", "Mon 26 09:00"]


def test_monthly_keeps_the_day_number_and_moves_off_days():
    r = rule("month", 1)
    first = at("2026-10-08T14:30")
    nov = next_slot(r, first, first, first)
    assert nov.astimezone(ZoneInfo(KOL)).strftime("%a %d %b %H:%M") == "Mon 09 Nov 09:00"  # 8 Nov is a Sunday
    dec = next_slot(r, first, nov, nov)
    assert dec.astimezone(ZoneInfo(KOL)).strftime("%a %d %b") == "Tue 08 Dec"  # the count does not move


def test_monthly_from_the_31st_uses_the_last_day_of_a_short_month():
    r = rule("month", 1, days=[1, 2, 3, 4, 5, 6, 7])
    first = at("2027-01-31T10:00")
    assert next_slot(r, first, first, first).astimezone(ZoneInfo(KOL)).strftime("%d %b") == "28 Feb"


def test_a_pause_of_many_days_gives_the_next_planned_time():
    r, first = rule("day", 2), at("2026-10-08T14:30")
    assert local(next_slot(r, first, first, at("2026-11-02T12:00"))) == "Tue 03 09:00"


def test_one_catch_up_for_the_newest_missed_time_on_its_day():
    r, first = rule(), at("2026-10-08T09:00")
    # The server was off from 15:10 to 16:40. The goals of 15:00 and 16:00 were missed: only 16:00 starts.
    due = newest_due(r, first, at("2026-10-08T14:00"), at("2026-10-08T15:00"), at("2026-10-08T16:40"))
    assert local(due) == "Thu 08 16:00" and can_start_late(r, due, at("2026-10-08T16:40"))


def test_no_late_goal_after_the_window_or_on_an_other_day():
    r = rule()
    assert not can_start_late(r, at("2026-10-08T17:00"), at("2026-10-08T18:10"))
    assert not can_start_late(r, at("2026-10-08T17:00"), at("2026-10-09T08:00"))
    daily = rule("day", 1)
    assert can_start_late(daily, at("2026-10-09T09:00"), at("2026-10-09T20:00"))  # days: until midnight
    assert not can_start_late(daily, at("2026-10-09T09:00"), at("2026-10-10T08:00"))


def test_the_slot_stays_at_local_time_across_the_end_of_summer_time():
    # Europe/Berlin leaves summer time on Sun 25 Oct 2026.
    berlin = "Europe/Berlin"
    assert plan(rule("day", 1, tz=berlin), "2026-10-23T09:00", 1, berlin) == ["Mon 26 09:00"]


def test_the_result_is_in_utc_and_needs_an_active_day():
    first = at("2026-10-08T12:00")
    slot = next_slot(rule(), first, first, first)
    assert slot.tzinfo == UTC and slot == datetime(2026, 10, 8, 7, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        next_slot(rule(days=[]), first, first, first)


def test_slot_text():
    assert slot_text(datetime(2026, 10, 8, 3, 30, tzinfo=UTC), KOL) == "Thu 8 Oct 2026 09:00"


def test_the_time_zone_is_the_one_of_this_machine(tmp_path):
    zoneinfo = tmp_path / "usr" / "share" / "zoneinfo" / "Asia"
    zoneinfo.mkdir(parents=True)
    (zoneinfo / "Kolkata").write_text("")
    link = tmp_path / "localtime"
    link.symlink_to(zoneinfo / "Kolkata")
    assert local_zone({}, link) == "Asia/Kolkata"
    assert local_zone({"TZ": "Europe/Berlin"}, link) == "Europe/Berlin"  # TZ wins, as for other programs
    assert local_zone({"TZ": "not a zone"}, tmp_path / "missing") == "UTC"


def test_a_known_zone_is_a_zone_name_of_the_tz_database():
    assert known_zone(" Asia/Kolkata ") == "Asia/Kolkata"
    for bad in (None, "", "Mars/Base", "../etc/passwd", "Asia/"):
        assert known_zone(bad) is None


@pytest.fixture
def no_system_zones():
    """A machine without the zone files of the system, such as the slim Docker image: it has no old zone names."""
    zoneinfo.reset_tzpath(to=[])
    ZoneInfo.clear_cache()
    yield
    zoneinfo.reset_tzpath()
    ZoneInfo.clear_cache()


def test_the_old_zone_names_of_browsers_are_known_on_each_machine(no_system_zones):
    assert known_zone("Asia/Calcutta") == "Asia/Calcutta"  # Chrome gives this name for India
    assert slot_text(datetime(2026, 10, 14, 3, 30, tzinfo=UTC), "Asia/Calcutta") == "Wed 14 Oct 2026 09:00"
