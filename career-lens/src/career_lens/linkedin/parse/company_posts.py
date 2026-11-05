"""Company Posts-tab data and capture completeness."""

from __future__ import annotations

from collections.abc import Mapping

from lxml.html import HtmlElement

from .activity import extract_activity


EMPTY_FEED = (
    ".//*[contains(concat(' ', normalize-space(@class), ' '),"
    " ' scaffold-finite-scroll__empty ')]"
    "[ancestor::*[contains(concat(' ', normalize-space(@class), ' '),"
    " ' feed-container-theme ')]]"
)


def _reached_end(
    document: HtmlElement,
    items: list[object],
    capture: Mapping[str, object] | None,
) -> bool | None:
    if not items and document.xpath(EMPTY_FEED):
        return True
    stopped = capture.get("scroll_stopped") if capture else None
    if stopped == "bottom":
        return True
    if stopped == "distance":
        return False
    return None


def extract_posts(
    document: HtmlElement,
    source_url: str,
    capture: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Read company posts without exposing profile-activity bookkeeping."""
    activity = extract_activity(document, source_url)
    items = list(activity["items"])
    if not items and document.xpath(".//main//h2[normalize-space()='Feed post']"):
        raise ValueError("LinkedIn company Posts content could not be read")
    return {
        "source_url": source_url,
        "reached_end": _reached_end(document, items, capture),
        "items": items,
    }
