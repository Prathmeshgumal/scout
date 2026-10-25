"""Whether a Chrome profile can answer a Google reCAPTCHA, and installing it.

The solver is a separate Chrome extension installed from the Web Store into one
profile at a time. Chrome refuses `--load-extension` and has no per-profile
external-install directory on Linux, so the install is a person accepting
Chrome's own dialog -- we only open the listing and press the button.

Presence is read from the profile directory rather than from the browser: it
costs nothing, needs no Chrome running, and is the same answer a worker gets.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable

from .browser import open_profile, profile_path

log = logging.getLogger("career_lens.capture")

#: Free AI ReCaptcha Solver by Raptor, the extension the fleet installs.
SOLVER_ID = "iomcoelgdkghlligeempdbfcaobodacg"

#: `hl=en` is load-bearing: the button is found by its accessible name, so the
#: listing has to render in the language that name is written in.
LISTING_URL = (
    f"https://chromewebstore.google.com/detail/{SOLVER_ID}?hl=en")

ADD_BUTTON = "Add to Chrome"

#: How long the dialog is left open for a person to accept it.
INSTALL_TIMEOUT_S = 300.0
POLL_INTERVAL_S = 1.0
PAGE_TIMEOUT_MS = 30_000
BUTTON_TIMEOUT_MS = 15_000


#: How long a challenge is given to clear once, and the extra allowed the first
#: time in a browser. The solver loads two ONNX models (53MB) before it can
#: answer anything, and that load happens on the first challenge, not at start.
CLEAR_TIMEOUT_S = 25.0
WARMUP_EXTRA_S = 20.0
CLEAR_POLL_S = 1.0

#: Whether this process has already waited out one challenge, and so has paid
#: the model load. Per process because the browser it describes is per process.
_warmed = False


def extension_directory(profile: str) -> Path:
    """Where Chrome unpacks the solver in `profile`."""
    return profile_path(profile) / "Default" / "Extensions" / SOLVER_ID


def installed(profile: str) -> bool:
    """Whether `profile` has the solver.

    A version directory has to be present, not just the folder: Chrome leaves
    the parent behind after an uninstall.
    """
    directory = extension_directory(profile)
    try:
        return any(child.is_dir() for child in directory.iterdir())
    except OSError:
        return False


def missing(profiles: Iterable[str]) -> tuple[str, ...]:
    """The profiles without the solver, deduplicated and in order."""
    seen: dict[str, None] = {}
    for profile in profiles:
        if profile not in seen and not installed(profile):
            seen[profile] = None
    return tuple(seen)


def available() -> bool:
    """Whether the profile driving this process can answer a challenge."""
    profile = open_profile()
    return profile is not None and installed(profile)


def clear_budget() -> float:
    """How long to allow the next challenge, longer before the models load."""
    return CLEAR_TIMEOUT_S + (0.0 if _warmed else WARMUP_EXTRA_S)


def wait_until(ready, *, timeout: float, poll: float = CLEAR_POLL_S,
               clock: Any = time.monotonic,
               rest: Any = time.sleep) -> bool:
    """Poll `ready` until it holds or `timeout` passes.

    Marks this process warmed on the first call whatever the outcome: the model
    load is paid by waiting, not by succeeding.
    """
    global _warmed
    deadline = clock() + timeout
    try:
        while True:
            if ready():
                return True
            if clock() >= deadline:
                return False
            rest(poll)
    finally:
        _warmed = True


def install(profile: str, *, timeout: float = INSTALL_TIMEOUT_S,
            poll: float = POLL_INTERVAL_S,
            opener: Any = None, click: Any = None) -> bool:
    """Open the listing in `profile`, press Add to Chrome, wait for a person.

    Returns whether the extension appeared before `timeout`. A click that does
    not raise the dialog is not an error: the browser stays open and the
    operator can press the button themselves, which is what the wait is for.
    """
    # Imported here, not at module scope: `installed` must stay usable in an
    # install that has no browser.
    if opener is None:
        from .browser import session
        opener = session
    if click is None:
        from .interaction import click as click_target
        click = click_target

    target = extension_directory(profile)
    deadline = time.monotonic() + timeout
    with opener(profile=profile, show_cursor=True) as page:
        page.goto(LISTING_URL, wait_until="domcontentloaded",
                  timeout=PAGE_TIMEOUT_MS)
        button = page.get_by_role("button", name=ADD_BUTTON).first
        button.wait_for(state="visible", timeout=BUTTON_TIMEOUT_MS)
        click(page, button)
        while time.monotonic() < deadline:
            if installed(profile):
                log.info("captcha assist installed at %s", target)
                return True
            time.sleep(poll)
    return installed(profile)
