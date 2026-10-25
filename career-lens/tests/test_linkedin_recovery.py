"""Recovery from LinkedIn's transient whole-page error."""

from __future__ import annotations

import pytest


class Locator:
    def __init__(self, count: int = 0):
        self._count = count
        self.clicked = 0
        self.waited = None

    @property
    def first(self):
        return self

    def count(self) -> int:
        return self._count

    def is_visible(self) -> bool:
        return bool(self._count)

    def click(self, *, timeout: int) -> None:
        self.clicked += 1
        self.click_timeout = timeout

    def wait_for(self, *, state: str, timeout: int) -> None:
        self.waited = (state, timeout)


class Page:
    def __init__(self, *, url: str = "https://www.linkedin.com/in/jane/",
                 error: bool = False, retry: bool = False):
        self.url = url
        self.heading = Locator(1 if error else 0)
        self.retry = Locator(1 if retry else 0)
        self.queries = []

    def get_by_role(self, role: str, *, name: str, exact: bool):
        self.queries.append((role, name, exact))
        return self.heading if role == "heading" else self.retry


def test_the_exact_linkedin_error_page_clicks_try_again_once():
    from career_lens.linkedin.recovery import recover_transient_error

    page = Page(error=True, retry=True)

    assert recover_transient_error(page, timeout_ms=4321) is True
    assert page.retry.clicked == 1
    assert page.heading.waited == ("hidden", 4321)


def test_an_ordinary_linkedin_page_is_not_touched():
    from career_lens.linkedin.recovery import recover_transient_error

    page = Page()

    assert recover_transient_error(page) is False
    assert page.retry.clicked == 0


def test_the_recovery_never_runs_away_from_linkedin():
    from career_lens.linkedin.recovery import recover_transient_error

    page = Page(url="https://example.com/", error=True, retry=True)

    assert recover_transient_error(page) is False
    assert page.queries == []


def test_an_error_page_without_its_retry_control_is_not_captured_as_content():
    from career_lens.linkedin.recovery import (
        LinkedInTransientError,
        recover_transient_error,
    )

    with pytest.raises(LinkedInTransientError, match="Try again"):
        recover_transient_error(Page(error=True, retry=False))


class LabelledPage:
    """A page whose retry control has a specific label.

    The Page above answers any button query with the same locator, which
    cannot show that the right label was asked for. Found live: LinkedIn
    labels the whole-page retry "Try again" and the failed-feed one
    "Refresh", and a recogniser that knows only the first captured a
    260-character error page as a profile's Comments feed.
    """

    def __init__(self, *, label: str | None,
                 url: str = "https://www.linkedin.com/in/jane/recent-activity/comments/"):
        self.url = url
        self.heading = Locator(1)
        self.label = label
        self.buttons: dict[str, Locator] = {}
        self.asked: list[str] = []

    def get_by_role(self, role: str, *, name: str, exact: bool):
        if role == "heading":
            return self.heading
        self.asked.append(name)
        return self.buttons.setdefault(
            name, Locator(1 if name == self.label else 0))


def test_a_failed_feed_offers_refresh_rather_than_try_again():
    from career_lens.linkedin.recovery import recover_transient_error

    page = LabelledPage(label="Refresh")

    assert recover_transient_error(page) is True
    assert page.buttons["Refresh"].clicked == 1
    assert page.buttons["Try again"].clicked == 0


def test_the_whole_page_error_still_uses_try_again():
    from career_lens.linkedin.recovery import recover_transient_error

    page = LabelledPage(label="Try again")

    assert recover_transient_error(page) is True
    assert page.buttons["Try again"].clicked == 1
    # Cheapest first: the known label is tried before the alternative.
    assert page.asked == ["Try again"]


def test_neither_label_present_still_refuses_to_capture_the_error():
    from career_lens.linkedin.recovery import (
        LinkedInTransientError,
        recover_transient_error,
    )

    page = LabelledPage(label=None)

    with pytest.raises(LinkedInTransientError, match="Refresh"):
        recover_transient_error(page)
    assert page.asked == ["Try again", "Refresh"]
