"""Natural pointer behavior without a live browser."""

import random

import pytest

from career_lens.capture import interaction


class Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y):
        self.page.log.append(("move", x, y))

    def wheel(self, dx, dy):
        self.page.log.append(("wheel", dx, dy))
        if self.page.wheel_moves:
            self.page.scroll_y = max(0, self.page.scroll_y + dy)


class Page:
    def __init__(self, *, width=1200, height=900):
        self.log = []
        self.scroll_y = 0
        self.mouse = Mouse(self)
        self.width = width
        self.height = height
        self.wheel_moves = True

    def evaluate(self, script):
        if script == interaction.VIEWPORT:
            return {"w": self.width, "h": self.height}
        if script == interaction.PAGE_SCROLL_STATE:
            if self.scroll_y <= 2:
                return None
            return {"scrollTop": self.scroll_y, "left": 0, "top": 0,
                    "right": self.width, "bottom": self.height}
        raise AssertionError(script)


class Locator:
    def __init__(self, page, box, context=None):
        self.page = page
        self.box = box
        self.context = context

    def bounding_box(self, **kwargs):
        self.page.log.append(("box", kwargs))
        return self.box | {"y": self.box["y"] - self.page.scroll_y}

    def evaluate(self, script, arg=None):
        assert script == interaction.SCROLL_CONTEXT
        return self.context or {
            "left": 0, "top": 0,
            "right": self.page.width, "bottom": self.page.height,
        }

    def click(self, **kwargs):
        self.page.log.append(("click", kwargs))
        return "clicked"


def test_trajectory_curves_and_ends_at_the_requested_point():
    start, end = (20.0, 30.0), (900.0, 500.0)
    points = interaction.trajectory(start, end, rng=random.Random(4))
    assert points[-1] == end
    assert len(points) > interaction.MIN_POINTS
    assert any(abs((x - start[0]) * (end[1] - start[1])
                   - (y - start[1]) * (end[0] - start[0])) > 1
               for x, y in points[:-1])


