"""LinkedIn session decisions use observed redirects, never assumed state."""

from __future__ import annotations

import pytest
from patchright.sync_api import Error as BrowserError
from patchright.sync_api import TimeoutError as BrowserTimeoutError


class Locator:
    def __init__(self, name, page, *, available=True, submit=False,
                 box=None, value=""):
        self.name = name
        self.page = page
        self.available = available
        self.submit = submit
        self.box = box
        self.value = value
        self.first = self
        self.last = self

    def click(self, **kwargs):
        if not self.available:
            raise BrowserTimeoutError(f"{self.name} was not found")
        self.page.calls.append(("click", self.name))
        if self.submit and self.page.submit_advances:
            self.page.url = self.page.final_url

    def bounding_box(self, **kwargs):
        if not self.available:
            raise BrowserTimeoutError(f"{self.name} was not found")
        return self.box

    def evaluate(self, expression, argument=None):
        if "Boolean(el.value)" in expression:
            return bool(self.value)
        return {
            "left": 0, "top": 0, "right": 1200, "bottom": 800,
        }

    def wait_for(self, **kwargs):
        if not self.available:
            raise BrowserTimeoutError(f"{self.name} was not found")
        return None


class Keyboard:
    def __init__(self, calls):
        self.calls = calls

    def press(self, key):
        self.calls.append(("press", key))

    def type(self, char):
        self.calls.append(("type", char))


class Mouse:
    def __init__(self, calls):
        self.calls = calls

    def move(self, x, y):
        self.calls.append(("move", x, y))

    def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class Page:
    USERNAME = 'input[autocomplete~="username"]:visible'
    PASSWORD = 'input[autocomplete~="current-password"]:visible'

    def __init__(self, final_url, *, missing=None, submit_advances=True,
                 initial_url="about:blank", values=None,
                 viewport_fails_after_submit=False):
        self.final_url = final_url
        self.url = initial_url
        self.calls = []
        self.keyboard = Keyboard(self.calls)
        self.mouse = Mouse(self.calls)
        self.missing = missing
        self.submit_advances = submit_advances
        self.values = values or {}
        self.viewport_fails_after_submit = viewport_fails_after_submit

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        if url.endswith("/in/me/"):
            self.url = self.final_url
        else:
            self.url = url

    def locator(self, selector):
        available = selector in {self.USERNAME, self.PASSWORD}
        if self.missing == "username" and selector == self.USERNAME:
            available = False
        if self.missing == "password" and selector == self.PASSWORD:
            available = False
        boxes = {
            self.USERNAME: {"x": 280, "y": 300, "width": 320, "height": 42},
            self.PASSWORD: {"x": 280, "y": 365, "width": 320, "height": 42},
        }
        return Locator(
            selector, self, available=available, box=boxes.get(selector),
            value=self.values.get(selector, ""))

    def get_by_role(self, role, *, name, exact):
        available = role == "button" and name == "Sign in" and exact is True
        if self.missing == "submit":
            available = False
        return Locator(
            "Sign in", self, available=available, submit=True,
            box={"x": 280, "y": 430, "width": 320, "height": 42})

    def evaluate(self, expression):
        if "innerWidth" in expression:
            if self.viewport_fails_after_submit and self.url == self.final_url:
                raise BrowserError("document changed during pointer drift")
            return {"w": 1200, "h": 800}
        self.calls.append(("evaluate", "storage cleared"))

    def title(self):
        return ""

    def inner_text(self, selector):
        return ""

    def wait_for_load_state(self, state):
        self.calls.append(("wait", state))

    def wait_for_url(self, predicate, **kwargs):
        self.calls.append(("wait_for_url", self.url))
        if not predicate(self.url):
            raise BrowserTimeoutError("login URL did not change")


class CDPSession:
    def __init__(self):
        self.calls = []
        self.detached = False

    def send(self, method, params):
        self.calls.append((method, params))

    def detach(self):
        self.detached = True


class ServiceWorker:
    def __init__(self, url):
        self.url = url


class BrowserContext:
    def __init__(self):
        self.cookie_filters = []
        self.cdp = CDPSession()
        self.service_workers = [
            ServiceWorker("https://in.linkedin.com/service-worker.js"),
            ServiceWorker("https://example.com/service-worker.js"),
        ]

    def clear_cookies(self, **kwargs):
        self.cookie_filters.append(kwargs)

    def new_cdp_session(self, page):
        return self.cdp


