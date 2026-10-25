"""Get past the security screens that stand in front of a page.

Two shapes, and one escalates into the other: a screen that completes by
itself given a few seconds, and a screen that wants a click first. Both serve
a stale response for a page that was never shown, so both discard it once the
real document arrives.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any

from . import interaction, trace
from .detect import CONTENT_FLOOR
from . import verification_contract as policy

if TYPE_CHECKING:
    from patchright.sync_api import Locator, Page, Response


AUTOMATIC_SIGNS = policy.AUTOMATIC_SIGNS
AUTOMATIC_STATUS = policy.AUTOMATIC_STATUS
TIMEOUT_MS = policy.TIMEOUT_MS

# (vendor, the widget as it appears in the host page, controls inside it).
# Matched on the vendor's own frame rather than on what the page says: the
# wording is localised and translated, the challenge URL is neither.
WIDGETS = policy.WIDGETS

# Where the control sits when it cannot be resolved inside the widget. Turnstile
# draws a 300x65 box with the checkbox at its left, so the centre of the widget
# is the vendor's logo rather than the thing to press.
CONTROL_INSET = policy.CONTROL_INSET

# The widget is injected by script, so it is not in the document the
# navigation returned. Measured: absent 0.17s after domcontentloaded on a page
# that was showing it a second later.
WIDGET_GRACE_MS = policy.WIDGET_GRACE_MS

# What makes a page worth waiting on a widget for. Without this gate every
# single-page app -- empty at domcontentloaded, like a challenge -- would pay
# the grace above on every capture. Kept beside `detect.TEXT_SIGNS`, which
# names the same wall for a different purpose: that classifies it, this acts.
CHALLENGE_STATUS = policy.CHALLENGE_STATUS
CHALLENGE_SIGNS = policy.CHALLENGE_SIGNS

# Everything that means "still the wall". Cloudflare takes the widget down
# while it verifies and only then redirects, so the widget being gone is not
# the page being here.
CLEARED_SIGNS = policy.CLEARED_SIGNS

CLICK_TIMEOUT_MS = policy.CLICK_TIMEOUT_MS
# A person sees the checkbox before reaching for it.
READ_PAUSE_S = policy.READ_PAUSE_S
POLL_S = policy.POLL_S

# A real document has arrived, and it is not another way of saying the same
# wall. The non-empty clause earns its place: clearing a challenge navigates,
# and for about a second afterwards no sign matches because *nothing* is there
# yet -- which read as success and handed the capture an empty page.
READY = """signs => {
  const body = document.body ? document.body.innerText : '';
  if (document.readyState !== 'complete' || !body.trim()) return false;
  const head = `${document.title}\n${body}`.slice(0, 2000).toLowerCase();
  return !signs.some(sign => head.includes(sign));
}"""


def _is_automatic(page: Page, response: Response | None) -> bool:
    status = response.status if response is not None else None
    if status != AUTOMATIC_STATUS:
        return False
    head = _head_text(page)
    return any(sign in head for sign in AUTOMATIC_SIGNS)


def _head_text(page: Page) -> str:
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = page.inner_text("body")[:2000]
    except Exception:
        body = ""
    return f"{title}\n{body}".lower()


def _wait_automatic(page: Page, response: Response | None,
                    timeout_ms: int) -> Response | None:
    with trace.phase(trace.WAIT, "automatic verification") as span:
        try:
            page.wait_for_function(
                READY, arg=list(AUTOMATIC_SIGNS), timeout=timeout_ms)
        except Exception:
            span.detail = f"still present after {timeout_ms / 1000:g}s"
            return response
        span.detail = "completed · discarded initial challenge response"
    return None


def _challenged(page: Page, response: Response | None) -> bool:
    """Whether this page is worth waiting on a challenge widget for."""
    status = response.status if response is not None else None
    if status is not None and status in CHALLENGE_STATUS:
        return True
    head = _head_text(page)
    return any(sign in head for sign in CHALLENGE_SIGNS)


def _widget(page: Page,
            grace_ms: int) -> tuple[str, str, tuple[str, ...], dict] | None:
    """The interactive challenge drawn on this page, with its on-screen box."""
    for vendor, selector, controls in WIDGETS:
        try:
            locator = page.locator(selector).first
            if not locator.count():
                locator.wait_for(state="visible", timeout=grace_ms)
            box = locator.bounding_box()
        except Exception:
            continue
        if box and box["width"] > 0 and box["height"] > 0:
            return vendor, selector, controls, box
    return None


def _replaced_the_page(page: Page) -> bool:
    """True when the challenge is the page rather than part of one.

    A widget on a document we can already read belongs to that document's own
    form -- a login, a signup -- and pressing it acts on the site rather than
    on our way in.
    """
    try:
        return len(page.inner_text("body")) < CONTENT_FLOOR
    except Exception:
        return True


def _control(page: Page, selector: str, controls: tuple[str, ...],
             grace_ms: int) -> Locator | None:
    """The pressable control inside the widget's frame, if it can be reached.

    `bounding_box` on a locator inside a frame reports page coordinates, so
    what comes back can be approached and clicked like anything else.

    The widget being drawn does not mean its contents are: waited for once, on
    the checkbox, because that is the control worth having. The rest are
    alternative spellings of it and are gone if it is.
    """
    try:
        frame = page.frame_locator(selector)
    except Exception:
        return None
    for index, inner in enumerate(controls):
        try:
            target = frame.locator(inner).first
            if not target.count() and index == 0:
                target.wait_for(state="visible", timeout=grace_ms)
            if target.count() and target.bounding_box():
                return target
        except Exception:
            continue
    return None


def _press(page: Page, box: dict, target: Locator | None, rng: Any) -> str:
    interaction.ensure_pointer_in_viewport(page, rng=rng)
    time.sleep(rng.uniform(*READ_PAUSE_S))
    if target is not None:
        interaction.click_in_place(
            page, target, timeout=CLICK_TIMEOUT_MS, rng=rng)
        return "its own control"

    x = box["x"] + box["width"] * CONTROL_INSET[0]
    y = box["y"] + box["height"] * CONTROL_INSET[1]
    interaction.move_to(page, x, y, rng=rng)
    page.mouse.click(x, y, delay=rng.randint(*interaction.CLICK_HOLD_MS))
    return "the widget's own offset"


def _wait_cleared(page: Page, selector: str, timeout_ms: int) -> bool:
    """Poll rather than `wait_for_function`, because a cleared challenge
    navigates and an evaluation in flight when it does is destroyed."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            # Whether the widget is up is asked through a locator, not in
            # the script: `document.querySelector` does not cross a shadow
            # root and the vendor renders into one, so a script could never
            # see the widget it was meant to be watching.
            if not page.locator(selector).first.is_visible() \
                    and page.evaluate(READY, list(CLEARED_SIGNS)):
                return True
        except Exception:
            pass
        time.sleep(POLL_S)
    return False


