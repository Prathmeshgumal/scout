"""Company analytics and rendered suggestions from a LinkedIn People tab."""

from __future__ import annotations

import re
from collections.abc import Mapping

from lxml.html import HtmlElement

from .company_common import class_xpath, first, resolve_url, text


VIEWER_SPECIFIC_ANALYTICS = {"How you are connected"}


def _count(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[\d,]+", value):
        return None
    return int(value.replace(",", ""))


def _analytics_card(card: HtmlElement) -> dict[str, object] | None:
    title = text(first(card, ".//h3"))
    if not title or title in VIEWER_SPECIFIC_ANALYTICS:
        return None

    items: list[dict[str, object]] = []
    semantic_rows = card.xpath("./ul/li[.//strong]")
    if semantic_rows:
        for row in semantic_rows:
            strong = first(row, ".//strong")
            count = _count(text(strong))
            label = " ".join(
                value.strip()
                for value in strong.xpath("following-sibling::text()")
                if value.strip()
            ) if strong is not None else None
            if count is not None and label:
                items.append({"label": label, "count": count})
    elif card.xpath(".//a[@role='checkbox']"):
        for row in card.xpath(".//a[@role='checkbox']"):
            parts = [text(node) for node in row.xpath(".//p")]
            parts = [value for value in parts if value]
            if len(parts) < 2:
                continue
            count = _count(parts[-1])
            label = parts[0]
            if count is not None and label:
                items.append({"label": label, "count": count})
    else:
        for row in card.xpath(class_xpath("org-people-bar-graph-element")):
            count = _count(text(first(row, ".//strong")))
            label = text(
                first(row, class_xpath("org-people-bar-graph-element__category"))
            )
            if count is not None and label:
                items.append({"label": label, "count": count})

    return {"title": title, "items": items} if items else None


def _analytics_cards(document: HtmlElement) -> list[dict[str, object]]:
    consolidated = first(
        document, ".//section[@data-career_lens='people-analytics']"
    )
    if consolidated is not None:
        scopes = consolidated.xpath("./article")
    else:
        container = first(document, class_xpath("org-people__insights-container"))
        carousel = first(container, class_xpath("artdeco-carousel")) if container is not None else None
        scopes = carousel.xpath(class_xpath("insight-container")) if carousel is not None else []
        if not scopes:
            scopes = []
            for heading in document.xpath(".//main//h3"):
                scope = next(
                    (
                        ancestor for ancestor in heading.iterancestors()
                        if ancestor.tag in {"li", "article", "div"}
                        and ancestor.xpath(".//a[@role='checkbox']")
                    ),
                    None,
                )
                if scope is not None and scope not in scopes:
                    scopes.append(scope)

    cards: list[dict[str, object]] = []
    for scope in scopes:
        card = _analytics_card(scope)
        if card is not None:
            cards.append(card)
    return cards


def _reached_end(capture: Mapping[str, object] | None) -> bool | None:
    if not isinstance(capture, Mapping):
        return None
    analytics = capture.get("people_analytics")
    if not isinstance(analytics, Mapping):
        return None
    if isinstance(analytics.get("reached_end"), bool):
        return analytics["reached_end"]
    visited = analytics.get("positions_visited")
    total = analytics.get("positions_total")
    if not isinstance(visited, int) or not isinstance(total, int) or total <= 0:
        return None
    return visited >= total


def _profile_image(card: HtmlElement, source_url: str) -> str | None:
    image = first(card, ".//a[contains(@href, '/in/')]//img[@src]")
    if image is None:
        return None
    value = (image.get("src") or "").strip()
    if not value or value.startswith("data:image/"):
        return None
    return resolve_url(value, source_url)


def _people_you_may_know(
    document: HtmlElement, source_url: str
) -> list[dict[str, object]]:
    sections = document.xpath(
        class_xpath("org-people-profile-card__card-spacing")
        + "[.//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' org-people-profile-card__profile-card-spacing ')]]"
    )
    if not sections:
        return []

    people: list[dict[str, object]] = []
    for card in sections[0].xpath(
        class_xpath("org-people-profile-card__profile-card-spacing")
    ):
        title = first(card, class_xpath("artdeco-entity-lockup__title"))
        profile = first(title, ".//a[contains(@href, '/in/')][@href]") if title is not None else None
        name = text(profile) or text(title)
        if not name or profile is None:
            continue
        people.append(
            {
                "name": name,
                "headline": text(
                    first(card, class_xpath("artdeco-entity-lockup__subtitle"))
                ),
                "profile_url": resolve_url(
                    profile.get("href"), source_url, keep_query=False
                ),
                "image_url": _profile_image(card, source_url),
            }
        )
    return people


def extract_people(
    document: HtmlElement,
    source_url: str,
    capture: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Read company People analytics and suggestions already rendered on-page."""
    cards = _analytics_cards(document)
    if not cards and document.xpath(
        ".//main//h3[not(normalize-space()='How you are connected')]"
        "[ancestor::*[.//a[@role='checkbox']]]"
    ):
        raise ValueError("LinkedIn company People analytics could not be read")
    return {
        "source_url": source_url,
        "analytics": {
            "reached_end": _reached_end(capture),
            "cards": cards,
        },
        "people_you_may_know": _people_you_may_know(document, source_url),
    }