class ClearPage:
    """One signed-in feed page, in a chosen LinkedIn navigation variant.

    LinkedIn serves more than one global nav, and which one an account gets is
    not ours to choose: measured on 2026-08-14, the account in `research-a`
    carried an avatar button whose aria-label ends in " Me", while the accounts
    in `research-b` and `research-c` carried a `navigation-settings` button with
    no aria-label at all. Sign-out has to find the account menu in either.
    """

    LEGACY_MENU = '[aria-label$=" Me"]:visible'
    CURRENT_MENU = 'button[data-view-name="navigation-settings"]:visible'
    SIGN_OUT = 'a[href*="logout"]:visible'

    def __init__(self, *, menu=LEGACY_MENU):
        self.calls = []
        self.menu = menu
        self.url = "https://www.linkedin.com/feed/"
        self.context = BrowserContext()

    def get_by_role(self, role, *, name, exact):
        return Locator(name, self, available=False)

    def locator(self, selector):
        offered = {part.strip() for part in selector.split(",")}
        return Locator(
            selector, self,
            available=bool(offered & {self.menu, self.SIGN_OUT}))

    def get_by_text(self, text, *, exact):
        return Locator(text, self, available=text == "Sign out" and exact)

    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))

    def evaluate(self, expression):
        self.calls.append(("evaluate", expression))
        if "innerWidth" in expression:
            return {"w": 1200, "h": 800}
        return None

    def wait_for_url(self, predicate, **kwargs):
        self.calls.append(("wait_for_url", kwargs))


class DelayedIdentityPage(Page):
    def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.url = url

    def wait_for_url(self, predicate, **kwargs):
        self.calls.append(("wait_for_url", self.url))
        self.url = self.final_url
        if not predicate(self.url):
            raise BrowserTimeoutError("identity URL did not change")


@pytest.mark.parametrize(("url", "state"), [
    ("https://www.linkedin.com/in/alice/", "signed_in"),
    ("https://www.linkedin.com/login", "signed_out"),
    ("https://www.linkedin.com/checkpoint/challenge/", "challenge"),
])
def test_observe_session_classifies_me_redirect(monkeypatch, url, state):
    from career_lens.linkedin import session

    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)
    result = session.observe_session(Page(url))
    assert result.state == state


def test_observe_session_records_the_redirect_host(monkeypatch):
    from career_lens.linkedin import session

    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)
    result = session.observe_session(
        Page("https://in.linkedin.com/in/alice/?isSelfProfile=true"))

    assert result.state == "signed_in"
    assert result.host == "in.linkedin.com"


def test_a_public_profile_header_means_the_session_was_lost():
    """A target profile URL is not proof of authentication after logout."""
    from career_lens.linkedin import session

    class PublicProfile(Page):
        def __init__(self):
            super().__init__("https://www.linkedin.com/in/afreen-munshi")
            self.url = self.final_url

        def inner_text(self, selector):
            assert selector == "body"
            return (
                "Skip to main content\nLinkedIn\nJoin now\nSign in\n"
                + "the public profile remains rendered " * 200
            )

    result = session._read_identity(PublicProfile())

    assert result.state == "signed_out"


def test_observe_session_waits_for_linkedin_to_resolve_in_me(monkeypatch):
    from career_lens.linkedin import session

    page = DelayedIdentityPage("https://www.linkedin.com/in/alice/")
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.observe_session(page)

    assert result.state == "signed_in"
    assert result.profile_url == "https://www.linkedin.com/in/alice/"
    assert any(kind == "wait_for_url" for kind, _ in page.calls)


def test_observe_session_traces_remote_wait_and_local_classification(
        monkeypatch):
    from career_lens.capture import trace
    from career_lens.linkedin import session

    recorded = []
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)
    trace.activate(["test"], sink=lambda **event: recorded.append(event))
    try:
        result = session.observe_session(
            Page("https://www.linkedin.com/in/alice/"))
    finally:
        trace.close()

    assert result.state == "signed_in"
    assert [(event["kind"], event["phase"]) for event in recorded] == [
        ("net", "verify LinkedIn session"),
        ("work", "read LinkedIn identity"),
    ]


class FastRng:
    def gauss(self, *args): return 0
    def random(self): return 1
    def uniform(self, *args): return 0
    def randint(self, *args): return 0
    def triangular(self, low, high, mode): return mode


def test_login_preserves_checkpoint_instead_of_navigating_to_identity(monkeypatch):
    from career_lens.linkedin import session

    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    page = Page("https://www.linkedin.com/checkpoint/challenge/")
    result = session.login(page, username="example", password="example", rng=FastRng())
    assert result.state == "challenge"
    assert ("goto", session.ME_URL) not in page.calls


