"""The day generator: a pure function of template, date and seeded RNG."""

from __future__ import annotations

from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from career_lens.fleet.schedules import (
    EDGE_EXCLUSION_MINUTES, PACE_MAX, PACE_MIN, DayTemplate, ScheduleError,
    day_rng, plan_day,
)

LONDON = ZoneInfo("Europe/London")
MONDAY = date(2026, 8, 17)


def _template(**overrides) -> DayTemplate:
    values = {
        "weekday": 1, "working": True, "start_local": time(9, 0),
        "end_local": time(17, 30), "lunch_minutes": 45, "break_count": 2,
        "break_minutes": 12,
    }
    return DayTemplate(**{**values, **overrides})


def _plan(template=None, *, on=MONDAY, seed=7, tz=LONDON, start_jitter=15,
          end_jitter=20):
    return plan_day(template or _template(), date=on, tz=tz,
                    rng=day_rng(seed, on), start_jitter_minutes=start_jitter,
                    end_jitter_minutes=end_jitter)


def test_a_day_off_has_no_blocks():
    assert _plan(_template(working=False, start_local=None, end_local=None,
                           lunch_minutes=0, break_count=0,
                           break_minutes=0)) == []


def test_blocks_tile_the_shift_without_gap_or_overlap():
    blocks = _plan()
    assert blocks
    for earlier, later in zip(blocks, blocks[1:]):
        assert earlier.ends_at == later.starts_at
    assert all(block.ends_at > block.starts_at for block in blocks)


def test_a_shift_opens_and_closes_with_work():
    blocks = _plan()
    assert blocks[0].kind == "work"
    assert blocks[-1].kind == "work"


def test_the_template_pauses_all_appear():
    blocks = _plan()
    kinds = [block.kind for block in blocks]
    assert kinds.count("break") == 2
    assert kinds.count("lunch") == 1
    for block in blocks:
        if block.kind == "break":
            assert (block.ends_at - block.starts_at).total_seconds() == 12 * 60


def test_a_template_without_pauses_is_one_unbroken_block():
    blocks = _plan(_template(lunch_minutes=0, break_count=0, break_minutes=0))
    assert [block.kind for block in blocks] == ["work"]


def test_no_pause_lands_against_either_end_of_the_shift():
    blocks = _plan()
    opening, closing = blocks[0].starts_at, blocks[-1].ends_at
    for block in blocks:
        if block.kind in ("break", "lunch"):
            assert (block.starts_at - opening).total_seconds() >= (
                EDGE_EXCLUSION_MINUTES * 60)
            assert (closing - block.ends_at).total_seconds() >= (
                EDGE_EXCLUSION_MINUTES * 60)


def test_lunch_moves_around_the_middle_of_the_shift():
    """A fixed lunch hour is the most recognisable thing about a robot.

    Measured against the shift's own midpoint, not its opening: the opening
    already drifts with jitter, so an offset from it varies even when lunch is
    pinned to the exact middle of every day.
    """
    offsets = set()
    for offset in range(20):
        blocks = _plan(on=MONDAY + timedelta(days=offset))
        lunch = next(block for block in blocks if block.kind == "lunch")
        opening, closing = blocks[0].starts_at, blocks[-1].ends_at
        midpoint = opening + (closing - opening) / 2
        offsets.add((lunch.starts_at - midpoint).total_seconds())
    assert len(offsets) > 5


def test_only_work_blocks_are_paced_and_every_pace_is_in_range():
    paces = []
    for block in _plan():
        if block.kind == "work":
            assert PACE_MIN <= float(block.pace) <= PACE_MAX
            paces.append(float(block.pace))
        else:
            assert block.pace is None
    assert len(set(paces)) > 1, "a day that never changes pace is uniform"


def test_the_day_finishes_slower_than_it_started():
    """The last stretch draws from the slower half, so pace is not flat."""
    first, last = [], []
    for offset in range(40):
        blocks = [b for b in _plan(on=date(2026, 8, 17), seed=offset)
                  if b.kind == "work"]
        first.append(float(blocks[0].pace))
        last.append(float(blocks[-1].pace))
    assert sum(last) / len(last) < sum(first) / len(first)


def _shape(blocks):
    """The day's shape, independent of which date it fell on.

    Comparing whole blocks across two dates proves nothing: the instants
    differ because the dates do, even if the generator ignored the date
    entirely. Offsets from the opening are what actually vary.
    """
    opening = blocks[0].starts_at
    return [(block.kind, (block.starts_at - opening).total_seconds(),
             (block.ends_at - block.starts_at).total_seconds(), block.pace)
            for block in blocks]


def test_the_same_account_and_date_produce_the_same_day():
    assert _plan() == _plan()


def test_a_different_date_produces_a_different_day():
    assert _shape(_plan(on=date(2026, 8, 17))) != _shape(
        _plan(on=date(2026, 8, 18)))


def test_a_different_account_produces_a_different_day():
    assert _shape(_plan(seed=1)) != _shape(_plan(seed=2))


def _opening_local(blocks, tz=LONDON) -> int:
    """Minutes past local midnight at which the shift began."""
    opening = blocks[0].starts_at.astimezone(tz)
    return opening.hour * 60 + opening.minute


def test_the_shift_starts_within_its_jitter_of_the_template():
    assert abs(_opening_local(_plan()) - 9 * 60) <= 15


def test_no_jitter_means_the_template_time_exactly():
    assert _opening_local(_plan(start_jitter=0, end_jitter=0)) == 9 * 60


def test_the_shift_does_not_start_at_the_same_minute_every_day():
    openings = {_opening_local(_plan(on=MONDAY + timedelta(days=offset)))
                for offset in range(20)}
    assert len(openings) > 5


@pytest.mark.parametrize("on", [date(2026, 3, 29), date(2026, 10, 25)])
def test_daylight_saving_days_still_produce_increasing_instants(on):
    blocks = _plan(_template(start_local=time(1, 0), end_local=time(6, 0),
                             lunch_minutes=0, break_count=1,
                             break_minutes=10), on=on, start_jitter=0,
                   end_jitter=0)
    for earlier, later in zip(blocks, blocks[1:]):
        assert earlier.ends_at == later.starts_at
    assert blocks[0].starts_at < blocks[-1].ends_at


def test_a_template_the_generator_cannot_satisfy_raises():
    with pytest.raises(ScheduleError):
        _plan(_template(start_local=time(9, 0), end_local=time(10, 0),
                        lunch_minutes=45, break_count=4, break_minutes=15))


def test_jitter_that_shrinks_a_tight_shift_too_far_raises():
    """The table constraint checks the nominal shift; jitter can undercut it.

    Which seeds shrink and which stretch is the generator's business, so this
    asserts over a fixed range rather than pinning one lucky seed.
    """
    tight = _template(start_local=time(9, 0), end_local=time(11, 30),
                      lunch_minutes=45, break_count=0, break_minutes=0)
    raised = 0
    for seed in range(50):
        try:
            _plan(tight, seed=seed, start_jitter=60, end_jitter=60)
        except ScheduleError:
            raised += 1
    assert raised, "jitter can shrink a shift below what its pauses need"
