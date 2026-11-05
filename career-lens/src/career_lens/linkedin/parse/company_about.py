"""Typed fields from a LinkedIn company About tab."""

from __future__ import annotations

import json
import re

from lxml.html import HtmlElement

from .company_common import (
    class_xpath,
    exact_count,
    first,
    resolve_url,
    text,
    without_label,
)


ABOUT_FIELDS = {
    "Website": "website",
    "Verified page": "verified_on",
    "Industry": "industry",
    "Company size": "company_size",
    "Headquarters": "headquarters",
    "Founded": "founded",
    "Specialties": "specialties",
}
EMPLOYEES = re.compile(r"\bemployees?\b", re.IGNORECASE)
ASSOCIATED_MEMBERS = re.compile(r"\bassociated members?\b", re.IGNORECASE)


def _about_card(document: HtmlElement) -> HtmlElement | None:
    legacy = first(
        document,
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        "' org-page-details-module__card-spacing ')][.//h2[normalize-space()='Overview']]",
    )
    if legacy is not None:
        return legacy
    heading = first(document, ".//main//h2[normalize-space()='Overview']")
    if heading is None:
        return None
    for scope in heading.iterancestors():
        if scope.tag == "main":
            break
        if scope.xpath(".//p[normalize-space()='Website' or normalize-space()='Industry']"):
            return scope
    return None


def _current_definitions(card: HtmlElement, source_url: str) -> dict[str, object]:
    values: dict[str, object] = {field: None for field in ABOUT_FIELDS.values()}
    values["associated_members"] = None
    for label, field in ABOUT_FIELDS.items():
        labels = card.xpath(f".//p[normalize-space()={label!r}]")
        if not labels:
            continue
        label_wrapper = labels[0].getparent()
        group = label_wrapper.getparent() if label_wrapper is not None else None
        if group is None:
            continue
        siblings = list(group)[1:]
        if not siblings:
            continue
        if field == "website":
            anchor = first(group, ".//a[@href]")
            values[field] = (
                resolve_url(anchor.get("href"), source_url)
                if anchor is not None else text(siblings[0])
            )
        else:
            value = text(siblings[0])
            values[field] = (
                without_label(value, EMPLOYEES)
                if field == "company_size" else value
            )
        if label == "Company size":
            anchor = first(group, ".//a[contains(@href, '/search/results/people/')]")
            if anchor is not None:
                values["associated_members"] = {
                    "count": exact_count(text(anchor), ASSOCIATED_MEMBERS),
                    "url": resolve_url(anchor.get("href"), source_url),
                    "description": None,
                }
    return values


def _definition_values(term: HtmlElement) -> list[HtmlElement]:
    values: list[HtmlElement] = []
    sibling = term.getnext()
    while sibling is not None:
        tag = sibling.tag.lower() if isinstance(sibling.tag, str) else None
        if tag == "dt":
            break
        if tag == "dd":
            values.append(sibling)
        sibling = sibling.getnext()
    return values


def _definitions(card: HtmlElement, source_url: str) -> dict[str, object]:
    values: dict[str, object] = {field: None for field in ABOUT_FIELDS.values()}
    values["associated_members"] = None
    for term in card.xpath(".//dl/dt"):
        label = text(first(term, ".//h3")) or text(term)
        definitions = _definition_values(term)
        if label in ABOUT_FIELDS and definitions:
            field = ABOUT_FIELDS[label]
            if field == "website":
                anchor = first(definitions[0], ".//a[@href]")
                values[field] = (
                    resolve_url(anchor.get("href"), source_url)
                    if anchor is not None
                    else text(definitions[0])
                )
            else:
                value = text(definitions[0])
                values[field] = (
                    without_label(value, EMPLOYEES)
                    if field == "company_size"
                    else value
                )
        if label != "Company size":
            continue
        for definition in definitions:
            anchor = first(
                definition, ".//a[contains(@href, '/search/results/people/')][@href]"
            )
            if anchor is None:
                continue
            tooltip = first(definition, class_xpath("org-about-company-module__tooltip-trigger"))
            values["associated_members"] = {
                "count": exact_count(text(anchor), ASSOCIATED_MEMBERS),
                "url": resolve_url(anchor.get("href"), source_url),
                "description": text(tooltip),
            }
            break
    return values


def _commitments(document: HtmlElement, source_url: str) -> list[dict[str, object]]:
    commitments: list[dict[str, object]] = []
    for card in document.xpath(class_xpath("org-commitments-module__commitment-card")):
        name = text(first(card, class_xpath("org-commitment-description__title")))
        if not name:
            continue
        resources: list[dict[str, object]] = []
        for anchor in card.xpath(class_xpath("org-commitment-carousel__target-wrapper")):
            image = first(anchor, ".//img[@src]")
            title = first(anchor, class_xpath("org-commitment-carousel__title"))
            description = first(
                anchor,
                ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' text-body-small ')][not(contains(@class, 'org-commitment-carousel__title'))]",
            )
            resources.append(
                {
                    "title": text(title),
                    "description": text(description),
                    "url": resolve_url(anchor.get("href"), source_url),
                    "image_url": image.get("src") if image is not None else None,
                }
            )
        commitments.append(
            {
                "name": name,
                "description": text(first(card, class_xpath("org-commitment-description__body"))),
                "programs": text(first(card, class_xpath("org-commitment-programs__text"))),
                "resources": resources,
            }
        )
    return commitments


def _locations(document: HtmlElement) -> list[dict[str, object]]:
    """Read the normalized response records stored in the About artifact."""
    payload = first(
        document,
        ".//section[@data-career_lens='company-locations']"
        "//script[@type='application/json']",
    )
    if payload is None:
        return []
    try:
        locations = json.loads(payload.text or "")
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(locations, list):
        return []
    return [location for location in locations if isinstance(location, dict)]


def extract_about(document: HtmlElement, source_url: str) -> dict[str, object]:
    card = _about_card(document)
    fields = (
        _definitions(card, source_url)
        if card is not None and card.xpath(".//dl/dt")
        else _current_definitions(card, source_url)
        if card is not None
        else {
        field: None for field in ABOUT_FIELDS.values()
        } | {"associated_members": None}
    )
    overview = (
        first(card, ".//h2[normalize-space()='Overview']/following-sibling::p[1]")
        if card is not None else None
    )
    if overview is None and card is not None:
        paragraphs = card.xpath(
            ".//p[not(normalize-space()='Website') "
            "and not(normalize-space()='Verified page') "
            "and not(normalize-space()='Industry') "
            "and not(normalize-space()='Company size') "
            "and not(normalize-space()='Headquarters') "
            "and not(normalize-space()='Founded') "
            "and not(normalize-space()='Specialties')]"
        )
        overview = paragraphs[0] if paragraphs else None
    return {
        "overview": text(overview),
        **fields,
        "commitments": _commitments(document, source_url),
        "locations": _locations(document),
    }
