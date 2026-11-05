"""Browser acquisition.

Patchright driving branded Chrome through a persistent context. Measured against
vanilla Playwright on the same Chrome 151 build, the difference that matters is
`navigator.webdriver`: true under Playwright, false under Patchright, because
Patchright adds --disable-blink-features=AutomationControlled. It also drops 12
fingerprintable launch flags (--disable-extensions, --disable-component-update,
--metrics-recording-only and similar).

Patchright's stealth depends on *not* using certain Playwright features:
add_init_script, route interception and expose_function all reintroduce the
isolated-world injection it exists to avoid. Nothing in this package may use
them. See docs/career-lens-cli.md.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, Protocol

from . import trace
from .cursor import enable_cursor

_log = logging.getLogger("career_lens.capture")

if TYPE_CHECKING:
    from patchright.sync_api import BrowserContext, Page

DEFAULT_PROFILE = "research"
DEFAULT_HEADLESS_WINDOW_SIZE = (1920, 1080)


def profiles_root() -> Path:
    """Profiles live outside the repo: they hold live logged-in sessions."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "career_lens" / "profiles"


def profile_path(name: str = DEFAULT_PROFILE) -> Path:
    return profiles_root() / name


def _mark_clean_exit(profile: Path) -> None:
    """Tell Chrome the last session ended normally.

    Playwright tears the browser down without Chrome's own shutdown sequence, so
    the profile keeps exit_type "Crashed" and every subsequent launch raises the
    "Restore pages? Chrome didn't shut down correctly" bubble. That bubble sits
    over the page in screenshots and, on a profile used for research, is a
    standing signal that this browser is not being closed by a person.

    Rewriting the two flags Chrome checks is the accepted fix; there is no API
    for a graceful quit over CDP.
    """
    prefs = profile / "Default" / "Preferences"
    if not prefs.exists():
        return
    try:
        data = json.loads(prefs.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    section = data.setdefault("profile", {})
    if section.get("exit_type") == "Normal" and section.get("exited_cleanly") is True:
        return
    section["exit_type"] = "Normal"
    section["exited_cleanly"] = True
    try:
        prefs.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    except OSError:
        pass


#: The beat between the pointer reaching the corner and the tab going away.
#: `departure.depart` walks the pointer to the top-right, where the window's X
#: is, and without this the tab vanished in the same instant the hand arrived.
#: A person reaches the button and then presses it.
CLOSE_PAUSE_S = (0.4, 1.2)


def _close_tabs(context) -> None:
    """Close the pages before the process that is showing them.

    `context.close()` ends Chrome from outside: nothing in the document runs,
    so from the site's side the visit stops mid-sentence. Closing the page
    first gives it its own unload, which is what a person pressing the X looks
    like -- and it is the only part of the exit a page can actually see, since
    Chrome is killed rather than quit either way. See `_mark_clean_exit`.

    A tab that refuses is untidy and nothing more: the release below is the
    half that must happen.
    """
    time.sleep(random.SystemRandom().uniform(*CLOSE_PAUSE_S))
    try:
        pages = list(context.pages)
    except Exception:
        return
    for page in pages:
        try:
            page.close()
        except Exception:
            _log.debug("a tab refused to close at teardown", exc_info=True)


def sole_page(context) -> Page:
    """The one page this run will use, with every other tab closed.

    Nothing else in the capture path ever closes a page, so a tab left open is
    a tab that outlives the run. Two things leave them behind. A worker killed
    mid-run never reaches `session()`'s teardown, so its Chrome survives with
    everything it had open; and Playwright's teardown looks like a crash to
    Chrome even on the clean path -- which is why `_mark_clean_exit` exists --
    so the profile keeps its session files and can restore what was open. Left
    alone the profile gains tabs indefinitely: eleven after an afternoon of
    testing, including one `about:blank` per launch.

    That costs memory on a long run, loads restored pages in the background
    while a capture is being timed, and stops the browser looking like one a
    person uses. A thousand profiles would mean hundreds of tabs.

    Cleaning up on the way IN rather than on the way out is what makes it
    self-healing: the run that inherits the mess is the one that clears it,
    which is exactly the case a killed worker leaves behind.
    """
    pages = list(context.pages)
    if not pages:
        return context.new_page()
    for extra in pages[1:]:
        try:
            extra.close()
        except Exception:
            # A tab that refuses to close is untidy; failing the capture over
            # it would be worse. The next run tries again.
            pass
    return pages[0]


class Driver(Protocol):
    """The seam a second driver would implement.

    Deliberately narrow. Only add to it when a real target forces a second
    driver in -- speculatively building the seam wide is how the last driver
    comparison ended up as an n=1 anecdote.
    """

    def start(self) -> Page: ...

    def close(self) -> None: ...


class PatchrightDriver:
    """Branded Chrome via patchright with a persistent profile."""

    name = "patchright"

    def __init__(self, profile: str = DEFAULT_PROFILE, *,
                 slow_mo_ms: int = 0, headless: bool = False,
                 window_size: tuple[int, int] | None = None):
        self.profile = profile
        self.slow_mo_ms = slow_mo_ms
        self.headless = headless
        if headless and window_size is None:
            window_size = DEFAULT_HEADLESS_WINDOW_SIZE
        self.window_size = window_size
        self._playwright = None
        self._context: BrowserContext | None = None

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("driver not started")
        return self._context

    def start(self) -> Page:
        # Imported here rather than at the top, the way blobs.py imports boto3:
        # reading captures back must work in an install that has no browser.
        from patchright.sync_api import sync_playwright

        path = profile_path(self.profile)
        path.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        if self.window_size is None:
            window_argument = "--start-maximized"
        else:
            width, height = self.window_size
            window_argument = f"--window-size={width},{height}"
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(path),
            channel="chrome",
            headless=self.headless,
            # A Playwright-set viewport produces a window whose inner size does
            # not match the frame the OS drew. Letting Chrome size itself keeps
            # window.outerHeight - innerHeight consistent with a real window.
            no_viewport=True,
            slow_mo=self.slow_mo_ms,
            # Playwright launches Chrome with --password-store=basic, which
            # replaces the OS keyring with a hardcoded key. Chrome then cannot
            # decrypt cookies written by a real Chrome (they carry a "v11"
            # keyring prefix, not "v10"), silently discards them, and rewrites
            # the profile signed out -- which destroys the session a copied
            # profile was set up to carry. Measured: 0 of 7 session credentials
            # readable with the flag, 7 of 7 without.
            #
            # Dropping it also matches real Chrome, which is what this project
            # is trying to look like.
            #
            # --disable-background-networking turns off Chrome's extension
            # updater, which is what downloads a policy force-installed
            # extension. With it, the captcha-assist policy on virtual-display
            # hosts never installed the solver. Measured on Chrome 156 with
            # the same policy: not installed after 120s with the flag,
            # installed without it. Real Chrome does not pass it either.
            ignore_default_args=[
                "--password-store=basic", "--disable-background-networking",
            ],
            # Playwright defaults this to False, which adds --no-sandbox. Chrome
            # then shows a permanent "unsupported command-line flag" infobar --
            # visible in every screenshot, and a tell that survives any amount of
            # stealth patching. It was invisible in the patchright-vs-playwright
            # comparison because both sides had it.
            chromium_sandbox=True,
            args=[
                window_argument,
                # Suppresses Chrome's "unsupported command-line flag" infobar,
                # which patchright's own --disable-blink-features=Automation-
                # Controlled otherwise triggers. Counter-intuitively this is the
                # stealthier option: the infobar costs 56px of innerHeight, and
                # a page can measure outerHeight-innerHeight. --test-type is not
                # exposed to page JavaScript at all. Measured: webdriver stays
                # false either way.
                "--test-type",
            ],
        )
        return sole_page(self._context)

    def close(self) -> None:
        """Close the tab like a person, then release the browser and Playwright.

        Both halves are unconditional for the same reason. A Chrome that has
        already gone raises on close -- and left to propagate, that exception
        both skipped `stop()` below and replaced the error that actually ended
        the run, so the recorded cause named the teardown instead of the
        navigation that failed first.

        Skipping `stop()` is the expensive half. Playwright's sync API parks an
        event loop in this thread, so a Playwright nobody stopped makes the next
        capture's `sync_playwright()` find a loop already running and refuse
        with "Playwright Sync API inside the asyncio loop" -- a message about
        asyncio for a fault that has nothing to do with it. Every later job on
        the thread then fails in about 50ms and the process never recovers.
        Measured once in the wild: one dead browser, 65 minutes, six requests.
        """
        context, self._context = self._context, None
        playwright, self._playwright = self._playwright, None
        try:
            if context is not None:
                _close_tabs(context)
                context.close()
        except Exception:
            _log.debug("browser was already gone at teardown", exc_info=True)
        finally:
            if playwright is not None:
                playwright.stop()
            _mark_clean_exit(profile_path(self.profile))


