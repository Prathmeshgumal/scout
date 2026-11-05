"""Unit coverage for capture behavior that does not launch a browser."""

from career_lens.capture import browser
from career_lens.capture import options
from career_lens.capture import page as capture
from career_lens.capture import pace
from career_lens.capture import trace

import pytest


class _Page:
    def __init__(self, url: str, refuses: bool = False):
        self.url = url
        self.closed = False
        self._refuses = refuses

    def close(self) -> None:
        if self._refuses:
            raise RuntimeError("tab will not close")
        self.closed = True


class _Context:
    """A persistent context as it arrives: whatever Chrome restored, plus the
    blank page each launch adds."""

    def __init__(self, *pages: _Page):
        self.pages = list(pages)
        self.opened = 0

    def new_page(self) -> _Page:
        self.opened += 1
        return _Page("about:blank")


def test_the_run_closes_every_tab_it_did_not_open():
    """A tab nobody closes outlives the run, and the profile gains one per
    launch until the browser is carrying hundreds of them."""
    leftovers = [_Page("https://www.linkedin.com/in/jenhsunhuang/"),
                 _Page("about:blank"), _Page("https://www.google.com/")]
    context = _Context(_Page("about:blank"), *leftovers)

    page = browser.sole_page(context)

    assert page is context.pages[0]
    assert [tab.closed for tab in leftovers] == [True, True, True]


def test_a_tab_that_refuses_to_close_does_not_fail_the_capture():
    """Untidy beats losing the run over a tab. The next launch tries again."""
    stubborn = _Page("https://www.linkedin.com/feed/", refuses=True)
    after = _Page("about:blank")
    context = _Context(_Page("about:blank"), stubborn, after)

    page = browser.sole_page(context)

    assert page is context.pages[0]
    assert after.closed is True


def test_an_empty_context_gets_a_page_rather_than_an_IndexError():
    context = _Context()

    assert browser.sole_page(context).url == "about:blank"
    assert context.opened == 1


def test_configured_launch_settings_reach_every_browser_session(monkeypatch):
    settings = []

    class Driver:
        def __init__(self, profile, *, headless, window_size):
            settings.append((headless, window_size))
            self.context = _Context(_Page("about:blank"))

        def start(self):
            return self.context.pages[0]

        def close(self):
            pass

    monkeypatch.setitem(browser.DRIVERS, "fake", Driver)
    monkeypatch.setattr(browser, "HEADLESS", False)
    monkeypatch.setattr(browser, "WINDOW_SIZE", None)
    browser.configure(headless=True, window_size=(1440, 900))

    with browser.session("fake"):
        pass

    assert settings == [(True, (1440, 900))]


def test_browser_session_traces_process_startup_as_work(monkeypatch):
    recorded = []

    class Driver:
        def __init__(self, profile, *, headless, window_size):
            self.context = _Context(_Page("about:blank"))

        def start(self):
            return self.context.pages[0]

        def close(self):
            pass

    monkeypatch.setitem(browser.DRIVERS, "fake", Driver)
    trace.activate(["test"], sink=lambda **event: recorded.append(event))
    try:
        with browser.session("fake"):
            pass
    finally:
        trace.close()

    assert [(event["kind"], event["phase"]) for event in recorded] == [
        ("work", "start browser"),
    ]


def _driver_whose_browser_died(monkeypatch, tmp_path):
    """A driver whose Chrome went away while the run was still using it.

    Closing the context then raises, which is what the ola capture hit.
    """
    stopped = []

    class Context:
        def __init__(self):
            self.pages = [_Page("about:blank")]

        def close(self):
            raise RuntimeError(
                "BrowserContext.close: Target page, context or browser "
                "has been closed")

    class Chromium:
        def launch_persistent_context(self, **options):
            return Context()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            stopped.append(True)

    class Manager:
        def start(self):
            return Playwright()

    monkeypatch.setattr(
        "patchright.sync_api.sync_playwright", lambda: Manager())
    monkeypatch.setattr(
        browser, "profile_path", lambda profile: tmp_path / profile)
    return stopped


