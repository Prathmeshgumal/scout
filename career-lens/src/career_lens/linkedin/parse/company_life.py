"""Modules owned by a LinkedIn company Life tab."""

from __future__ import annotations

from lxml.html import HtmlElement

from .activity import extract_activity
from .company_common import BACKGROUND_IMAGE, class_xpath, first, resolve_url, text


def _heading(document: HtmlElement, value: str) -> HtmlElement | None:
    return first(
        document,
        ".//main//*[self::h1 or self::h2 or self::h3 or self::h4]"
        f"[normalize-space()={value!r}]",
    )


def _heading_scope(
    document: HtmlElement, value: str, descendant: str
) -> HtmlElement | None:
    heading = _heading(document, value)
    if heading is None:
        return None
    for scope in heading.iterancestors():
        if scope.tag == "main":
            break
        if scope.xpath(descendant):
            return scope
    return None


def _media(element: HtmlElement, base_url: str) -> dict[str, object] | None:
    iframe = first(element, ".//iframe[@src]")
    if iframe is not None:
        return {
            "type": "video",
            "url": resolve_url(iframe.get("src"), base_url),
            "title": iframe.get("title") or None,
        }

    image = first(element, ".//img[@src]")
    if image is not None:
        return {
            "type": "image",
            "url": resolve_url(image.get("src"), base_url),
            "alt_text": image.get("alt") or None,
        }

    cover = first(element, class_xpath("org-cropped-image__cover-image"))
    match = BACKGROUND_IMAGE.search(cover.get("style") or "") if cover is not None else None
    if match:
        return {
            "type": "image",
            "url": resolve_url(match.group("url").strip(" '\""), base_url),
            "alt_text": None,
        }
    return None


def _module_title(section: HtmlElement) -> str | None:
    title = first(section, class_xpath("org-module__heading-title"))
    if title is None:
        title = first(section, ".//*[self::h1 or self::h2 or self::h3 or self::h4]")
    return text(title)


def _image_url(image: HtmlElement | None, base_url: str) -> str | None:
    if image is None:
        return None
    source = (image.get("src") or "").strip()
    if not source or source.startswith("data:image/"):
        return None
    return resolve_url(source, base_url)


def _available_pages(document: HtmlElement, source_url: str) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    for anchor in document.xpath(
        ".//*[@role='tablist' and @aria-labelledby='target-selection-tabs-title']"
        "//a[@role='tab' and @href]"
    ):
        pages.append(
            {
                "name": text(anchor),
                "url": resolve_url(anchor.get("href"), source_url, keep_query=False),
                "selected": anchor.get("aria-current") == "true",
            }
        )
    return pages


def _employee_content(document: HtmlElement, source_url: str) -> dict[str, object]:
    section = first(document, ".//*[@data-view-name='org-member-life-trending-employee-content']")
    if section is None:
        return {"title": None, "items": []}

    slides = section.xpath(class_xpath("artdeco-carousel__item"))
    scopes = slides or [section]
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for scope in scopes:
        for item in extract_activity(scope, source_url)["items"]:
            identity = str(item.get("id") or item.get("permalink") or "")
            if identity and identity in seen:
                continue
            if identity:
                seen.add(identity)
            items.append(item)
    return {
        "title": _module_title(section),
        "items": [
            {**item, "position": position}
            for position, item in enumerate(items, start=1)
        ],
    }


def _spotlights(document: HtmlElement, source_url: str) -> list[dict[str, object]]:
    spotlights: list[dict[str, object]] = []
    for section in document.xpath(
        ".//*[@data-view-name='org-member-life-company-spotlight']"
    ):
        links = []
        for anchor in section.xpath(".//*[contains(@class, 'media-section-list')]//a[@href]"):
            links.append(
                {
                    "label": text(anchor),
                    "url": resolve_url(anchor.get("href"), source_url),
                }
            )
        media_container = first(section, class_xpath("org-life-custom-modules-module__media"))
        spotlights.append(
            {
                "title": text(first(section, class_xpath("inline-title"))),
                "description": text(
                    first(section, class_xpath("org-life-custom-modules-module__inline-body"))
                ),
                "media": _media(media_container, source_url) if media_container is not None else None,
                "links": links,
            }
        )
    if not spotlights and _heading(document, "Company spotlights") is not None:
        seen: set[tuple[str, str | None]] = set()
        section = _heading_scope(document, "Company spotlights", ".//h3")
        candidates = section.xpath(".//h3") if section is not None else []
        for title in candidates:
            title_value = text(title)
            scope = title.getparent()
            description = text(first(scope, ".//p")) if scope is not None else None
            if not title_value or not description:
                continue
            identity = (title_value, description)
            if identity in seen:
                continue
            seen.add(identity)
            link = first(scope, ".//a[@href]")
            spotlights.append({
                "title": title_value,
                "description": description,
                "media": _media(scope, source_url),
                "links": ([{
                    "label": text(link),
                    "url": resolve_url(link.get("href"), source_url),
                }] if link is not None else []),
            })
    return spotlights


