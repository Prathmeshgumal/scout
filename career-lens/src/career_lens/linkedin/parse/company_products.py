"""Rendered product details from a LinkedIn company Product tab."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

from lxml.html import HtmlElement

from .company_common import class_xpath, first, resolve_url, text


PRODUCT_NAME = re.compile(r"^What is (?P<name>.+)\?$", re.IGNORECASE)
MEDIA_ASSET = re.compile(r"urn:li:digitalmediaAsset:(?P<identifier>[^/?&\s\"']+)")


def _walk_json(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _media_asset_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = MEDIA_ASSET.search(value)
    return match.group("identifier") if match else None


def _embedded_video_urls(document: HtmlElement) -> dict[str, str]:
    urls: dict[str, str] = {}
    for code in document.xpath(".//code"):
        raw = code.text_content().strip()
        if not raw or raw[0] not in "{[":
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for value in _walk_json(payload):
            record_type = value.get("$type")
            if not isinstance(record_type, str) or not record_type.endswith(
                ".OrganizationProduct"
            ):
                continue
            media_sections = value.get("mediaSections")
            if not isinstance(media_sections, list):
                continue
            for media_section in media_sections:
                if not isinstance(media_section, dict):
                    continue
                media_content = media_section.get("mediaContent")
                if not isinstance(media_content, dict):
                    continue
                embedded_video = media_content.get("embeddedVideo")
                if not isinstance(embedded_video, dict):
                    continue
                thumbnail = embedded_video.get("thumbnail")
                if not isinstance(thumbnail, dict):
                    continue
                asset_id = _media_asset_id(thumbnail.get("digitalmediaAsset"))
                video_url = embedded_video.get("url")
                if asset_id and isinstance(video_url, str) and video_url:
                    urls[asset_id] = video_url
    return urls


def _media(document: HtmlElement) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    video_urls = _embedded_video_urls(document)
    media_items = document.xpath(
        class_xpath("products-media-section__featured-item")
        + " | "
        + class_xpath("products-media-section__sub-item")
    )
    if not media_items:
        heading = first(document, ".//main//h2[normalize-space()='Product media']")
        if heading is not None:
            for scope in heading.iterancestors():
                if scope.tag == "main":
                    break
                if scope.xpath(".//img[@src]"):
                    media_items = scope.xpath(".//img[@src]")
                    break
    for media_item in media_items:
        image = (
            media_item
            if media_item.tag == "img"
            else first(
                media_item,
                ".//img[contains(@class, 'products-media-section__thumbnail')]",
            )
        )
        if image is None:
            continue
        is_video = bool(
            media_item.xpath(
                ".//*[contains(@class, "
                "'products-media-section__video-icon-wrapper')]"
            )
        )
        item: dict[str, object] = {
            "type": "video" if is_video else "image",
            "thumbnail_url": image.get("src"),
            "alt_text": image.get("alt") or None,
        }
        if is_video:
            thumbnail_url = image.get("src") or ""
            item["video_url"] = next(
                (
                    video_url
                    for asset_id, video_url in video_urls.items()
                    if asset_id in thumbnail_url
                ),
                None,
            )
        items.append(item)
    return items


def _features(document: HtmlElement, source_url: str) -> dict[str, object] | None:
    container = first(document, class_xpath("products-top-rated-features__main"))
    if container is None:
        heading = first(document, ".//main//h2[normalize-space()='Top rated features']")
        if heading is None:
            return None
        container = next(
            (
                scope for scope in heading.iterancestors()
                if scope.tag != "main" and scope.xpath(".//h3")
            ),
            None,
        )
        if container is None:
            return None
    items: list[dict[str, object]] = []
    features = container.xpath(class_xpath("products-top-rated-features__feature-item"))
    if not features:
        features = container.xpath(".//*[self::div or self::li][./h3 and .//p]")
    for feature in features:
        items.append(
            {
                "name": text(
                    first(
                        feature,
                        class_xpath("products-top-rated-features__feature-item-title")
                        + " | .//h3",
                    )
                ),
                "description": text(first(feature, ".//p")),
            }
        )
    source = first(container, ".//a[@href][.//img[contains(@alt, 'G2')]]")
    return {
        "description": text(
            first(
                container,
                class_xpath("products-top-rated-features__subtitle")
                + " | .//h2[normalize-space()='Top rated features']/following-sibling::p[1]"
                + " | .//p[1]",
            )
        ),
        "items": items,
        "source": (
            {"name": "G2", "url": resolve_url(source.get("href"), source_url)}
            if source is not None
            else None
        ),
    }


def extract_product(document: HtmlElement, source_url: str) -> dict[str, object]:
    heading = text(
        first(document, class_xpath("products-about__consistant-ui-card-header"))
    )
    if not heading:
        heading = next(
            (
                value for node in document.xpath(".//main//h2")
                if (value := text(node)) and PRODUCT_NAME.match(value)
            ),
            None,
        )
    match = PRODUCT_NAME.match(heading or "")
    if (
        document.xpath(".//main//h2[starts-with(normalize-space(), 'What is ')]")
        and match is None
    ):
        raise ValueError("LinkedIn company Product content could not be read")
    description = first(
        document, class_xpath("products-about__consistant-ui-card-description")
    )
    if description is None and heading:
        heading_node = next(
            (node for node in document.xpath(".//main//h2") if text(node) == heading),
            None,
        )
        if heading_node is not None:
            description = next(
                (
                    paragraphs[0]
                    for scope in heading_node.iterancestors()
                    if scope.tag != "main"
                    if (paragraphs := scope.xpath(".//p"))
                ),
                None,
            )
    return {
        "name": match.group("name") if match else heading,
        "source_url": source_url,
        "description": text(description),
        "media": _media(document),
        "top_rated_features": _features(document, source_url),
    }