def _living_driver(monkeypatch, tmp_path, *pages):
    """A driver whose Chrome is still there when the run ends.

    `order` records the teardown as it happens, because what matters is not
    only that the tabs were closed but that they were closed before the
    process that was showing them.
    """
    order: list[str] = []

    class Context:
        def __init__(self):
            self.pages = list(pages) or [_Page("about:blank")]

        def close(self):
            order.append("context")

    class Chromium:
        def launch_persistent_context(self, **options):
            return Context()

    class Playwright:
        chromium = Chromium()

        def stop(self):
            order.append("playwright")

    class Manager:
        def start(self):
            return Playwright()

    for page in pages:
        original = page.close

        def closing(_original=original):
            order.append("tab")
            _original()

        page.close = closing
    monkeypatch.setattr(
        "patchright.sync_api.sync_playwright", lambda: Manager())
    monkeypatch.setattr(
        browser, "profile_path", lambda profile: tmp_path / profile)
    return order


def test_the_tab_is_closed_before_the_browser_process_is(monkeypatch, tmp_path):
    """A page killed with its process never runs its own unload.

    `context.close()` ends Chrome from outside, so from the site's side the
    visit stops mid-sentence. Closing the tab first is what a person pressing
    the X does, and it is the only part of the exit a page can actually see.
    """
    page = _Page("https://www.example.com/organization/acme")
    order = _living_driver(monkeypatch, tmp_path, page)
    driver = browser.PatchrightDriver()
    driver.start()

    driver.close()

    assert page.closed is True
    assert order == ["tab", "context", "playwright"]


def test_the_worker_chrome_keeps_what_policy_installs_and_saved_logins_need(
        monkeypatch, tmp_path):
    """Patchright defaults a real Chrome does not have, and that break it.

    --disable-background-networking stops the extension updater, so the
    captcha-assist force-install policy never downloads the solver.
    --password-store=basic makes Chrome discard a real profile's cookies.
    """
    launched = {}

    class Context:
        pages = [_Page("about:blank")]

    class Chromium:
        def launch_persistent_context(self, **options):
            launched.update(options)
            return Context()

    class Playwright:
        chromium = Chromium()

    class Manager:
        def start(self):
            return Playwright()

    monkeypatch.setattr(
        "patchright.sync_api.sync_playwright", lambda: Manager())
    monkeypatch.setattr(
        browser, "profile_path", lambda profile: tmp_path / profile)

    browser.PatchrightDriver().start()

    assert "--disable-background-networking" in launched["ignore_default_args"]
    assert "--password-store=basic" in launched["ignore_default_args"]
    assert launched["channel"] == "chrome"


def test_a_beat_passes_before_the_tab_closes(monkeypatch, tmp_path):
    """A hand reaches the X and then presses it. Both in the same instant is
    the shape the departure wander exists to avoid."""
    slept: list[float] = []
    monkeypatch.setattr(browser.time, "sleep", slept.append)
    _living_driver(monkeypatch, tmp_path, _Page("about:blank"))
    driver = browser.PatchrightDriver()
    driver.start()

    driver.close()

    assert len(slept) == 1
    assert browser.CLOSE_PAUSE_S[0] <= slept[0] <= browser.CLOSE_PAUSE_S[1]


def test_a_tab_that_will_not_close_still_releases_the_browser(
        monkeypatch, tmp_path):
    """Leaving the tab open is untidy; leaving Playwright running poisons the
    worker. The teardown carries on."""
    order = _living_driver(
        monkeypatch, tmp_path, _Page("about:blank", refuses=True))
    driver = browser.PatchrightDriver()
    driver.start()

    driver.close()

    assert order == ["tab", "context", "playwright"]


