"""Presence of the reCAPTCHA solver in a profile, and the one-time install."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from career_lens.capture import captcha_assist


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return tmp_path / "career_lens" / "profiles"


def _install_into(profiles, name: str, version: str = "0.2.3000_0") -> None:
    directory = (profiles / name / "Default" / "Extensions"
                 / captcha_assist.SOLVER_ID / version)
    directory.mkdir(parents=True)
    (directory / "manifest.json").write_text("{}")


def test_a_profile_without_the_solver_is_not_installed(profiles):
    assert captcha_assist.installed("research") is False


def test_a_profile_with_a_version_directory_is_installed(profiles):
    _install_into(profiles, "research")
    assert captcha_assist.installed("research") is True


def test_the_folder_alone_does_not_count_as_installed(profiles):
    """Chrome leaves the parent behind after an uninstall."""
    captcha_assist.extension_directory("research").mkdir(parents=True)
    assert captcha_assist.installed("research") is False


def test_missing_reports_each_profile_once_and_in_order(profiles):
    _install_into(profiles, "has-it")
    assert captcha_assist.missing(
        ["needs-it", "has-it", "needs-it", "other"]) == ("needs-it", "other")


class FakeButton:
    def __init__(self):
        self.waited = False

    def wait_for(self, **_):
        self.waited = True


class FakePage:
    def __init__(self):
        self.button = FakeButton()
        self.url = None
        self.role = None

    def goto(self, url, **_):
        self.url = url

    def get_by_role(self, role, *, name):
        self.role = (role, name)
        return self

    @property
    def first(self):
        return self.button


def _session(page, opened):
    @contextmanager
    def open_session(*, profile, show_cursor):
        opened.append((profile, show_cursor))
        yield page
    return open_session


def test_install_opens_the_english_listing_and_clicks_add(profiles):
    page, opened, clicked = FakePage(), [], []
    _install_into(profiles, "research")

    done = captcha_assist.install(
        "research", opener=_session(page, opened),
        click=lambda page, target: clicked.append(target))

    assert done is True
    assert opened == [("research", True)]
    assert page.url == captcha_assist.LISTING_URL
    assert "hl=en" in page.url
    assert page.role == ("button", captcha_assist.ADD_BUTTON)
    assert clicked == [page.button]
    assert page.button.waited is True


def test_install_waits_for_the_person_to_accept_the_dialog(profiles):
    """The extension appears only after a few polls, as a person is slower."""
    page, polls = FakePage(), []

    def slow_click(_page, _target):
        polls.append("clicked")

    original = captcha_assist.installed

    def appears(profile):
        polls.append(profile)
        if len(polls) > 3:
            _install_into(profiles, profile)
        return original(profile)

    captcha_assist.installed = appears
    try:
        done = captcha_assist.install(
            "research", poll=0, opener=_session(page, []), click=slow_click)
    finally:
        captcha_assist.installed = original

    assert done is True
    assert len(polls) > 3


def test_install_gives_up_when_nobody_accepts(profiles):
    page = FakePage()
    done = captcha_assist.install(
        "research", timeout=0, poll=0, opener=_session(page, []),
        click=lambda page, target: None)
    assert done is False


def test_assist_is_unavailable_without_an_open_session(profiles):
    assert captcha_assist.available() is False


def test_assist_is_available_when_the_open_profile_has_it(profiles,
                                                          monkeypatch):
    _install_into(profiles, "research")
    monkeypatch.setattr(captcha_assist, "open_profile", lambda: "research")
    assert captcha_assist.available() is True


def test_assist_is_unavailable_when_the_open_profile_lacks_it(profiles,
                                                              monkeypatch):
    monkeypatch.setattr(captcha_assist, "open_profile", lambda: "research")
    assert captcha_assist.available() is False


def test_the_first_challenge_of_a_browser_gets_the_model_load_too(monkeypatch):
    monkeypatch.setattr(captcha_assist, "_warmed", False)
    cold = captcha_assist.clear_budget()

    captcha_assist.wait_until(lambda: True, timeout=0, rest=lambda _: None)

    assert cold == (captcha_assist.CLEAR_TIMEOUT_S
                    + captcha_assist.WARMUP_EXTRA_S)
    assert captcha_assist.clear_budget() == captcha_assist.CLEAR_TIMEOUT_S


def test_a_challenge_that_never_clears_gives_up_at_the_budget(monkeypatch):
    monkeypatch.setattr(captcha_assist, "_warmed", True)
    now, slept = [0.0], []

    def clock():
        return now[0]

    def rest(seconds):
        slept.append(seconds)
        now[0] += seconds

    assert captcha_assist.wait_until(
        lambda: False, timeout=10, poll=2, clock=clock, rest=rest) is False
    # Bounded: it stops at the budget rather than polling forever.
    assert sum(slept) <= 10


def test_a_cleared_challenge_stops_polling_immediately(monkeypatch):
    monkeypatch.setattr(captcha_assist, "_warmed", True)
    answers, slept = iter([False, False, True]), []

    assert captcha_assist.wait_until(
        lambda: next(answers), timeout=60, poll=1,
        rest=lambda seconds: slept.append(seconds)) is True
    assert len(slept) == 2
