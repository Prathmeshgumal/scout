"""Waiting for a lazy list to finish loading.

A details page renders a first batch and appends the rest as the reader reaches
the bottom. Nothing here waits a fixed time: the pause between wheel flicks is
tempo-scaled (~0.08s under `research`, ~0.83s under `human`), so a sleep long
enough for one starves the other -- which is how a section announcing 22
publications was captured holding 10 and read as complete.

No test here touches a browser. The page is a fake whose behaviour each test
scripts.
"""

from __future__ import annotations

import time

import pytest

from career_lens.capture.page import (
    LIST_QUIET_POLLS, RequestsInFlight, is_loading, quiet, wait_until_settled)


class FakePage:
    """A page whose loading state and listeners the test drives."""

    def __init__(self, *, loading: bool = False, listens: bool = True):
        self.loading = loading
        self.listens = listens
        self.handlers: dict[str, list] = {}
        self.evaluations = 0

    def on(self, event: str, handler) -> None:
        if not self.listens:
            raise RuntimeError("this page does not take listeners")
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler) -> None:
        self.handlers.get(event, []).remove(handler)

    def evaluate(self, script, argument=None):
        self.evaluations += 1
        return self.loading

    def fire(self, event: str) -> None:
        for handler in self.handlers.get(event, []):
            handler(object())


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Exercise the polling state machine without production-sized pauses."""
    from career_lens.capture import page

    monkeypatch.setattr(page, "LIST_QUIET_POLL_S", 0.001)


# --- the rule ---------------------------------------------------------------

def test_nothing_happening_is_quiet():
    assert quiet(outstanding=0, grew=False, loading=False) is True


@pytest.mark.parametrize("outstanding,grew,loading", [
    (1, False, False),   # a batch is on the wire
    (0, True, False),    # entries are still landing
    (0, False, True),    # the page says it is fetching
])
def test_any_signal_alone_means_still_working(outstanding, grew, loading):
    # Three independent signals, because none covers the others: a list that
    # pages from data already delivered never touches the network, and a list
    # that renders no spinner still grows.
    assert quiet(outstanding, grew, loading) is False


# --- counting requests ------------------------------------------------------

def test_outstanding_counts_what_has_not_come_back():
    page = FakePage()
    watch = RequestsInFlight(page)

    page.fire("request")
    page.fire("request")
    assert watch.outstanding == 2

    page.fire("requestfinished")
    assert watch.outstanding == 1
    page.fire("requestfailed")
    assert watch.outstanding == 0


def test_a_failed_request_is_not_outstanding_forever():
    # A request that errors never fires `requestfinished`. Counting only
    # completions would leave the watch permanently busy and every wait would
    # run to its budget.
    page = FakePage()
    watch = RequestsInFlight(page)

    page.fire("request")
    page.fire("requestfailed")

    assert watch.outstanding == 0


def test_a_page_that_refuses_listeners_still_works():
    # The other two signals carry the wait; refusing to attach must not raise
    # in the middle of a capture.
    watch = RequestsInFlight(FakePage(listens=False))

    assert watch.attached is False
    assert watch.outstanding == 0
    watch.close()


def test_closing_detaches_every_handler():
    page = FakePage()
    watch = RequestsInFlight(page)
    watch.close()

    page.fire("request")

    assert watch.outstanding == 0
    assert all(not handlers for handlers in page.handlers.values())


# --- the loading indicator --------------------------------------------------

def test_a_page_that_cannot_run_script_is_treated_as_quiet():
    # Mid-navigation. Waiting forever on a page we cannot ask is worse than
    # trusting the other two signals.
    class Mute(FakePage):
        def evaluate(self, script, argument=None):
            raise RuntimeError("execution context destroyed")

    assert is_loading(Mute()) is False


# --- the wait ---------------------------------------------------------------

def test_a_settled_list_returns_as_soon_as_it_is_quiet():
    page = FakePage()
    sizes = iter([100] * 10)
    started = time.monotonic()

    size, why = wait_until_settled(
        page, RequestsInFlight(page), lambda: next(sizes), budget_s=5.0)

    assert why == "quiet"
    assert size == 100
    # Two polls, not the whole budget.
    assert time.monotonic() - started < 0.5


def test_a_growing_list_keeps_being_waited_on():
    page = FakePage()
    # Grows for the first few polls, then settles.
    sizes = iter([100, 200, 300, 400, 400, 400, 400, 400])

    size, why = wait_until_settled(
        page, RequestsInFlight(page), lambda: next(sizes), budget_s=5.0)

    assert why == "quiet"
    assert size == 400


def test_a_list_that_never_settles_gives_up_at_its_budget():
    # The hard stop. A list that will not finish returns what it has rather
    # than holding the run open -- the announced-count contract reports it.
    page = FakePage(loading=True)
    counter = iter(range(1000))
    started = time.monotonic()

    size, why = wait_until_settled(
        page, RequestsInFlight(page), lambda: next(counter), budget_s=0.02)

    assert why == "budget"
    assert 0.02 <= time.monotonic() - started < 0.5


def test_an_outstanding_request_holds_the_wait_open():
    page = FakePage()
    watch = RequestsInFlight(page)
    page.fire("request")           # never comes back
    sizes = iter([50] * 100)

    _, why = wait_until_settled(page, watch, lambda: next(sizes), budget_s=0.02)

    assert why == "budget"


def test_the_callers_ceiling_is_never_exceeded():
    # exhaust_list passes a deadline covering every pass. A single settle must
    # not run past it even when its own budget is larger.
    page = FakePage(loading=True)
    sizes = iter(range(1000))
    started = time.monotonic()

    _, why = wait_until_settled(
        page, RequestsInFlight(page), lambda: next(sizes),
        budget_s=30.0, deadline=time.monotonic() + 0.02)

    assert why == "budget"
    assert time.monotonic() - started < 0.5


def test_one_quiet_poll_is_not_enough():
    # A batch that has landed but not yet triggered the next fetch looks
    # exactly like a finished list, for about one poll.
    assert LIST_QUIET_POLLS >= 2


# --- the step that was missing ----------------------------------------------

class ListPage(FakePage):
    """A page whose column grows for a scripted number of scrolls.

    `scroll` is what `mouse.wheel` would drive; here it just advances the
    column size, which is all `exhaust_list` measures.
    """

    def __init__(self, sizes: list[int], **kwargs):
        super().__init__(**kwargs)
        self.sizes = sizes
        self.index = 0
        self.scrolls = 0
        self.url = "https://www.linkedin.com/in/someone/details/courses/"

    def size(self) -> int:
        return self.sizes[min(self.index, len(self.sizes) - 1)]

    def evaluate(self, script, argument=None):
        self.evaluations += 1
        if "innerText" in str(script) or "CONTENT" in str(script).upper():
            return self.size()
        return self.loading


def test_exhausting_a_list_runs_end_to_end():
    # The test that would have caught the NameError this shipped with:
    # `exhaust_list` was dead code, so a missing import never fired until it
    # was wired in. Asserting on source text cannot see that -- only calling it
    # can.
    from career_lens.linkedin import sections

    page = ListPage([100, 400, 700, 700, 700, 700, 700, 700])
    scrolled = []

    def fake_scroll(target, tempo=None, **kwargs):
        scrolled.append(tempo)
        page.index += 1

    original = sections.scroll_through
    sections.scroll_through = fake_scroll
    try:
        size = sections.exhaust_list(page, tempo=None)
    finally:
        sections.scroll_through = original

    assert size == 700
    assert scrolled, "the list was never scrolled"


def test_exhausting_a_list_passes_the_tempo_down():
    # The scroll inside exhaust_list took no tempo before, so it ran at full
    # speed whatever the caller asked for.
    from career_lens.linkedin import sections

    page = ListPage([50, 50])
    seen = []

    def fake_scroll(target, tempo=None, **kwargs):
        seen.append(tempo)
        page.index += 1

    original = sections.scroll_through
    sections.scroll_through = fake_scroll
    try:
        sections.exhaust_list(page, tempo="the-tempo")
    finally:
        sections.scroll_through = original

    assert seen == ["the-tempo"]


def test_exhausting_a_list_is_bounded_even_if_it_never_settles():
    from career_lens.capture.page import LIST_TOTAL_BUDGET_S

    assert 0 < LIST_TOTAL_BUDGET_S <= 120


# --- the screenshot sweep starts at the top ---------------------------------
#
# The sweep photographs each screen as it passes, so it captures the page from
# wherever it begins. Two steps now leave the page far down before it runs --
# exhausting the list, and opening every clipped "... more" block, which also
# takes keyboard focus. A publications section that grew from 10 entries to 22
# was photographed showing its last two entries and the footer.

def test_rewinding_finds_the_scroller_before_asking_whether_it_is_at_the_top():
    # The bug the first attempt at this shipped with. AT_TOP looks for the
    # data-wf-scroller marker, and every scroll_through clears it on the way
    # out -- so a rewind called between scrolls found nothing, fell back to
    # window.scrollY, and reported "already at the top" while the container
    # holding the content sat at the bottom. The page was photographed from its
    # own footer, and the fix was a no-op that looked like it worked.
    calls: list[str] = []

    class Mouse:
        def __init__(self, log):
            self.log = log

        def move(self, *a, **k):
            self.log.append("move")

        def wheel(self, dx, dy):
            self.log.append("wheel")

    class Scrolled:
        def __init__(self):
            self.keyboard = self
            self.mouse = Mouse(calls)

        def evaluate(self, script, argument=None):
            if "data-wf-scroller" in str(script) and "removeAttribute" in str(script):
                calls.append("clear")
                return None
            if "scrollHeight" in str(script):        # FIND_SCROLLER
                calls.append("find")
                return {"x": 100, "y": 300, "top": 4000, "height": 9000,
                        "client": 800, "left": 0, "right": 200,
                        "viewportTop": 0, "viewportBottom": 600, "crop": False}
            if "activeElement" in str(script):
                calls.append("blur")
                return True
            if "scrollY" in str(script) or "scrollTop" in str(script):
                calls.append("at_top?")
                return True
            return 0

        def press(self, key):
            calls.append(f"press:{key}")

    from career_lens.capture.page import rewind_to_top

    rewind_to_top(Scrolled())

    # The scroller must be located before anything asks whether we are at the
    # top, and the marker must not be left on the page afterwards.
    assert "find" in calls
    assert calls.index("find") < calls.index("at_top?")
    assert calls[-1] == "clear"


def test_rewinding_survives_a_page_that_will_not_run_script():
    from career_lens.capture.page import rewind_to_top

    class Mute:
        def __init__(self):
            self.keyboard = self

        def evaluate(self, script, argument=None):
            raise RuntimeError("execution context destroyed")

        def press(self, key):
            raise RuntimeError("no page")

    rewind_to_top(Mute())  # must not raise


def test_rewinding_preserves_an_explicit_crop_for_the_screenshot_sweep():
    calls: list[str] = []

    class Cropped:
        def __init__(self):
            self.keyboard = self
            self.mouse = self

        def evaluate(self, script, argument=None):
            source = str(script)
            if "removeAttribute" in source:
                calls.append("clear")
                return None
            if "scrollHeight" in source:
                return {"x": 100, "y": 300, "top": 0, "height": 9000,
                        "client": 800, "left": 0, "right": 200,
                        "viewportTop": 0, "viewportBottom": 600,
                        "crop": True}
            if "activeElement" in source:
                return True
            if "scrollY" in source or "scrollTop" in source:
                return True
            return 0

        def press(self, key):
            assert key == "Home"

        def move(self, *args, **kwargs):
            pass

        def wheel(self, dx, dy):
            pass

    from career_lens.capture.page import rewind_to_top

    rewind_to_top(Cropped())

    assert calls == []


def test_dropping_focus_survives_a_page_that_will_not_run_script():
    from career_lens.capture.page import _blur_focus

    class Mute:
        def evaluate(self, script, argument=None):
            raise RuntimeError("execution context destroyed")

    _blur_focus(Mute())  # must not raise
