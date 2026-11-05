from datetime import UTC, date, datetime, time

import pytest

from job_finder.mandate_store import MandateStore
from job_finder.mandates import (
    MandateError,
    change_table,
    clean,
    config_table,
    create_mandate,
    describe,
    kickoff_instruction,
    kickoff_message,
    new_settings,
    preview_change,
    set_mandate_status,
    start_goal_now,
    update_mandate,
)

NOW = datetime(2026, 10, 8, 6, 30, tzinfo=UTC)  # Thu 8 Oct 2026 12:00 in Asia/Kolkata


@pytest.fixture
def mandates(pool) -> MandateStore:
    return MandateStore(pool)


@pytest.fixture
def settings(app_settings, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")  # the time zone of this machine
    return app_settings


async def notices(store, goal_id):
    return [e for e in await store.list_events(goal_id) if e["type"] == "notice"]


async def new(store, mandates, recorder, settings, **changes):
    home = await store.create_goal("Find EU fintech jobs every weekday")
    return await create_mandate(mandates, recorder, home["id"],
                                {"instruction": "Series B fintechs in the EU", "jobs_per_run": 20, **changes},
                                settings, NOW)


def test_clean_reads_user_values_and_refuses_bad_ones():
    assert clean({"every": "6h", "window_start": "09:00", "window_end": "21:00", "jobs_per_run": "100",
                  "max_spend_per_run": "2.5", "window_days": "1,2,3", "ends_on": "2026-12-31"}) == {
        "every_unit": "hour", "every_count": 6, "window_start": time(9), "window_end": time(21), "jobs_per_run": 100,
        "max_spend_per_run": 2.5, "window_days": [1, 2, 3], "ends_on": date(2026, 12, 31)}
    assert clean({"max_total_spend": None, "window_end": ""}) == {"max_total_spend": None, "window_end": None}
    assert clean({"timezone": " Europe/Berlin "}) == {"timezone": "Europe/Berlin"}
    for bad in ({"every": "30h"}, {"every": "400d"}, {"every_unit": "week"}, {"jobs_per_run": 0},
                {"max_spend_per_run": 0}, {"window_days": []},
                {"window_days": [8]}, {"timezone": "Mars/Base"}, {"window_start": "25:00"}, {"instruction": " "},
                {"colour": "red"}, {"max_scrapes_per_run": -1}):
        with pytest.raises(MandateError):
            clean(bad)


def test_clean_reads_the_time_limit_in_minutes_or_in_hours_and_minutes():
    assert clean({"time_limit_minutes": "90", "grace_minutes": 0}) == {"time_limit_minutes": 90, "grace_minutes": 0}
    assert clean({"time_limit_minutes": "2h", "grace_minutes": "45m"}) == {"time_limit_minutes": 120,
                                                                           "grace_minutes": 45}
    assert clean({"time_limit_minutes": "1h 30m"}) == {"time_limit_minutes": 90}
    assert clean({"time_limit_minutes": " 1.5h "}) == {"time_limit_minutes": 90}
    for bad in ({"time_limit_minutes": "4"}, {"time_limit_minutes": "soon"}, {"grace_minutes": -1},
                {"time_limit_minutes": None}, {"grace_minutes": "2 days"}):
        with pytest.raises(MandateError):
            clean(bad)


async def test_create_uses_the_defaults_and_starts_the_first_run_now(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    assert (m["every_unit"], m["every_count"], m["window_days"], m["window_start"], m["window_end"]) == (
        "day", 1, [1, 2, 3, 4, 5, 6, 7], time(9), None)
    assert (m["timezone"], m["max_spend_per_run"], m["max_scrapes_per_run"], m["max_total_spend"]) == (
        "Asia/Kolkata", 2.0, 15, 10.0)
    assert (m["time_limit_minutes"], m["grace_minutes"]) == (120, 30)
    assert m["title"] == "Series B fintechs in the EU" and m["next_run_at"] == NOW
    [note] = await notices(store, m["home_goal_id"])
    assert note["trigger"] == "mandate_created" and note["text"].startswith("Schedule created: ")
    assert "This chat now manages the schedule" in note["text"] and "Schedules tab of the left panel" in note["text"]
    assert note["text"].endswith("To change, pause, resume or end the schedule, ask here in this chat.")


async def test_create_refuses_a_window_that_ends_before_it_starts(store, mandates, recorder, settings):
    with pytest.raises(MandateError, match="No missions after"):
        await new(store, mandates, recorder, settings, window_start="21:00", window_end="09:00")


async def test_update_keeps_the_other_fields_and_records_the_change(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    await update_mandate(mandates, recorder, m["id"], {"jobs_per_run": 100}, "agent", NOW)
    m = await update_mandate(mandates, recorder, m["id"], {"max_spend_per_run": 3}, "agent", NOW)
    assert m["jobs_per_run"] == 100 and m["max_spend_per_run"] == 3.0
    changed = [n for n in await notices(store, m["home_goal_id"]) if n["trigger"] == "mandate_changed"]
    assert [n["source"] for n in changed] == ["agent", "agent"]
    assert changed[1]["old"] == {"max_spend_per_run": 2.0} and changed[1]["new"] == {"max_spend_per_run": 3.0}
    assert changed[1]["text"] == "Schedule changed by the agent: spend cap for each mission $2.00 → $3.00."


async def test_a_schedule_change_moves_the_next_run_to_the_next_slot(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    m = await update_mandate(mandates, recorder, m["id"], {"every": "6h"}, "agent", NOW)
    assert m["next_run_at"] == NOW  # no goal yet: the first goal starts at once
    await mandates.create_run(m, NOW, 2.0, 1)
    m = await update_mandate(mandates, recorder, m["id"], {"every": "6h", "window_end": "21:00"}, "agent", NOW)
    assert m["next_run_at"] == datetime(2026, 10, 8, 12, 30, tzinfo=UTC)  # 18:00 in Kolkata: 6 hours after 12:00
    changed = [n for n in await notices(store, m["home_goal_id"]) if n["trigger"] == "mandate_changed"]
    assert changed[0]["text"] == "Schedule changed by the agent: interval each day → every 6 hours."


async def test_two_changes_at_the_same_time_keep_each_other(store, mandates, recorder, settings):
    import asyncio
    m = await new(store, mandates, recorder, settings)
    await asyncio.gather(*(update_mandate(mandates, recorder, m["id"], {key: value}, "agent", NOW)
                           for key, value in (("jobs_per_run", 7), ("max_spend_per_run", 3), ("title", "New"))))
    m = await mandates.get(m["id"])
    assert (m["jobs_per_run"], m["max_spend_per_run"], m["title"]) == (7, 3.0, "New")


async def test_a_stale_write_is_refused(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    await mandates.update(m["id"], {"jobs_per_run": 7})
    assert await mandates.update(m["id"], {"jobs_per_run": 9}, seen=m["updated_at"]) is None
    assert not await mandates.claim(m, NOW)
    assert (await mandates.get(m["id"]))["jobs_per_run"] == 7


async def test_a_quota_change_reaches_the_run_in_progress(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    run = await mandates.create_run(m, NOW, 2.0, 1)
    await update_mandate(mandates, recorder, m["id"], {"jobs_per_run": 7}, "agent", NOW)
    assert (await store.get_goal(run["id"]))["brief"]["target_count"] == 7


async def test_a_time_limit_change_reaches_the_run_in_progress(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    run = await mandates.create_run(m, NOW, 2.0, 1)
    changed = await update_mandate(mandates, recorder, m["id"], {"time_limit_minutes": "3h", "grace_minutes": 10},
                                   "agent", NOW)
    assert (changed["time_limit_minutes"], changed["grace_minutes"]) == (180, 10)
    goal = await store.get_goal(run["id"])
    assert (goal["time_limit_s"], goal["grace_s"]) == (180 * 60, 10 * 60)
    [*_, note] = await notices(store, m["home_goal_id"])
    assert "time limit for each mission 2h → 3h" in note["text"] and "extra time to finish 30m → 10m" in note["text"]


async def test_pause_resume_and_end(store, mandates, recorder, settings):
    # Review focus 5
    m = await new(store, mandates, recorder, settings)
    await mandates.create_run(m, NOW, 2.0, 1)  # the first goal, Thu 12:00
    m = await set_mandate_status(mandates, recorder, m["id"], "paused", "agent", NOW)
    assert m["status"] == "paused"
    later = datetime(2026, 10, 12, 6, 30, tzinfo=UTC)  # Mon 12:00: the slots of Fri and Mon 09:00 were missed
    m = await set_mandate_status(mandates, recorder, m["id"], "active", "agent", later)
    assert m["status"] == "active" and m["next_run_at"] == datetime(2026, 10, 13, 3, 30, tzinfo=UTC)
    m = await set_mandate_status(mandates, recorder, m["id"], "ended", "agent", later)
    with pytest.raises(MandateError, match="ended"):
        await update_mandate(mandates, recorder, m["id"], {"jobs_per_run": 5}, "agent", later)
    with pytest.raises(MandateError, match="ended"):
        await set_mandate_status(mandates, recorder, m["id"], "active", "agent", later)


async def test_describe(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings, every="6h", window_end="21:00", window_days=[1, 2, 3, 4, 5],
                  max_total_spend=None)
    assert describe(m) == (
        'Schedule "Series B fintechs in the EU": Series B fintechs in the EU. '
        "One mission every 6 hours, Mon, Tue, Wed, Thu, Fri, 09:00 to 21:00. "
        "Quota: 20 fit roles for each mission. Caps for each mission: $2.00 and 15 scrapes. "
        "Time limit for each mission: 2h, then 30m to finish. "
        "No total spend limit. No end date. Status: active. Next mission: Thu 8 Oct 2026 12:00.")
    assert "One mission every hour," in describe({**m, "every_unit": "hour", "every_count": 1})
    daily = describe({**m, "every_unit": "day", "every_count": 1})
    every_day = {**m, "every_unit": "day", "every_count": 1, "window_days": [1, 2, 3, 4, 5, 6, 7]}
    assert "One mission each day, at 09:00." in describe(every_day)
    assert "One mission each day, Mon, Tue, Wed, Thu, Fri, at 09:00." in daily
    assert "One mission every 2 weeks," in describe({**m, "every_unit": "day", "every_count": 14})
    assert "One mission each month," in describe({**m, "every_unit": "month", "every_count": 1})


def test_kickoff_message():
    from job_finder.mandates import kickoff_message
    m = {"title": "EU fintech jobs", "instruction": "Series B fintechs in the EU", "jobs_per_run": 20,
         "max_spend_per_run": 2.0, "max_scrapes_per_run": 15, "timezone": "Asia/Kolkata",
         "time_limit_minutes": 90, "grace_minutes": 30}
    history = {"runs": 5, "jobs": 112, "fit": 87, "not_fit": 14, "unsure": 11,
               "reasons": ["More than 500 employees.", "Not in the EU."]}
    text = kickoff_message(m, 6, datetime(2026, 10, 8, 3, 30, tzinfo=UTC), history)
    assert text == (
        'This is mission 6 of the schedule "EU fintech jobs", for the slot Thu 8 Oct 2026 09:00.\n'
        "Instruction: Series B fintechs in the EU\n"
        "Quota for this mission: 20 fit roles. Spend cap: $2.00. Scrape cap: 15. Time limit: 1h 30m.\n"
        "Earlier missions: 112 roles found, 87 fit, 14 not a fit, 11 unsure.\n"
        "Newest reasons for not_fit:\n- More than 500 employees.\n- Not in the EU.")
    first = kickoff_message(m, 1, datetime(2026, 10, 8, 3, 30, tzinfo=UTC),
                            {**history, "runs": 0, "jobs": 0, "reasons": []})
    assert first.endswith("Quota for this mission: 20 fit roles. Spend cap: $2.00. Scrape cap: 15. Time limit: 1h 30m.\n"
                          "This is the first mission of the schedule.")


def test_kickoff_message_keeps_each_reason_to_one_short_line():
    # The message must not grow with each goal: at most 3 reasons, each on one line.
    from job_finder.mandates import REASON_CHARS, kickoff_message
    m = {"title": "T", "instruction": "I", "jobs_per_run": 1, "max_spend_per_run": 2.0, "max_scrapes_per_run": 15,
         "timezone": "UTC", "time_limit_minutes": 120, "grace_minutes": 30}
    history = {"runs": 2, "jobs": 3, "fit": 1, "not_fit": 2, "unsure": 0, "reasons": ["Too big.\nSee " + "x" * 400]}
    [line] = kickoff_message(m, 3, datetime(2026, 10, 8, tzinfo=UTC), history).splitlines()[-1:]
    assert line.startswith("- Too big. See xxx") and len(line) == 2 + REASON_CHARS and line.endswith("…")


async def test_config_table_shows_every_setting(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings, every="6h", window_end="21:00", window_days=[1, 2, 3, 4, 5],
                  profile={"target_titles": {"value": ["Go | Rust\nengineer"], "basis": "stated"}})
    assert config_table(m) == "\n".join([
        "| Setting | Value |",
        "|---|---|",
        "| Title | Series B fintechs in the EU |",
        "| Instruction | Series B fintechs in the EU |",
        "| Profile | Titles: Go \\| Rust engineer |",
        "| Status | active |",
        "| Next mission | Thu 8 Oct 2026 12:00 |",
        "| Time zone | Asia/Kolkata |",
        "| Interval | every 6 hours |",
        "| Days | Mon, Tue, Wed, Thu, Fri |",
        "| First mission at | 09:00 |",
        "| No missions after | 21:00 |",
        "| Quota for each mission | 20 fit roles |",
        "| Spend cap for each mission | $2.00 |",
        "| Scrape cap for each mission | 15 |",
        "| Time limit for each mission | 2h |",
        "| Extra time to finish | 30m |",
        "| Total spend limit | $10.00 |",
        "| End date | none |"])
    assert "| Next mission | none: the schedule is paused |" in config_table({**m, "status": "paused"})


def test_new_settings_fill_in_the_defaults_and_save_nothing(settings):
    values = new_settings({"instruction": "Series B fintechs in the EU", "jobs_per_run": 20}, settings)
    assert values["title"] == "Series B fintechs in the EU" and values["every_unit"] == "day"
    assert values["max_total_spend"] == 10.0 and values["status"] == "active"
    with pytest.raises(MandateError, match="instruction"):
        new_settings({"jobs_per_run": 20}, settings)
    with pytest.raises(MandateError, match="No missions after"):
        new_settings({"instruction": "x", "window_start": "21:00", "window_end": "09:00"}, settings)


async def test_preview_change_saves_nothing(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    await mandates.create_run(m, NOW, 2.0, 1)
    now, after = await preview_change(mandates, m["id"], {"jobs_per_run": 50, "status": "paused"}, NOW)
    assert now["jobs_per_run"] == 20 and after["jobs_per_run"] == 50 and after["status"] == "paused"
    assert (await mandates.get(m["id"]))["jobs_per_run"] == 20
    assert change_table(now, after) == "\n".join([
        "| Setting | Now | After the change |",
        "|---|---|---|",
        "| Status | active | paused |",
        "| Next mission | Thu 8 Oct 2026 12:00 | none: the schedule is paused |",
        "| Quota for each mission | 20 fit roles | 50 fit roles |"])
    _, later = await preview_change(mandates, m["id"], {"every": "6h"}, NOW)
    assert later["next_run_at"] == datetime(2026, 10, 8, 12, 30, tzinfo=UTC)  # the next slot, as a save gives
    for bad, match in (({}, "one or more"), ({"status": "active"}, "active already"),
                       ({"status": "paused", "start_now": True}, "Only an active"), ({"every": "30h"}, "from 1 to 23")):
        with pytest.raises(MandateError, match=match):
            await preview_change(mandates, m["id"], bad, NOW)


async def test_start_goal_now(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    await mandates.update(m["id"], {"next_run_at": datetime(2030, 1, 1, tzinfo=UTC)})
    m = await start_goal_now(mandates, recorder, m["id"], "agent", NOW)
    assert m["next_run_at"] == NOW
    note = (await notices(store, m["home_goal_id"]))[-1]
    assert note["trigger"] == "mandate_changed" and note["text"] == (
        "Schedule changed by the agent: a new mission starts within a minute.")
    await set_mandate_status(mandates, recorder, m["id"], "paused", "agent", NOW)
    with pytest.raises(MandateError, match="Only an active"):
        await start_goal_now(mandates, recorder, m["id"], "agent", NOW)


async def test_describe_does_not_double_the_period(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings, instruction="Series B fintechs in the EU.")
    assert ": Series B fintechs in the EU. One mission" in describe(m)


def test_a_new_mandate_takes_the_total_spend_from_the_settings(settings):
    from dataclasses import replace
    values = new_settings({"instruction": "Series B fintechs in the EU"}, replace(settings, mandate_total_usd=40.0))
    assert values["max_total_spend"] == 40.0


def test_the_texts_for_the_agent_give_no_fixed_total_spend():
    from job_finder.options import MAIN_PROMPT
    from job_finder.tools.mandates import CREATE_SCHEMA
    assert "$10" not in MAIN_PROMPT
    assert "Default: 10" not in CREATE_SCHEMA["properties"]["max_total_spend"]["description"]



def test_a_new_mandate_takes_the_time_zone_of_the_user(settings):
    values = new_settings({"instruction": "Series B fintechs in the EU"}, settings, zone="America/New_York")
    assert values["timezone"] == "America/New_York"
    assert new_settings({"instruction": "x"}, settings)["timezone"] == "Asia/Kolkata"  # no zone: this machine's
    assert new_settings({"instruction": "x", "timezone": "Europe/Berlin"}, settings, zone="UTC")["timezone"] == \
        "Europe/Berlin"  # the user asked for a zone


async def test_a_new_time_zone_moves_the_next_goal(store, mandates, recorder, settings):
    m = await new(store, mandates, recorder, settings)
    await mandates.create_run(m, NOW, 2.0, 1)
    assert m["timezone"] == "Asia/Kolkata"
    m = await update_mandate(mandates, recorder, m["id"], {"timezone": "Europe/Berlin"}, "agent", NOW)
    assert m["next_run_at"] == datetime(2026, 10, 9, 7, 0, tzinfo=UTC)  # Fri 9 Oct 09:00 in Berlin
    table = config_table(m)
    assert "| Time zone | Europe/Berlin |" in table and "| Next mission | Fri 9 Oct 2026 09:00 |" in table
    note = (await notices(store, m["home_goal_id"]))[-1]
    assert "time zone Asia/Kolkata → Europe/Berlin" in note["text"]


def test_the_instruction_comes_back_from_the_first_message_of_a_run():
    m = {"title": "T", "instruction": "Line one.\nLine two.", "timezone": "UTC", "jobs_per_run": 3,
         "max_spend_per_run": 2.0, "max_scrapes_per_run": 15, "time_limit_minutes": 120}
    history = {"runs": 0, "jobs": 0, "fit": 0, "not_fit": 0, "unsure": 0, "reasons": []}
    assert kickoff_instruction(kickoff_message(m, 1, datetime(2026, 10, 8, 9, tzinfo=UTC), history)) == m["instruction"]
    assert kickoff_instruction("A notice with no instruction.") is None
