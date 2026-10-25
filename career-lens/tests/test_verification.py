"""Clearing the screens a site puts in front of a page."""

from __future__ import annotations

import random

import pytest

from career_lens.capture import verification

WIDGET = "iframe[src*='challenges.cloudflare.com']"
BOX = {"x": 512.0, "y": 304.0, "width": 300.0, "height": 65.0}
CONTROL_BOX = {"x": 521.0, "y": 324.5, "width": 165.0, "height": 24.0}
CHALLENGE_TEXT = (
    "Performing security verification\n"
    "This website uses a security service to protect against malicious bots.")


class Response:
    def __init__(self, status: int) -> None:
        self.status = status


class Locator:
    def __init__(self, page, selector, *, matches=1, box=BOX, arrives=False):
        self.page = page
        self.selector = selector
        self.matches = matches
        self.box = box
        self.arrives = arrives

    @property
    def first(self):
        return self

    def count(self):
        return self.matches

    def is_visible(self):
        self.page.settle()
        return bool(self.page.widgets)

    def wait_for(self, *, state, timeout):
        self.page.waited_for_widget = timeout
        if not self.arrives:
            raise TimeoutError(f"no {self.selector} after {timeout}ms")
        self.matches = 1

    def bounding_box(self, **_kwargs):
        return self.box if self.matches else None

    def click(self, **kwargs):
        self.page.clicks.append((self.selector, kwargs))


class FrameLocator:
    def __init__(self, page, controls):
        self.page = page
        self.controls = controls

    def locator(self, selector):
        return Locator(self.page, f"frame:{selector}",
                       matches=1 if selector in self.controls else 0,
                       box=CONTROL_BOX)


class Mouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y):
        self.page.moves.append((x, y))

    def click(self, x, y, **kwargs):
        self.page.clicks.append((f"point:{x:.0f},{y:.0f}", kwargs))

    def wheel(self, *_args):
        pass


class Page:
    """Enough of a page to drive verification, and nothing else."""

    def __init__(self, *, title="Just a moment...", body=CHALLENGE_TEXT,
                 widgets=1, controls=("input[type='checkbox']",),
                 clears=True, late_widget=False):
        self.page_title = title
        self.body = body
        self.widgets = widgets
        self.controls = controls
        self.clears = clears
        self.late_widget = late_widget
        self.waited_for_widget = None
        self.clicks: list = []
        self.moves: list = []
        self.polls = 0
        self.waited_for = None
        self.mouse = Mouse(self)

    def title(self):
        return self.page_title

    def inner_text(self, _selector):
        return self.body

    def locator(self, selector):
        return Locator(self, selector, matches=self.widgets,
                       arrives=self.late_widget)

    def frame_locator(self, _selector):
        return FrameLocator(self, self.controls)

    def settle(self):
        """The challenge clears a poll after it is pressed, or never."""
        self.polls += 1
        if self.clicks and self.clears:
            self.widgets = 0
            self.body = "the real page " * 500

    def evaluate(self, script, arg=None):
        if script is verification.READY:
            return bool(self.body.strip())
        if "innerWidth" in script:
            return {"w": 1280, "h": 900}
        return None

    def wait_for_function(self, _script, *, arg, timeout):
        self.waited_for = timeout
        self.body = "the real page " * 500
        self.page_title = "Reviews"


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch):
    monkeypatch.setattr(verification.time, "sleep", lambda _s: None)
    monkeypatch.setattr(verification.interaction.time, "sleep", lambda _s: None)


def rng():
    return random.Random(7)


def test_a_page_with_no_challenge_keeps_its_response():
    page = Page(widgets=0, body="ordinary page")
    response = Response(200)

    assert verification.wait(page, response, rng=rng()) is response
    assert page.clicks == []


def test_the_checkbox_is_pressed_and_the_stale_response_discarded():
    page = Page()

    assert verification.wait(page, Response(403), rng=rng()) is None
    assert [selector for selector, _ in page.clicks] == [
        "frame:input[type='checkbox']"]


def test_the_pointer_travels_to_the_control_rather_than_jumping():
    page = Page()

    verification.wait(page, Response(403), rng=rng())

    # A curve, not a teleport, ending inside the control's own box.
    assert len(page.moves) > verification.interaction.MIN_POINTS
    x, y = page.moves[-1]
    assert CONTROL_BOX["x"] <= x <= CONTROL_BOX["x"] + CONTROL_BOX["width"]
    assert CONTROL_BOX["y"] <= y <= CONTROL_BOX["y"] + CONTROL_BOX["height"]


def test_an_unreachable_control_is_pressed_at_the_widgets_own_offset():
    # Turnstile's centre is the vendor's logo, so the fallback has to be
    # left-biased or it presses nothing.
    page = Page(controls=())

    assert verification.wait(page, Response(403), rng=rng()) is None
    selector, _ = page.clicks[0]
    assert selector == "point:542,336"


def test_a_challenge_that_does_not_clear_keeps_its_response():
    page = Page(clears=False)
    response = Response(403)

    assert verification.wait(page, response, timeout_ms=20,
                             rng=rng()) is response


def test_a_widget_on_a_readable_page_is_left_alone():
    # Turnstile guarding a site's own login form, on a page we can already
    # read. Pressing it would act on the site rather than on our way in.
    page = Page(body="real content " * 500)

    response = Response(200)
    assert verification.wait(page, response, rng=rng()) is response
    assert page.clicks == []


def test_an_automatic_screen_still_completes_on_its_own():
    page = Page(title="Just a moment...",
                body="Checking your browser before accessing", widgets=0)

    assert verification.wait(page, Response(403), rng=rng()) is None
    assert page.waited_for == verification.TIMEOUT_MS
    assert page.clicks == []


def test_an_automatic_screen_that_escalates_to_a_checkbox_is_pressed():
    # Cloudflare shows "checking your browser" first and puts up a checkbox
    # when watching was not enough. The wait clears, the widget remains.
    page = Page(title="Just a moment...",
                body="Checking your browser before accessing")

    def wait_for_function(_script, *, arg, timeout):
        page.waited_for = timeout          # the signs go, the widget stays

    page.wait_for_function = wait_for_function

    assert verification.wait(page, Response(403), rng=rng()) is None
    assert [selector for selector, _ in page.clicks] == [
        "frame:input[type='checkbox']"]


def test_a_widget_injected_after_the_navigation_is_still_found():
    # The document the navigation returned does not contain the widget: it is
    # written in afterwards, so one look is a look too early.
    page = Page(widgets=0, late_widget=True)

    assert verification.wait(page, Response(403), rng=rng()) is None
    assert page.waited_for_widget == verification.WIDGET_GRACE_MS
    assert [selector for selector, _ in page.clicks] == [
        "frame:input[type='checkbox']"]


def test_an_ordinary_page_never_waits_for_a_widget():
    # Every single-page app is empty at domcontentloaded, exactly like a
    # challenge. Without the status and wording gate they would all pay the
    # grace above on every capture.
    page = Page(widgets=0, late_widget=True, title="Reviews", body="")

    assert verification.wait(page, Response(200), rng=rng()) is not None
    assert page.waited_for_widget is None