DRIVERS = {"patchright": PatchrightDriver}
HEADLESS = False
WINDOW_SIZE: tuple[int, int] | None = None

#: The profile the live session owns, or None outside one. A worker process
#: drives exactly one profile, so this is that process's profile rather than
#: ambient state -- which is what lets a page-level check ask what is installed
#: beside it without every capture signature carrying a profile name.
OPEN_PROFILE: str | None = None


def open_profile() -> str | None:
    """The profile of the session running in this process, if there is one."""
    return OPEN_PROFILE


def configure(*, headless: bool = False,
              window_size: tuple[int, int] | None = None) -> None:
    """Set browser launch behavior for this worker process."""
    global HEADLESS, WINDOW_SIZE
    HEADLESS = headless
    WINDOW_SIZE = window_size


@contextmanager
def session(driver: str = "patchright", profile: str = DEFAULT_PROFILE, *,
            show_cursor: bool = False) -> Iterator[Page]:
    try:
        factory = DRIVERS[driver]
    except KeyError:
        raise ValueError(f"unknown driver {driver!r}; have {sorted(DRIVERS)}") from None
    instance = factory(
        profile=profile, headless=HEADLESS, window_size=WINDOW_SIZE)
    # Startup is inside the teardown, not before it. `start()` brings Playwright
    # up and only then launches Chrome, so a failure between the two leaves a
    # live Playwright behind -- and a driver started outside this `try` is one
    # nothing ever closes.
    global OPEN_PROFILE
    try:
        with trace.phase(trace.WORK, "start browser"):
            page = instance.start()
        OPEN_PROFILE = profile
        if show_cursor:
            enable_cursor(page.context)
        yield page
    finally:
        OPEN_PROFILE = None
        instance.close()
