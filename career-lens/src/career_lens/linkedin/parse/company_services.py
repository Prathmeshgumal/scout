"""Structured extraction for an optional LinkedIn company Services page."""

from __future__ import annotations

from lxml.html import HtmlElement

from .company_common import class_xpath, first, resolve_url, text


def _visible_text(element: HtmlElement | None) -> str | None:
    """Prefer LinkedIn's visible accessibility branch over its duplicate label."""
    if element is None:
        return None
    if element.get("aria-hidden") == "true":
        return text(element)
    visible = first(element, ".//*[@aria-hidden='true']")
    return text(visible) if visible is not None else text(element)


def _label_value(scope: HtmlElement, label: str) -> str | None:
    candidates = scope.xpath(
        ".//*[self::h1 or self::h2 or self::h3 or self::h4 or self::p]"
    )
    for node in candidates:
        if _visible_text(node) != label:
            continue
        siblings = node.xpath("following-sibling::*[1]")
        if siblings and (value := _visible_text(siblings[0])):
            return value
    return None


def _overview(scope: HtmlElement) -> str | None:
    for heading in ("About us", "Overview"):
        nodes = [
            node for node in scope.xpath(
                ".//*[self::h1 or self::h2 or self::h3 or self::h4]"
            )
            if _visible_text(node) == heading
        ]
        for node in nodes:
            section = node.xpath("ancestor::section[1]")
            if not section:
                continue
            paragraphs = section[0].xpath(".//p")
            values = [_visible_text(paragraph) for paragraph in paragraphs]
            values = [value for value in values if value and value not in {
                "Available", "Pricing", _label_value(section[0], "Available"),
                _label_value(section[0], "Pricing"),
            }]
            if values:
                return " ".join(values)
    return None


def extract_services(document: HtmlElement, base_url: str) -> dict[str, object]:
    """Extract only the primary services surface, excluding recommendation rails."""
    scope = first(document, class_xpath("organization-service-pages__main-content"))
    if scope is None:
        raise ValueError("LinkedIn company Services content was not found")

    action = first(scope, class_xpath(
        "service-pages-about-services-card-primary-cta__trigger-js"))
    services = []
    seen: set[tuple[str, str | None]] = set()
    for anchor in scope.xpath(class_xpath(
            "service-pages-services-provided-card__service-pills-link-light")):
        name = text(anchor)
        url = resolve_url(anchor.get("href"), base_url)
        if not name or (name, url) in seen:
            continue
        seen.add((name, url))
        services.append({"name": name, "url": url})

    return {
        "overview": _overview(scope),
        "request_action": (
            {"label": _visible_text(action),
             "url": resolve_url(action.get("href"), base_url)}
            if action is not None else None
        ),
        "availability": _label_value(scope, "Available"),
        "pricing": _label_value(scope, "Pricing"),
        "services": services,
    }