def _press_through(page: Page, response: Response | None, *, timeout_ms: int,
                   grace_ms: int, rng: Any) -> bool | None:
    """Press an interactive challenge. None when there is nothing to press."""
    if not _challenged(page, response) or not _replaced_the_page(page):
        return None
    found = _widget(page, grace_ms)
    if found is None:
        return None
    vendor, selector, controls, box = found

    with trace.phase(trace.WORK, f"{vendor} checkbox") as span:
        try:
            how = _press(page, box,
                         _control(page, selector, controls, grace_ms), rng)
        except Exception as exc:  # noqa: BLE001
            span.detail = f"could not be pressed: {type(exc).__name__}"
            return False
        span.detail = f"pressed by {how}"

    with trace.phase(trace.WAIT, f"{vendor} verification") as span:
        if not _wait_cleared(page, selector, timeout_ms):
            span.detail = f"still up after {timeout_ms / 1000:g}s"
            return False
        span.detail = "cleared · discarded initial challenge response"
    return True


def wait(page: Page, response: Response | None, *,
         timeout_ms: int = TIMEOUT_MS, grace_ms: int = WIDGET_GRACE_MS,
         rng: Any = random) -> Response | None:
    """Clear a verification screen and discard the response it served.

    Automatic first, then the interactive one -- in that order because
    Cloudflare escalates that way, showing "checking your browser" and putting
    up a checkbox when watching the browser was not enough. A screen that does
    not clear keeps its original response, which leaves it to ordinary
    challenge detection to report.
    """
    from .failures import raise_for_rate_limit

    raise_for_rate_limit(response)
    automatic = (_wait_automatic(page, response, timeout_ms) is None
                 if _is_automatic(page, response) else False)

    # Looked for even when the wait above succeeded: what that wait watches is
    # the wording, and escalating to a checkbox changes the wording too.
    pressed = _press_through(page, response, timeout_ms=timeout_ms,
                             grace_ms=grace_ms, rng=rng)
    if pressed is None:
        return None if automatic else response
    return None if pressed else response
