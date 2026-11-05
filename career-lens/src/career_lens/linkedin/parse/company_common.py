"""Shared DOM primitives for LinkedIn company captures."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from lxml.html import HtmlElement

from .dom import clean_text


FOLLOWERS = re.compile(r"\bfollowers?\b", re.IGNORECASE)
EMPLOYEES = re.compile(r"\bemployees?\b", re.IGNORECASE)
BACKGROUND_IMAGE = re.compile(r"background-image\s*:\s*url\((?P<url>[^)]+)\)")
COMPANY_TAB_PATHS = {
    "home": "home",
    "about": "about",
    "posts": "posts",
    "product": "products",
    "jobs": "jobs",
    "life": "life",
    "people": "people",
    "services": "services",
}


def _path_matches_company(href: str | None, slug: str) -> bool:
    if not href:
        return False
    parts = [part for part in urlsplit(href).path.split("/") if part]
    return len(parts) >= 2 and parts[:2] == ["company", slug]


def _current_header(
    document: HtmlElement, slug: str
) -> tuple[HtmlElement | None, HtmlElement | None]:
    """Find the current header by its company route and semantic heading."""
    for heading in document.xpath(".//main//*[self::h1 or self::h2]"):
        for scope in heading.iterancestors():
            if scope.tag == "main":
                break
            if not scope.xpath(".//p"):
                continue
            if any(
                _path_matches_company(anchor.get("href"), slug)
                for anchor in scope.xpath(".//a[@href]")
            ):
                return scope, heading
    return None, None


def class_xpath(class_name: str) -> str:
    return (
        ".//*[contains(concat(' ', normalize-space(@class), ' '), "
        f"' {class_name} ')]"
    )


def first(element: HtmlElement, expression: str) -> HtmlElement | None:
    matches = element.xpath(expression)
    return matches[0] if matches else None


def text(element: HtmlElement | None) -> str | None:
    if element is None:
        return None
    return " ".join(clean_text(element).split()) or None


def without_label(value: str | None, label: re.Pattern[str]) -> str | None:
    """Remove a LinkedIn display noun without changing the value's precision."""
    if value is None:
        return None
    normalized = label.sub("", value).strip()
    return normalized or None


def exact_count(value: str | None, label: re.Pattern[str]) -> int | None:
    """Return an exact displayed count; compact values such as 2M stay unknown."""
    normalized = without_label(value, label)
    if normalized is None or not re.fullmatch(r"[\d,]+", normalized):
        return None
    return int(normalized.replace(",", ""))


def resolve_url(value: str | None, base_url: str, *, keep_query: bool = True) -> str | None:
    if not value or not value.strip():
        return None
    absolute = urljoin(base_url, value.strip())
    parts = urlsplit(absolute)
    if (
        parts.path.rstrip("/") == "/safety/go"
        and (parts.hostname or "").endswith("linkedin.com")
    ):
        targets = parse_qs(parts.query).get("url", [])
        if targets:
            absolute = targets[0]
    if keep_query:
        return absolute
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def company_identity(company_url: str) -> tuple[str, str]:
    parts = [part for part in urlsplit(company_url).path.split("/") if part]
    if len(parts) < 2 or parts[0] != "company":
        raise ValueError(f"not a LinkedIn company URL: {company_url}")
    slug = parts[1]
    return slug, f"https://www.linkedin.com/company/{slug}/"


def available_company_sections(
    document: HtmlElement, company_url: str
) -> set[str] | None:
    """Return tabs proven present by the company navigation.

    ``None`` means the capture did not preserve a recognizable company nav,
    so absence of a tab is not evidence that the company does not offer it.
    """
    slug, canonical_url = company_identity(company_url)
    for navigation in document.xpath(".//nav"):
        sections: set[str] = set()
        for anchor in navigation.xpath(".//a[@href]"):
            url = resolve_url(anchor.get("href"), canonical_url, keep_query=False)
            if url is None:
                continue
            parts = [part for part in urlsplit(url).path.split("/") if part]
            if len(parts) < 2 or parts[:2] != ["company", slug]:
                continue
            if len(parts) == 2 or (len(parts) == 3 and parts[2] == "home"):
                sections.add("home")
            elif section := COMPANY_TAB_PATHS.get(parts[2]):
                sections.add(section)
        if "home" in sections:
            return sections
    return None


