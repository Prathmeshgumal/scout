"""Modules owned by the LinkedIn company Home tab."""

from __future__ import annotations

import re

from lxml.html import HtmlElement

from .activity import extract_activity
from .company_common import exact_count, first, resolve_url, sections_with_heading, text


SUBSCRIBERS = re.compile(r"\bsubscribers?\b", re.IGNORECASE)


def _featured(document: HtmlElement, company_url: str) -> list[dict[str, object]]:
    featured: list[dict[str, object]] = []
    seen: set[str] = set()
    sections = sections_with_heading(document, "Featured")
    for heading in document.xpath('.//main//h2[normalize-space()="Featured"]'):
        for scope in heading.iterancestors():
            if scope.tag == "main":
                break
            if scope.xpath(
                './/*[@data-testid="carousel-container" or @data-career_lens="consolidated-carousel"]'
            ):
                # An outer section can contain Featured and the ordinary feed.
                sections = [section for section in sections
                            if section not in scope.iterancestors()]
                if scope not in sections:
                    sections.append(scope)
                break
    for section in sections:
        slides = section.xpath(
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' artdeco-carousel__item ')]"
        )
        scopes = slides or [section]
        for scope in scopes:
            for item in extract_activity(scope, company_url)["items"]:
                identity = str(item.get("id") or item.get("permalink") or "")
                if identity and identity in seen:
                    continue
                if identity:
                    seen.add(identity)
                featured.append(item)
    return featured


def _newsletters(document: HtmlElement, company_url: str) -> list[dict[str, object]]:
    newsletters: list[dict[str, object]] = []
    sections = document.xpath(
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' org-newsletters-module__card-spacing ')]"
    )
    for heading in document.xpath(
        './/main//*[self::h2 or self::h3 or self::h4][normalize-space()="Newsletter"]'
    ):
        for scope in heading.iterancestors():
            if scope.tag == "main":
                break
            if scope.xpath('.//a[contains(@href,"/newsletters/")]'):
                if scope not in sections:
                    sections.append(scope)
                break
    seen: set[str] = set()
    for section in sections:
        name = first(section, ".//h4")
        anchor = first(section, ".//a[contains(@href, '/newsletters/')][.//h4]")
        if anchor is None:
            anchor = first(section, ".//a[contains(@href, '/newsletters/')]")
        url = resolve_url(anchor.get("href"), company_url, keep_query=False) if anchor is not None else None
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        if name is None and anchor is not None:
            name = first(anchor, ".//*[self::h3 or self::p]")
        image = first(
            section,
            ".//img[contains(concat(' ', normalize-space(@class), ' '), "
            "' org-newsletters-module__logo-entity ')]",
        )
        if image is None and anchor is not None:
            image = first(anchor, ".//img[@src]")
        details = first(section, ".//h4/following-sibling::p[1]")
        detail_parts = (
            [part for part in (text(node) for node in details.xpath(".//span")) if part]
            if details is not None
            else []
        )
        if not detail_parts:
            details = next((p for p in section.xpath('.//p') if SUBSCRIBERS.search(text(p) or "")), None)
            detail_parts = [part.strip() for part in (text(details) or "").split("·") if part.strip()]
        subscriber_label = next(
            (part for part in detail_parts if SUBSCRIBERS.search(part)), None
        )
        publication_frequency = next(
            (part for part in detail_parts if not SUBSCRIBERS.search(part)), None
        )
        description = first(
            section,
            ".//*[contains(concat(' ', normalize-space(@class), ' '), "
            "' org-newsletters-module__description ')]",
        )
        if description is None:
            description = next((p for p in reversed(section.xpath('.//p'))
                                if p is not details and p is not name
                                and (anchor is None or p not in anchor.iterdescendants())), None)
        newsletters.append(
            {
                "name": text(name),
                "url": url,
                "publication_frequency": publication_frequency,
                "subscriber_count": exact_count(subscriber_label, SUBSCRIBERS),
                "description": text(description),
                "image_url": image.get("src") if image is not None else None,
            }
        )
    return newsletters


def extract_home(document: HtmlElement, company_url: str) -> dict[str, object]:
    """Return Home-only data; dedicated-tab previews are intentionally omitted."""
    return {
        "featured": _featured(document, company_url),
        "newsletters": _newsletters(document, company_url),
    }