def test_a_browser_that_died_still_releases_playwright(monkeypatch, tmp_path):
    """Leaving Playwright running poisons the whole worker process, not just
    this run.

    Its sync API parks an event loop in the thread, so the next capture's
    `sync_playwright()` finds a loop already running and refuses with "Playwright
    Sync API inside the asyncio loop" -- a message about asyncio for a fault that
    has nothing to do with it. Every later job on that thread fails in about 50ms
    and the process never recovers. One dead browser cost a worker 65 minutes and
    six requests before anyone read the process tree.
    """
    stopped = _driver_whose_browser_died(monkeypatch, tmp_path)
    driver = browser.PatchrightDriver()
    driver.start()

    driver.close()

    assert stopped == [True]
    assert driver._playwright is None


def test_teardown_does_not_replace_the_failure_that_caused_it(
        monkeypatch, tmp_path):
    """A browser that has already gone cannot be closed, and saying so is
    useless: the capture failed earlier, for a reason worth keeping.

    The ola attempt recorded "BrowserContext.close: Target page, context or
    browser has been closed" and lost the navigation error that actually
    explained it.
    """
    _driver_whose_browser_died(monkeypatch, tmp_path)
    driver = browser.PatchrightDriver()
    driver.start()

    driver.close()


def test_a_session_whose_launch_fails_still_releases_the_driver(monkeypatch):
    """`start()` sets up Playwright and then launches Chrome. A driver that
    fails between the two holds a live Playwright nobody will ever stop unless
    the teardown covers the startup as well."""
    closed = []

    class Driver:
        def __init__(self, profile, *, headless, window_size):
            pass

        def start(self):
            raise RuntimeError("Chrome failed to launch")

        def close(self):
            closed.append(True)

    monkeypatch.setitem(browser.DRIVERS, "fake", Driver)

    with pytest.raises(RuntimeError, match="Chrome failed to launch"):
        with browser.session("fake"):
            pass

    assert closed == [True]


def _record_browser_launch(monkeypatch, tmp_path):
    launched = []
    context = _Context(_Page("about:blank"))

    class Chromium:
        def launch_persistent_context(self, **options):
            launched.append(options)
            return context

    class Playwright:
        chromium = Chromium()

    class Manager:
        def start(self):
            return Playwright()

    monkeypatch.setattr(
        "patchright.sync_api.sync_playwright", lambda: Manager())
    monkeypatch.setattr(
        browser, "profile_path", lambda profile: tmp_path / profile)
    return launched


def test_headless_chrome_defaults_to_a_desktop_sized_window(
        monkeypatch, tmp_path):
    launched = _record_browser_launch(monkeypatch, tmp_path)

    browser.PatchrightDriver(headless=True).start()

    assert "--window-size=1920,1080" in launched[0]["args"]
    assert "--start-maximized" not in launched[0]["args"]


@pytest.mark.parametrize("headless", [False, True])
def test_an_explicit_window_size_replaces_maximise_in_either_mode(
        monkeypatch, tmp_path, headless):
    launched = _record_browser_launch(monkeypatch, tmp_path)

    browser.PatchrightDriver(
        headless=headless, window_size=(1440, 900)).start()

    assert "--window-size=1440,900" in launched[0]["args"]
    assert "--start-maximized" not in launched[0]["args"]


def test_artifact_html_drops_live_only_nodes_without_mutating_other_content():
    source = """<!doctype html><html><body>
      <nav data-career-lens-artifact-only="" style="display:none">Private nav</nav>
      <main><h1>Profile</h1></main>
    </body></html>"""

    stored = capture._stored_html(source).decode("utf-8")

    assert "Private nav" not in stored
    assert "data-career-lens-artifact-only" not in stored
    assert "<h1>Profile</h1>" in stored


def test_feed_read_pause_is_text_aware_and_bounded(monkeypatch):
    monkeypatch.setattr(pace.random, "triangular", lambda low, high, mode: mode)
    tempo = pace.Tempo(
        name="test", between_queries=(0, 0), between_results=(0, 0),
        scroll_pause=(0, 0), dwell_scale=1.0)

    short = tempo.read_pause(1)
    long = tempo.read_pause(100_000)

    assert pace.READ_BOUNDS[0] <= short <= long <= pace.READ_BOUNDS[1]


def test_feed_read_pauses_allow_time_to_read():
    assert pace.READ_BOUNDS == (3.0, 9.0)