def test_login_preserves_unrecognized_landing_page(monkeypatch):
    from career_lens.linkedin import session

    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    page = Page("https://www.linkedin.com/unfamiliar-verification")
    result = session.login(page, username="example", password="example", rng=FastRng())
    assert result.state != "signed_in"
    assert ("goto", session.ME_URL) not in page.calls


def test_verify_preserves_visible_otp_challenge(monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/login", initial_url="https://www.linkedin.com/login")
    page.inner_text = lambda selector: "Let's verify it's you. Enter the verification code"
    result = session.observe_session(page)
    assert result.state == "challenge"
    assert page.url == "https://www.linkedin.com/login"
    assert not any(call[0] == "goto" for call in page.calls)


class ViewportRng(FastRng):
    def uniform(self, low, high): return (low + high) / 2


def test_login_creates_a_fresh_system_random_source_for_each_attempt(
        monkeypatch):
    from career_lens.linkedin import session

    created = []

    def system_random():
        source = FastRng()
        created.append(source)
        return source

    monkeypatch.setattr(session.random, "SystemRandom", system_random)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    for _ in range(2):
        result = session.login(
            Page("https://www.linkedin.com/in/alice/"),
            username="alice@example.com", password="secret")
        assert result.state == "signed_in"

    assert len(created) == 2


def test_login_varies_pauses_between_credential_stages(monkeypatch):
    from career_lens.linkedin import session

    class StageRng(FastRng):
        def uniform(self, low, high):
            pauses = {
                (0.7, 1.6): 1.1,
                (0.45, 1.2): 0.8,
                (0.5, 1.4): 0.9,
                (1.0, 2.2): 1.5,
            }
            return pauses.get((low, high), super().uniform(low, high))

    sleeps = []
    monkeypatch.setattr(session.time, "sleep", sleeps.append)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        Page("https://www.linkedin.com/in/alice/"),
        username="alice@example.com", password="secret", rng=StageRng())

    assert result.state == "signed_in"
    assert all(pause in sleeps for pause in (1.1, 0.8, 0.9, 1.5))


def test_login_reuses_an_open_login_page(monkeypatch):
    from career_lens.linkedin import session

    page = Page(
        "https://www.linkedin.com/in/alice/",
        initial_url="https://www.linkedin.com/login")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=FastRng())

    assert result.state == "signed_in"
    assert ("goto", session.LOGIN_URL) not in page.calls


def test_login_does_not_select_empty_credential_fields(monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/in/alice/")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=FastRng())

    assert result.state == "signed_in"
    assert ("press", "ControlOrMeta+a") not in page.calls


def test_login_initializes_pointer_in_the_viewport_before_the_first_control(
        monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/in/alice/")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=ViewportRng())

    assert result.state == "signed_in"
    pointer = page.calls.index(("move", 600.0, 400.0))
    username = page.calls.index(("click", Page.USERNAME))
    assert pointer < username


def test_login_ignores_pointer_drift_when_the_result_page_is_still_changing(
        monkeypatch):
    from career_lens.linkedin import session

    page = Page(
        "https://www.linkedin.com/in/alice/",
        viewport_fails_after_submit=True)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=FastRng())

    assert result.state == "signed_in"


def test_login_accepts_remembered_account_sign_in_without_username_form(
        monkeypatch):
    from career_lens.linkedin import session

    class RememberedAccountPage(Page):
        """Password-only until submit; `/in/me` cannot manufacture a login."""

        def goto(self, url, **kwargs):
            self.calls.append(("goto", url))
            if url.endswith("/in/me/"):
                self.url = (
                    self.final_url
                    if ("click", "Sign in") in self.calls
                    else session.LOGIN_URL
                )
            else:
                self.url = url

    page = RememberedAccountPage(
        "https://www.linkedin.com/in/alice/", missing="username")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=FastRng())

    assert result.state == "signed_in"
    assert result.profile_url == "https://www.linkedin.com/in/alice/"
    clicks = [call[1] for call in page.calls if call[0] == "click"]
    assert clicks == [Page.PASSWORD, "Sign in"]