def company_events_available(document: HtmlElement, company_url: str) -> bool:
    """Return whether Home links to the company's full Events view."""
    slug, canonical_url = company_identity(company_url)
    expected_path = f"/company/{slug}/events"
    for anchor in document.xpath(".//a[@href]"):
        url = resolve_url(anchor.get("href"), canonical_url, keep_query=False)
        if url is not None and urlsplit(url).path.rstrip("/") == expected_path:
            return True
    return False


def extract_company_header(document: HtmlElement, company_url: str) -> dict[str, object]:
    """Read the company identity repeated at the top of each company tab."""
    slug, canonical_url = company_identity(company_url)
    top_card = first(document, class_xpath("org-top-card"))
    current_heading = None
    if top_card is None:
        top_card, current_heading = _current_header(document, slug)
    if top_card is None:
        top_card = document

    logo = first(top_card, class_xpath("org-top-card-primary-content__logo"))
    cover = first(top_card, class_xpath("org-cropped-image__cover-image"))
    cover_match = BACKGROUND_IMAGE.search(cover.get("style") or "") if cover is not None else None

    industry = None
    location = None
    followers = None
    employee_count = None
    product_categories: list[dict[str, str | None]] = []
    descriptive_items: list[str] = []
    info_list = first(top_card, class_xpath("org-top-card-summary-info-list"))
    if info_list is not None:
        for item in info_list.xpath(
            "./*"
            "[contains(concat(' ', normalize-space(@class), ' '), "
            "' org-top-card-summary-info-list__info-item ') "
            "or contains(concat(' ', normalize-space(@class), ' '), ' inline-block ')]"
        ):
            candidates = (
                item.xpath(
                    ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                    "' org-top-card-summary-info-list__info-item ')]"
                )
                if "inline-block" in (item.get("class") or "").split()
                else [item]
            )
            for candidate in candidates:
                value = text(candidate)
                if not value:
                    continue
                product_link = first(
                    candidate, ".//a[contains(@href, '/search/results/products/')]"
                )
                if product_link is not None:
                    product_categories.append(
                        {
                            "name": value,
                            "url": resolve_url(product_link.get("href"), canonical_url),
                        }
                    )
                elif FOLLOWERS.search(value):
                    followers = without_label(value, FOLLOWERS)
                elif EMPLOYEES.search(value):
                    employee_count = without_label(value, EMPLOYEES)
                else:
                    descriptive_items.append(value)
    if descriptive_items:
        industry = descriptive_items[0]
    if len(descriptive_items) > 1:
        location = descriptive_items[1]

    if current_heading is not None:
        logo = first(
            document,
            ".//main//img[@src][contains(@src, 'company-logo_') or "
            "contains(@src, 'company-logo/') or contains(@src, 'company-logo-')]",
        )
        cover_image = first(
            document,
            ".//main//img[@src][contains(translate(@alt, "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'cover photo')]",
        )
        values = [
            value
            for node in top_card.xpath(".//p[not(@aria-hidden='true')]")
            if (value := text(node))
        ]
        employee_link = first(
            top_card,
            ".//a[contains(@href, '/search/results/people/')][contains(., 'employee')]",
        )
        if employee_link is not None and (value := text(employee_link)):
            values.append(value)
        tagline = values.pop(0) if values else None
        followers = next(
            (without_label(value, FOLLOWERS) for value in values if FOLLOWERS.search(value)),
            None,
        )
        employee_count = next(
            (without_label(value, EMPLOYEES) for value in values if EMPLOYEES.search(value)),
            None,
        )
        descriptive_items = [
            value for value in values
            if not FOLLOWERS.search(value) and not EMPLOYEES.search(value)
        ]
        industry = descriptive_items[0] if descriptive_items else None
        location = descriptive_items[-1] if len(descriptive_items) > 1 else None
        product_categories = [
            {"name": value, "url": None}
            for value in descriptive_items[1:-1]
        ]
    else:
        cover_image = None
        tagline = None

    action = first(
        top_card,
        ".//a[contains(concat(' ', normalize-space(@class), ' '), "
        "' org-top-card-primary-actions__action ')][@href]",
    )
    external_action = (
        {"label": text(action), "url": resolve_url(action.get("href"), canonical_url)}
        if action is not None
        else None
    )

    if current_heading is not None:
        action = next(
            (
                anchor for anchor in document.xpath(".//main//a[@href]")
                if text(anchor) == "Learn more"
                and not _path_matches_company(anchor.get("href"), slug)
            ),
            None,
        )
        external_action = (
            {"label": text(action), "url": resolve_url(action.get("href"), canonical_url)}
            if action is not None else None
        )

    title = first(top_card, class_xpath("org-top-card-summary__title"))
    legacy_tagline = first(top_card, class_xpath("org-top-card-summary__tagline"))
    return {
        "url": canonical_url,
        "slug": slug,
        "name": text(current_heading) if current_heading is not None else text(title),
        "logo_url": logo.get("src") if logo is not None else None,
        "cover_image_url": (
            cover_image.get("src") if cover_image is not None
            else cover_match.group("url").strip(" '\"") if cover_match else None
        ),
        "verified": bool(
            top_card.xpath(
                ".//*[starts-with(@aria-label, 'Verified') "
                "or @data-test-icon='verified-medium']"
            )
        ),
        "tagline": tagline if current_heading is not None else text(legacy_tagline),
        "industry": industry,
        "product_categories": product_categories,
        "location": location,
        "followers": followers,
        "employee_count": employee_count,
        "external_action": external_action,
    }


