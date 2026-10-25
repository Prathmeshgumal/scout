"""Provider-neutral capture failure boundaries."""

from __future__ import annotations

import pytest

from career_lens.capture.failures import BrowserGone, raise_if_browser_gone


class LivePage:
    def is_closed(self) -> bool:
        return False


def test_a_closed_browser_stops_the_run_instead_of_one_provider_section():
    class ClosedPage:
        def is_closed(self) -> bool:
            return True

    with pytest.raises(BrowserGone, match="skills"):
        raise_if_browser_gone(ClosedPage(), during="skills")


def test_the_error_that_announces_the_close_is_recognised_too():
    closed = type("TargetClosedError", (Exception,), {})(
        "Target page, context or browser has been closed")

    with pytest.raises(BrowserGone):
        raise_if_browser_gone(LivePage(), closed, during="experience")


def test_a_page_that_cannot_answer_is_treated_as_gone():
    class BrokenPage:
        def is_closed(self):
            raise RuntimeError("connection lost")

    with pytest.raises(BrowserGone):
        raise_if_browser_gone(BrokenPage(), during="courses")
