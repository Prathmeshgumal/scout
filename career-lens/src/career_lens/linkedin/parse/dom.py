"""DOM parsing and lossless primary-content snapshots for LinkedIn captures."""

from __future__ import annotations

import re
from copy import deepcopy
from urllib.parse import urljoin

from lxml import html
from lxml.html import HtmlElement


SPACE = re.compile(r"[\t\r\f\v ]+")
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
ANCHOR_BLOCK_PARENTS = frozenset({"article", "div", "main", "section"})


def parse_document(source: bytes | str) -> HtmlElement:
    """Parse a complete or partial capture with lxml's tolerant HTML parser."""
    parser = html.HTMLParser(encoding="utf-8", recover=True)
    if isinstance(source, bytes):
        return html.fromstring(source, parser=parser)
    return html.fromstring(source.encode("utf-8"), parser=parser)


def _first(document: HtmlElement, expression: str) -> HtmlElement | None:
    matches = document.xpath(expression)
    return matches[0] if matches else None


def primary_content(document: HtmlElement) -> HtmlElement:
    """Return a detached primary-content tree with known LinkedIn chrome removed."""
    selected = _first(
        document,
        "self::main//section[@aria-label='Primary content']"
        " | .//main//section[@aria-label='Primary content']",
    )
    if selected is None:
        selected = _first(document, "self::main//section[1] | .//main//section[1]")
    if selected is None:
        selected = _first(document, "self::main | .//main")
    if selected is None:
        selected = document
    content = deepcopy(selected)
    noise = content.xpath(
        ".//*[self::nav or self::footer]"
        # The video player's own controls. Every embedded video contributes
        # "Play Skip Backward", "Stream Type LIVE", "Playback Rate" and the
        # rest, which is furniture the player draws rather than anything the
        # profile says.
        " | .//*[contains(@class, 'vjs-control-bar')]"
        " | .//*[contains(@class, 'vjs-modal-dialog')]"
        " | .//*[contains(@class, 'vjs-text-track-settings')]"
        " | .//*[contains(@class, 'vjs-control-text')]"
        " | .//*[@data-ad-banner or @data-ad-component]"
        " | .//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'more profiles for you')]"
        " | .//*[contains(translate(@aria-label, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'ad options')]"
    )
    for element in reversed(noise):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    return content


def _boundary(element: HtmlElement) -> bool:
    tag = element.tag.lower() if isinstance(element.tag, str) else ""
    if tag in BLOCK_TAGS:
        return True
    parent = element.getparent()
    parent_tag = parent.tag.lower() if parent is not None and isinstance(parent.tag, str) else ""
    return tag == "a" and parent_tag in ANCHOR_BLOCK_PARENTS


def clean_text(element: HtmlElement) -> str:
    """Return all visible-ish text in source order with stable paragraph breaks."""
    paragraphs: list[str] = []
    words: list[str] = []

    def append(value: str | None) -> None:
        if value and (normalized := SPACE.sub(" ", value.replace("\n", " ")).strip()):
            words.append(normalized)

    def flush() -> None:
        if words:
            paragraphs.append(" ".join(words))
            words.clear()

    def walk(node: HtmlElement) -> None:
        if not isinstance(node.tag, str):
            return
        boundary = _boundary(node)
        if boundary:
            flush()
        append(node.text)
        for child in node:
            walk(child)
            append(child.tail)
        if boundary:
            flush()

    walk(element)
    flush()
    return "\n\n".join(paragraphs)


def unique_links(element: HtmlElement, base_url: str) -> list[dict[str, str]]:
    """Return resolved, visible links once each in source order."""
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for anchor in element.xpath(".//a[@href]"):
        text = SPACE.sub(" ", " ".join(anchor.itertext())).strip()
        url = urljoin(base_url, anchor.get("href", "").strip())
        if not text or not url or (text, url) in seen:
            continue
        seen.add((text, url))
        links.append({"text": text, "url": url})
    return links


def content_snapshot(document: HtmlElement, base_url: str) -> dict[str, object]:
    """Preserve the normalized text and links semantic extraction may not classify."""
    content = primary_content(document)
    return {"text": clean_text(content), "links": unique_links(content, base_url)}