def test_clear_browser_state_removes_every_linkedin_origin():
    """Sign-out alone leaves the account reachable.

    Measured on a live profile after LinkedIn's own sign-out: 24 linkedin.com
    cookies survived, `li_rm` among them, and the next visit was signed back
    in. Cookies, storage and the device identifiers that link one browser to
    an account all have to go, and only for LinkedIn -- the profile is shared
    with unrelated browsing.
    """
    from career_lens.linkedin import session

    page = ClearPage()

    session.clear_browser_state(page, origins=("https://in.linkedin.com/feed/",))

    assert [filters["domain"].pattern for filters in page.context.cookie_filters] == [
        r"(^|\.)linkedin\.com$"]
    cleared = {
        params["origin"]: params["storageTypes"]
        for method, params in page.context.cdp.calls
        if method == "Storage.clearDataForOrigin"
    }
    assert set(cleared) == {
        "https://in.linkedin.com",
        "https://linkedin.com",
        "https://www.linkedin.com",
    }
    assert all("local_storage" in types for types in cleared.values())
    assert all("indexeddb" in types for types in cleared.values())
    # Chrome resolves DOMStorage.clear against loaded frames, so it raises for
    # every origin that is not currently open. clearDataForOrigin does not.
    assert not [method for method, _ in page.context.cdp.calls
                if method == "DOMStorage.clear"]
    assert page.context.cdp.detached is True


@pytest.mark.parametrize("failure", ["cookies", "origin"])
def test_clear_browser_state_attempts_every_safety_step_after_one_fails(failure):
    """A partial scrub must still blank the page and try every origin."""
    from career_lens.linkedin import session

    page = ClearPage()
    if failure == "cookies":
        def clear_cookies(**kwargs):
            page.context.cookie_filters.append(kwargs)
            raise BrowserError("cookie store unavailable")

        page.context.clear_cookies = clear_cookies
    else:
        def send(method, params):
            page.context.cdp.calls.append((method, params))
            if (method == "Storage.clearDataForOrigin" and
                    params["origin"] == "https://linkedin.com"):
                raise BrowserError("one origin could not be cleared")

        page.context.cdp.send = send

    with pytest.raises(BrowserError):
        session.clear_browser_state(
            page, origins=("https://in.linkedin.com/feed/",))

    attempted_origins = {
        params["origin"] for method, params in page.context.cdp.calls
        if method == "Storage.clearDataForOrigin"
    }
    assert attempted_origins == {
        "https://in.linkedin.com",
        "https://linkedin.com",
        "https://www.linkedin.com",
    }
    assert ("Page.navigate", {"url": "about:blank"}) in page.context.cdp.calls
    assert page.context.cdp.detached is True


def test_clear_session_drops_linkedin_state_after_signing_out(monkeypatch):
    from career_lens.linkedin import session

    page = ClearPage(menu=ClearPage.CURRENT_MENU)
    monkeypatch.setattr(
        session.interaction, "ensure_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "maybe_move_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "click_in_place", lambda *args, **kwargs: None)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)

    result = session.clear_session(page, rng=FastRng())

    assert result.state == "signed_out"
    assert page.context.cookie_filters, "sign-out must not leave li_rm behind"
    assert [params["origin"] for method, params in page.context.cdp.calls
            if method == "Storage.clearDataForOrigin"] == [
        "https://in.linkedin.com", "https://linkedin.com",
        "https://www.linkedin.com"]