def test_click_scrolls_then_moves_to_a_varied_safe_point(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    target = Locator(page, {"x": 500, "y": 1800, "width": 100, "height": 40})

    assert interaction.click(
        page, target, timeout=5000, rng=random.Random(10)) == "clicked"

    click = page.log[-1][1]
    assert interaction.CLICK_HOLD_MS[0] <= click["delay"] <= interaction.CLICK_HOLD_MS[1]
    assert 22 <= click["position"]["x"] <= 78
    assert 10 <= click["position"]["y"] <= 30
    assert any(event[0] == "wheel" and event[2] > 0 for event in page.log)
    assert max(i for i, event in enumerate(page.log) if event[0] == "wheel") < (
        max(i for i, event in enumerate(page.log) if event[0] == "move"))


def test_click_scrolls_inside_a_narrow_target_ancestor(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page(width=760, height=540)
    target = Locator(
        page,
        {"x": 42, "y": 900, "width": 180, "height": 32},
        {"left": 20, "top": 70, "right": 260, "bottom": 500},
    )

    assert interaction.click(
        page, target, timeout=5000, rng=random.Random(14)) == "clicked"

    moves = [event for event in page.log if event[0] == "move"]
    assert moves
    assert all(20 <= event[1] <= 260 and 70 <= event[2] <= 500
               for event in moves[-2:])


def test_stalled_target_scroll_reports_the_scroll_owner(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page(width=760, height=540)
    page.wheel_moves = False
    target = Locator(
        page,
        {"x": 42, "y": 900, "width": 180, "height": 32},
        {
            "left": 20, "top": 70, "right": 740, "bottom": 540,
            "owner": {
                "selector": "main#workspace", "overflowY": "hidden",
                "scrollTop": 1200, "scrollHeight": 4200,
                "clientHeight": 820,
            },
        },
    )

    with pytest.raises(RuntimeError) as raised:
        interaction.scroll_to_target(
            page, target, timeout=5000, rng=random.Random(14))

    detail = str(raised.value)
    assert "wheel scrolling did not move the target" in detail
    assert "owner=main#workspace" in detail
    assert "scroll=1200/3380" in detail


def test_scroll_uses_a_point_verified_to_belong_to_the_scroll_owner(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page(width=1920, height=961)
    page.scroll_y = 2166
    interaction.move_to(page, 416, 74, rng=random.Random(3))
    page.log.clear()
    target = Locator(
        page,
        {"x": 388.5, "y": 2202.25, "width": 792, "height": 48},
        {
            "left": 0, "top": 52, "right": 1920, "bottom": 961,
            "owner": {
                "selector": "main#workspace", "overflowY": "scroll",
                "scrollTop": 2166, "scrollHeight": 3630,
                "clientHeight": 909,
            },
            "wheelPoint": {"x": 416, "y": 122},
        },
    )

    box = interaction.scroll_to_target(
        page, target, timeout=5000, rng=random.Random(4))

    assert box is not None
    first_wheel = next(
        index for index, event in enumerate(page.log) if event[0] == "wheel")
    moves_before_wheel = [
        event for event in page.log[:first_wheel] if event[0] == "move"]
    assert moves_before_wheel
    assert moves_before_wheel[-1][1:] == (416.0, 122.0)


def test_click_in_place_moves_to_fixed_control_without_scrolling(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    target = Locator(
        page, {"x": 1100, "y": 5.5, "width": 48, "height": 40})

    assert interaction.click_in_place(
        page, target, timeout=5000, rng=random.Random(12)) == "clicked"

    assert not any(event[0] == "wheel" for event in page.log)
    click = page.log[-1][1]
    assert 11 <= click["position"]["x"] <= 37
    assert 10 <= click["position"]["y"] <= 30
    assert any(event[0] == "move" for event in page.log)


def test_click_in_place_can_leave_navigation_waiting_to_the_caller(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    target = Locator(
        page, {"x": 1100, "y": 5.5, "width": 48, "height": 40})

    interaction.click_in_place(
        page, target, timeout=5000, no_wait_after=True,
        rng=random.Random(13))

    box = next(event[1] for event in page.log if event[0] == "box")
    assert "no_wait_after" not in box
    assert page.log[-1][1]["no_wait_after"] is True


def test_pointer_state_is_reused_instead_of_jumping_to_center(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    interaction.move_to(page, 900, 700, rng=random.Random(8))
    page.log.clear()

    assert interaction.move_into(
        page, (100, 100, 500, 500), fallback=(300, 300),
        rng=random.Random(9)) is True
    assert page.log[-1] == ("move", 488.0, 488.0)


def test_return_to_top_uses_upward_wheel_events(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    page.scroll_y = 1800

    assert interaction.scroll_to_top(page, rng=random.Random(11)) is True
    assert page.scroll_y == 0
    assert all(event[2] < 0 for event in page.log if event[0] == "wheel")


def test_idle_pointer_movement_does_not_initialize_an_unknown_pointer(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()

    assert interaction.maybe_move_pointer(
        page, (0, 0, 1200, 900), chance=1.0,
        rng=random.Random(7)) is False
    assert page.log == []


def test_idle_pointer_movement_respects_its_probability(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    interaction.move_to(page, 400, 300, rng=random.Random(8))
    page.log.clear()

    assert interaction.maybe_move_pointer(
        page, (0, 0, 1200, 900), chance=0.0,
        rng=random.Random(9)) is False
    assert page.log == []


def test_idle_pointer_movement_continues_within_the_supplied_bounds(monkeypatch):
    monkeypatch.setattr(interaction.time, "sleep", lambda _seconds: None)
    page = Page()
    interaction.move_to(page, 400, 300, rng=random.Random(10))
    page.log.clear()

    assert interaction.maybe_move_pointer(
        page, (100, 100, 700, 600), chance=1.0,
        rng=random.Random(11)) is True

    moves = [event for event in page.log if event[0] == "move"]
    assert moves
    assert 112 <= moves[-1][1] <= 688
    assert 112 <= moves[-1][2] <= 588
    assert moves[-1][1:] != (400.0, 300.0)


def _sampled(box, *, aim=None, page=None, count=600):
    """Where a click would land inside `box`, many times over."""
    rng = random.Random(11)
    points = []
    for _ in range(count):
        keywords = {"rng": rng}
        if aim is not None:
            keywords["aim"] = aim
        if page is not None:
            keywords["bias"] = interaction.aiming_bias(page, rng=rng)
        _, _, local_x, local_y = interaction._click_point(box, **keywords)
        points.append((local_x, local_y))
    return points


def test_the_default_aim_is_exactly_what_it_has_always_been():
    """LinkedIn's whole click surface runs on this; it must not move."""
    box = {"x": 0.0, "y": 0.0, "width": 360.0, "height": 48.0}

    points = _sampled(box)

    assert all(abs(x - 180.0) <= interaction.MAX_CLICK_OFFSET_X_PX
               for x, _ in points)
    assert all(abs(y - 24.0) <= interaction.MAX_CLICK_OFFSET_Y_PX
               for _, y in points)


def test_a_loose_aim_uses_the_control_instead_of_its_exact_centre():
    box = {"x": 0.0, "y": 0.0, "width": 360.0, "height": 48.0}

    points = _sampled(box, aim=interaction.LOOSE_AIM)

    span = max(x for x, _ in points) - min(x for x, _ in points)
    assert span > 120.0, f"a 360px control still only used {span:.0f}px"


def test_a_loose_aim_never_lands_on_a_control_edge():
    """Spread is not worth a click that misses, or that hits a neighbour."""
    box = {"x": 0.0, "y": 0.0, "width": 360.0, "height": 48.0}

    points = _sampled(box, aim=interaction.LOOSE_AIM)

    margin = interaction.LOOSE_AIM.margin_px
    assert all(margin <= x <= 360.0 - margin for x, _ in points)
    assert all(margin <= y <= 48.0 - margin for _, y in points)


def test_a_narrow_control_is_still_aimed_at_safely():
    """The margin, not the ratio, is what a small control is protected by."""
    box = {"x": 0.0, "y": 0.0, "width": 18.0, "height": 14.0}

    points = _sampled(box, aim=interaction.LOOSE_AIM)

    assert all(0.0 < x < 18.0 for x, _ in points)
    assert all(0.0 < y < 14.0 for _, y in points)


def test_one_browser_aims_with_one_hand():
    """A person's aim is consistently off-centre, not re-rolled every click."""
    first, second = Page(), Page()
    rng = random.Random(3)

    assert interaction.aiming_bias(first, rng=rng) == interaction.aiming_bias(
        first, rng=rng)
    assert interaction.aiming_bias(first, rng=rng) != interaction.aiming_bias(
        second, rng=rng)


def test_the_hand_moves_where_the_cluster_sits():
    """The bias shifts the whole distribution, not one click within it."""
    box = {"x": 0.0, "y": 0.0, "width": 360.0, "height": 48.0}
    left = _sampled(box, aim=interaction.LOOSE_AIM, page=None)
    biased = [interaction._click_point(
        box, rng=random.Random(11), aim=interaction.LOOSE_AIM,
        bias=(1.0, 1.0))[2] for _ in range(600)]

    assert (sum(biased) / len(biased)
            > sum(x for x, _ in left) / len(left) + 5.0)


def _slept(page, monkeypatch, **keywords):
    """Total time a move spends between its points."""
    naps = []
    monkeypatch.setattr(interaction.time, "sleep", naps.append)
    interaction.move_to(page, 900.0, 500.0, rng=random.Random(7), **keywords)
    return sum(naps)


def test_a_slower_pace_takes_proportionally_longer(monkeypatch):
    """A wander is not a click approach; it needs to travel more slowly."""
    ordinary = _slept(Page(), monkeypatch)
    slow = _slept(Page(), monkeypatch, pace=3.0)

    assert ordinary > 0
    assert 2.7 < slow / ordinary < 3.3


def test_the_default_pace_is_unchanged(monkeypatch):
    assert _slept(Page(), monkeypatch) == _slept(Page(), monkeypatch, pace=1.0)
