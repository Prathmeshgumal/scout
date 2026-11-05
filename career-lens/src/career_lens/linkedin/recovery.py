"""Recover from LinkedIn's transient whole-page failure state."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from patchright.sync_api import Page


ERROR_HEADING = "Something went wrong"
# LinkedIn labels its retry control differently depending on how much of the
# page failed: the whole-page error offers "Try again", while the empty state
# that replaces a single feed offers "Refresh". Both sit under the same
# heading, so the label is the only thing that distinguishes them, and a
# recogniser that knows one of them silently gives up on the other.
RETRY_LABELS = ("Try again", "Refresh")
RETRY_LABEL = RETRY_LABELS[0]
RECOVERY_TIMEOUT_MS = 20_000
CLICK_TIMEOUT_MS = 5_000


class LinkedInTransientError(RuntimeError):
    """LinkedIn showed its retry page and could not be recovered once."""


def recover_transient_error(
    page: Page, *, timeout_ms: int = RECOVERY_TIMEOUT_MS,
) -> bool:
    """Click LinkedIn's exact whole-page retry control once when visible.

    Returns whether recovery was needed. A recognized error page that cannot
    recover raises instead of being mistaken for captured profile content.
    """
    host = (urlsplit(page.url).hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return False

    heading = page.get_by_role("heading", name=ERROR_HEADING, exact=True).first
    try:
        shown = bool(heading.count()) and heading.is_visible()
    except Exception:
        shown = False
    if not shown:
        return False

    retry, label = None, ""
    for candidate in RETRY_LABELS:
        found = page.get_by_role("button", name=candidate, exact=True).first
        if found.count():
            retry, label = found, candidate
            break
    if retry is None:
        offered = " or ".join(repr(name) for name in RETRY_LABELS)
        raise LinkedInTransientError(
            f"LinkedIn showed {ERROR_HEADING!r} without a {offered} control")
    try:
        retry.click(timeout=CLICK_TIMEOUT_MS)
        heading.wait_for(state="hidden", timeout=timeout_ms)
    except Exception as exc:
        raise LinkedInTransientError(
            f"LinkedIn remained on {ERROR_HEADING!r} after clicking {label!r}") from exc
    return True