def _featured_members(document: HtmlElement, source_url: str) -> dict[str, object]:
    members: list[dict[str, object]] = []
    section = first(document, ".//*[@data-view-name='org-member-life-page-featured-members']")
    if section is None:
        section = _heading_scope(document, "Meet our team", ".//a[contains(@href, '/in/')]")
    if section is None:
        return {"title": None, "items": []}
    anchors = section.xpath(
        ".//a[@data-view-name='org-member-life-page-featured-members-profile-link']"
    )
    if not anchors:
        anchors = section.xpath(".//a[contains(@href, '/in/')]")
    for anchor in anchors:
        profile_image = first(anchor, class_xpath("org-profile-card-content__profile-picture"))
        background = first(anchor, class_xpath("profile-background-image__image"))
        if profile_image is None:
            profile_image = first(
                anchor,
                ".//img[@src][contains(@src, 'profile-displayphoto') "
                "or not(contains(@src, 'profile-displaybackgroundimage'))]",
            )
        name = text(first(anchor, ".//dl[contains(@class, 'org-profile-card-content__info')]//h3"))
        headline = text(first(anchor, ".//dl[contains(@class, 'org-profile-card-content__info')]//dd"))
        if name is None:
            values = [text(node) for node in anchor.xpath(".//h3 | .//p")]
            values = [value for value in values if value]
            name = values[0] if values else None
            headline = values[1] if len(values) > 1 else None
        members.append(
            {
                "name": name,
                "headline": headline,
                "profile_url": resolve_url(anchor.get("href"), source_url, keep_query=False),
                "image_url": _image_url(profile_image, source_url),
                "background_image_url": _image_url(background, source_url),
            }
        )
    return {"title": _module_title(section), "items": members}


def _photos(document: HtmlElement, source_url: str) -> dict[str, object]:
    photos: list[dict[str, object]] = []
    section = first(document, ".//*[@data-view-name='org-member-life-company-photos']")
    if section is None:
        section = _heading_scope(document, "Company photos", ".//img[@src]")
    if section is None:
        return {"title": None, "items": []}
    photo_nodes = section.xpath(class_xpath("org-life-company-photos-module__photo-button"))
    if not photo_nodes:
        photo_nodes = section.xpath(".//img[@src]")
    for button in photo_nodes:
        image = button if button.tag == "img" else first(button, ".//img[@src]")
        if image is None:
            continue
        dates = [text(node) for node in button.xpath(".//*[contains(@class, 'photo-overlay')]//p")]
        photos.append(
            {
                "image_url": resolve_url(image.get("src"), source_url),
                "alt_text": image.get("alt") or None,
                "posted_on": next((value for value in reversed(dates) if value), None),
            }
        )
    return {"title": _module_title(section), "items": photos}


def _testimonials(document: HtmlElement, source_url: str) -> dict[str, object]:
    section = first(
        document,
        ".//*[@data-view-name='org-member-life-page-employee-testimonials']",
    )
    if section is None:
        return {"title": None, "items": []}

    items: list[dict[str, object]] = []
    for testimonial in section.xpath(class_xpath("org-life-testimonials-module__testimonial")):
        author = first(
            testimonial,
            ".//a[@data-view-name="
            "'org-member-life-page-employee-testimonials-profile-link']",
        )
        profile_image = (
            first(author, class_xpath("org-profile-card-content__profile-picture"))
            if author is not None
            else None
        )
        items.append(
            {
                "text": text(
                    first(testimonial, class_xpath("org-life-testimonials-module__text"))
                ),
                "author": {
                    "name": text(
                        first(
                            author,
                            ".//dl[contains(@class, 'org-profile-card-content__info')]//h3",
                        )
                    )
                    if author is not None
                    else None,
                    "headline": text(
                        first(
                            author,
                            ".//dl[contains(@class, 'org-profile-card-content__info')]//dd",
                        )
                    )
                    if author is not None
                    else None,
                    "profile_url": resolve_url(
                        author.get("href"), source_url, keep_query=False
                    )
                    if author is not None
                    else None,
                    "image_url": _image_url(profile_image, source_url),
                },
            }
        )
    return {"title": _module_title(section), "items": items}


def extract_life(document: HtmlElement, source_url: str) -> dict[str, object]:
    """Read the selected Life page and only the modules rendered on it."""
    hero_section = first(document, class_xpath("hero-media-module"))
    title = text(first(document, ".//*[@id='target-selection-tabs-title']"))
    if title is None:
        title = next(
            (
                value for node in document.xpath(".//main//h2")
                if (value := text(node)) and value.startswith("Careers at ")
            ),
            None,
        )
    result = {
        "source_url": source_url,
        "title": title,
        "available_pages": _available_pages(document, source_url),
        "hero": _media(hero_section, source_url) if hero_section is not None else None,
        "employee_content": _employee_content(document, source_url),
        "spotlights": _spotlights(document, source_url),
        "featured_members": _featured_members(document, source_url),
        "photos": _photos(document, source_url),
        "testimonials": _testimonials(document, source_url),
    }
    if _heading(document, "Company spotlights") is not None and not result["spotlights"]:
        raise ValueError("LinkedIn company Life spotlights could not be read")
    return result