def sections_with_heading(document: HtmlElement, heading: str) -> list[HtmlElement]:
    sections: list[HtmlElement] = []
    for node in document.xpath(
        ".//*[self::h1 or self::h2 or self::h3 or self::h4]"
        f"[normalize-space(.)={heading!r}]"
    ):
        ancestors = node.xpath("ancestor::section[1]")
        if ancestors and ancestors[0] not in sections:
            sections.append(ancestors[0])
    return sections


def extract_affiliated_pages(
    document: HtmlElement, company_url: str
) -> list[dict[str, object]]:
    """Read the repeated Affiliated pages sidebar once, deduplicated by URL."""
    pages: list[dict[str, object]] = []
    seen: set[str] = set()
    for section in sections_with_heading(document, "Affiliated pages"):
        for card in section.xpath(class_xpath("org-view-entity-card__container")):
            content = first(card, class_xpath("artdeco-entity-lockup__content"))
            anchor = first(content, ".//a[@href]") if content is not None else None
            url = (
                resolve_url(anchor.get("href"), company_url, keep_query=False)
                if anchor is not None
                else None
            )
            if not url or url in seen:
                continue
            seen.add(url)
            image = first(card, ".//img[@src]")
            pages.append(
                {
                    "name": text(first(card, class_xpath("org-view-entity-card__title"))),
                    "url": url,
                    "industry": text(
                        first(card, class_xpath("org-view-entity-card__subtitle"))
                    ),
                    "relationship": text(
                        first(card, class_xpath("org-view-entity-card__secondary-subtitle"))
                    ),
                    "image_url": image.get("src") if image is not None else None,
                }
            )
    if pages:
        return pages

    heading = first(
        document,
        ".//main//*[self::h1 or self::h2 or self::h3 or self::h4]"
        "[normalize-space()='Affiliated pages']",
    )
    section = next(
        (
            scope for scope in heading.iterancestors()
            if scope.tag != "main"
            and scope.xpath(
                ".//a[@href][contains(@href, '/company/') "
                "or contains(@href, '/school/') or contains(@href, '/showcase/')]"
            )
        ),
        None,
    ) if heading is not None else None
    if section is not None:
        for anchor in section.xpath(
            ".//a[@href][contains(@href, '/company/') "
            "or contains(@href, '/school/') or contains(@href, '/showcase/')]"
        ):
            url = resolve_url(anchor.get("href"), company_url, keep_query=False)
            if not url or url in seen or url == company_url:
                continue
            values = [text(node) for node in anchor.xpath(".//h3 | .//p")]
            values = [value for value in values if value]
            if not values:
                continue
            seen.add(url)
            image = first(anchor, ".//img[@src]")
            pages.append({
                "name": values[0],
                "url": url,
                "industry": values[1] if len(values) > 1 else None,
                "relationship": values[2] if len(values) > 2 else None,
                "image_url": image.get("src") if image is not None else None,
            })
    return pages