def test_clear_session_abandons_an_unfinished_logout_after_eight_seconds(
        monkeypatch):
    """A committed `/m/logout/` document must not hold Chrome open.

    LinkedIn was measured leaving this navigation blank and unfinished for
    three minutes. The server gets eight seconds to process the click, but the
    client never waits for a document lifecycle event before scrubbing state.
    """
    from career_lens.linkedin import session

    page = ClearPage(menu=ClearPage.CURRENT_MENU)
    sleeps = []
    monkeypatch.setattr(
        session.interaction, "ensure_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "maybe_move_pointer", lambda *args, **kwargs: True)

    def click(current_page, target, **kwargs):
        if target.name == ClearPage.SIGN_OUT:
            current_page.url = "https://www.linkedin.com/m/logout/"

    monkeypatch.setattr(session.interaction, "click_in_place", click)
    monkeypatch.setattr(session.time, "sleep", sleeps.append)

    result = session.clear_session(page, rng=FastRng())

    assert result.state == "signed_out"
    assert sleeps[-1] == 8
    assert not [call for call in page.calls if call[0] == "wait_for_url"]
    assert ("Page.stopLoading", {}) in page.context.cdp.calls
    assert ("Page.navigate", {"url": "about:blank"}) in page.context.cdp.calls


def test_clear_session_scrubs_state_when_the_logout_click_times_out(monkeypatch):
    """A click timeout may mean navigation started but never settled."""
    from career_lens.linkedin import session

    page = ClearPage()
    sleeps = []
    monkeypatch.setattr(
        session.interaction, "ensure_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "maybe_move_pointer", lambda *args, **kwargs: True)

    def click(current_page, target, **kwargs):
        if target.name == ClearPage.SIGN_OUT:
            raise BrowserTimeoutError("logout navigation did not finish")

    monkeypatch.setattr(session.interaction, "click_in_place", click)
    monkeypatch.setattr(session.time, "sleep", sleeps.append)

    result = session.clear_session(page, rng=FastRng())

    assert result.state == "signed_out"
    assert sleeps[-1] == 8
    assert page.context.cookie_filters
    assert ("Page.stopLoading", {}) in page.context.cdp.calls


def test_clear_session_scrubs_state_when_the_logout_control_detaches(monkeypatch):
    """A browser-side locator failure must not bypass local cleanup."""
    from career_lens.linkedin import session

    page = ClearPage()
    sleeps = []
    monkeypatch.setattr(
        session.interaction, "ensure_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "maybe_move_pointer", lambda *args, **kwargs: True)

    def click(current_page, target, **kwargs):
        if target.name == ClearPage.SIGN_OUT:
            raise BrowserError("locator detached during navigation")

    monkeypatch.setattr(session.interaction, "click_in_place", click)
    monkeypatch.setattr(session.time, "sleep", sleeps.append)

    result = session.clear_session(page, rng=FastRng())

    assert result.state == "signed_out"
    assert sleeps[-1] == 8
    assert page.context.cookie_filters
    assert ("Page.navigate", {"url": "about:blank"}) in page.context.cdp.calls


@pytest.mark.parametrize("menu", [ClearPage.LEGACY_MENU, ClearPage.CURRENT_MENU])
def test_clear_session_uses_the_visible_account_menu_to_sign_out(
        monkeypatch, menu):
    from career_lens.linkedin import session

    page = ClearPage(menu=menu)
    pointer_moves = []
    clicks = []
    sleeps = []
    monkeypatch.setattr(
        session.interaction, "ensure_pointer", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        session.interaction, "maybe_move_pointer",
        lambda *args, **kwargs: pointer_moves.append(kwargs["rng"]))
    monkeypatch.setattr(session.time, "sleep", sleeps.append)
    def click(current_page, target, **kwargs):
        if not target.available:
            raise BrowserTimeoutError("not found")
        clicks.append((target.name, kwargs))

    monkeypatch.setattr(session.interaction, "click_in_place", click)
    monkeypatch.setattr(
        session, "observe_session",
        lambda current_page: (_ for _ in ()).throw(
            AssertionError("logout navigation must finish by closing context")))

    result = session.clear_session(page, rng=FastRng())

    assert result.state == "signed_out"
    assert [call[0] for call in page.calls] == ["evaluate"]
    menu_selector, sign_out_selector = (name for name, _ in clicks)
    assert menu in {part.strip() for part in menu_selector.split(",")}
    assert sign_out_selector == ClearPage.SIGN_OUT
    assert clicks[1][1]["no_wait_after"] is True
    assert len(pointer_moves) == 1
    assert sleeps == [0, 8]


def test_login_uses_visible_semantic_controls_and_never_reports_the_password(
        monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/in/alice/")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(session, "recover_transient_error", lambda page: False)
    password = "never-log-this"
    result = session.login(
        page, username="alice@example.com", password=password, rng=FastRng())
    clicks = [call[1] for call in page.calls if call[0] == "click"]
    assert clicks == [Page.USERNAME, Page.PASSWORD, "Sign in"]
    assert result.state == "signed_in"
    assert password not in repr(result)


def test_login_reports_the_control_stage_without_exposing_the_password(monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/in/alice/", missing="password")
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)
    password = "never-log-this"

    result = session.login(
        page, username="alice@example.com", password=password, rng=FastRng())

    assert result == session.SessionObservation(
        "signed_out", detail="LinkedIn login password control unavailable")
    assert password not in repr(result)


def test_login_reports_when_submission_does_not_leave_the_login_page(monkeypatch):
    from career_lens.linkedin import session

    page = Page("https://www.linkedin.com/login/", submit_advances=False)
    monkeypatch.setattr(session.time, "sleep", lambda seconds: None)

    result = session.login(
        page, username="alice@example.com", password="secret", rng=FastRng())

    assert result == session.SessionObservation(
        "signed_out", detail="LinkedIn login did not advance")
