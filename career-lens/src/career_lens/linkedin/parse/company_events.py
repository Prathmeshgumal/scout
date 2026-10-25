"""Structured extraction for a LinkedIn company Events page."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from lxml.html import HtmlElement

from .company_common import class_xpath, exact_count, first, resolve_url, text


ATTENDEES = re.compile(r"\battendees?\b", re.IGNORECASE)


def _event_url(anchor: HtmlElement, base_url: str) -> str | None:
    url = resolve_url(anchor.get("href"), base_url, keep_query=False)
    if not url:
        return None
    parts = urlsplit(url)
    segments = parts.path.strip("/").split("/")
    if (
        (parts.hostname == "linkedin.com" or (parts.hostname or "").endswith(".linkedin.com"))
        and len(segments) == 2 and segments[0] == "events"
    ):
        return url
    return None


def _event_cards(document: HtmlElement, heading: str, base_url: str) -> list[dict]:
    cards: list[dict] = []
    labels = {heading, heading.removesuffix(" events")}
    headings = [node for node in document.xpath(
        ".//*[self::h1 or self::h2 or self::h3 or self::h4]"
    ) if text(node) in labels]
    for heading_node in headings:
        scope = None
        for ancestor in heading_node.iterancestors():
            group_headings = ancestor.xpath(".//*[self::h1 or self::h2 or self::h3 or self::h4]")
            if any(text(node) in {"Past", "Past events", "Upcoming", "Upcoming events",
                                 "No upcoming events planned"} and node is not heading_node
                   for node in group_headings):
                break
            if ancestor.xpath(".//a[contains(@href, '/events/')]"):
                scope = ancestor
                break
        if scope is None:
            continue
        for card in scope.xpath(class_xpath("org-events__list-item--event-card")):
            anchor = first(card, class_xpath("events-components-shared-event-card"))
            title = text(first(card, class_xpath(
                "events-components-shared-event-card__event-title")))
            starts_at = text(first(card, class_xpath(
                "events-components-shared-event-card__text-single-line")))
            if anchor is None or not title:
                continue
            image = first(card, class_xpath(
                "events-components-shared-event-card__background-image"))
            attendee = first(card, class_xpath(
                "events-components-shared-social-proof__copy-text"))
            cards.append({
                "title": title,
                "starts_at": starts_at,
                "url": resolve_url(anchor.get("href"), base_url),
                "image_url": (
                    image.get("src") or image.get("data-delayed-url")
                    if image is not None else None
                ),
                "attendee_count": exact_count(text(attendee), ATTENDEES),
            })
        if cards:
            continue
        seen: set[str] = set()
        for anchor in scope.xpath(".//a[@href][contains(@href, '/events/')]"):
            url = _event_url(anchor, base_url)
            if not url or url in seen:
                continue
            card = anchor
            if not anchor.xpath(".//p"):
                card = next((parent for parent in anchor.iterancestors()
                             if parent is not scope and parent.tag in {"article", "li"}), anchor)
            title = text(first(anchor, ".//h3 | .//h4"))
            if not title:
                title = next((text(link) for link in anchor.xpath(".//a[@href]")
                              if _event_url(link, base_url) == url and text(link)), None)
            if not title and not anchor.xpath(".//p"):
                title = text(anchor)
            if not title:
                continue
            paragraphs = [text(node) for node in card.xpath(".//p")]
            paragraphs = [value for value in paragraphs if value]
            attendee_label = next(
                (value for value in paragraphs if ATTENDEES.search(value)), None
            )
            starts_at = next(
                (value for value in paragraphs if value != title and not ATTENDEES.search(value)), None
            )
            image = first(card, ".//img[@src or @data-delayed-url]")
            seen.add(url)
            cards.append({
                "title": title,
                "starts_at": starts_at,
                "url": url,
                "image_url": (
                    image.get("src") or image.get("data-delayed-url")
                    if image is not None else None
                ),
                "attendee_count": exact_count(attendee_label, ATTENDEES),
            })
    return cards


def extract_events(document: HtmlElement, base_url: str) -> dict[str, object]:
    """Keep LinkedIn's upcoming and past event groups distinct."""
    result = {
        "upcoming": _event_cards(document, "Upcoming events", base_url),
        "past": _event_cards(document, "Past events", base_url),
    }
    parsed_urls = {item["url"] for items in result.values() for item in items}
    visible_urls = {url for anchor in document.xpath(".//a[@href]")
                    if (url := _event_url(anchor, base_url))}
    if visible_urls - parsed_urls:
        raise ValueError("LinkedIn company Events cards could not all be read")
    return result