def test_linkedin_section_selection_has_explicit_all_none_and_aliases():
    assert options.normalise_linkedin_sections(None) is None
    assert options.normalise_linkedin_sections("all") is None
    assert options.normalise_linkedin_sections("none") == frozenset()
    assert options.normalise_linkedin_sections(
        "about, experience, activity"
    ) == frozenset({
        "about-this-member", "experience", "activity-posts",
        "activity-comments", "activity-articles", "activity-reactions",
    })
    assert options.normalise_linkedin_sections(
        options.LINKEDIN_PERSON_SECTIONS,
    ) is None

    with pytest.raises(options.InvalidCaptureOption, match="unknown LinkedIn section"):
        options.normalise_linkedin_sections("experience,telepathy")


def test_causes_is_a_section_a_request_may_name():
    """It has no capture of its own, but it is still declinable."""
    assert options.normalise_linkedin_sections(
        "experience,causes"
    ) == frozenset({"experience", "causes"})


def test_new_public_profile_collections_are_selectable():
    assert options.normalise_linkedin_sections(
        "volunteering,test-scores,services,connected-apps"
    ) == frozenset({
        "volunteering", "test-scores", "services", "connected-apps",
    })


def test_about_this_member_is_always_included_as_metadata():
    sections = options.linkedin_capture_sections(frozenset({"experience"}))

    assert sections == frozenset(
        {"experience", "about-this-member", "contact-info", "open-to-work"})


def test_company_section_selection_uses_company_vocabulary():
    assert options.normalise_linkedin_sections(
        "home,about,services,posts,products,jobs,life,people,events",
        kind="linkedin_company",
    ) is None
    assert options.normalise_linkedin_sections(
        "home,posts", kind="linkedin_company",
    ) == frozenset({"home", "posts"})

    with pytest.raises(options.InvalidCaptureOption,
                       match="unknown LinkedIn company section"):
        options.normalise_linkedin_sections(
            "experience", kind="linkedin_company",
        )


def test_a_window_scroll_that_stalled_with_page_below_it_is_defeated():
    """What a scroll lock looks like from here. A consent banner that sets
    `overflow: hidden` stops the wheel dead, and the screens taken on the way
    are of a page that never moved -- one plain full-page shot is strictly
    more of the page than all of them together."""
    stalled = capture.Scroll(pixels=228, stopped=capture.STALLED,
                             owner=capture.WINDOW)

    assert capture.defeated(stalled, viewport=1093, height=5736) is True


def test_a_scroll_that_stalled_at_the_end_of_the_page_is_not_defeated():
    """Nothing was missed, so there is nothing to go back for."""
    stalled = capture.Scroll(pixels=4643, stopped=capture.STALLED,
                             owner=capture.WINDOW)

    assert capture.defeated(stalled, viewport=1093, height=5736) is False


def test_a_scroll_that_reached_the_bottom_is_never_defeated():
    reached = capture.Scroll(pixels=228, stopped=capture.BOTTOM,
                             owner=capture.WINDOW)

    assert capture.defeated(reached, viewport=1093, height=5736) is False


def test_a_stalled_inner_scroller_keeps_its_screens():
    """A page whose content lives inside a clipped element is one viewport tall
    to a full-page screenshot, so falling back to one would store less than the
    screens already hold. LinkedIn is the whole reason that path exists."""
    stalled = capture.Scroll(pixels=0, stopped=capture.STALLED,
                             owner=capture.ELEMENT)

    assert capture.defeated(stalled, viewport=1093, height=5736) is False


def test_contact_info_is_captured_alongside_any_selection():
    """Profile metadata rather than an optional section, the way About this
    member is: a caller asking for experience should not get a profile whose
    contact details are missing because they did not think to name them."""
    sections = options.linkedin_capture_sections(frozenset({"experience"}))

    assert "contact-info" in sections


def test_open_to_work_is_captured_alongside_any_selection():
    """Whether a member is looking, and for what, is metadata about them
    rather than a section they wrote."""
    sections = options.linkedin_capture_sections(frozenset({"experience"}))

    assert "open-to-work" in sections
