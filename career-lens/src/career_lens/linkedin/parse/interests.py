"""Interests: the people, companies, groups, newsletters and schools followed.

These pages come from LinkedIn's newer application, where every class name is a
build hash -- `_36d0853c d717982c _0c0f48e0` -- so nothing here can anchor on
one the way the feed does. What is stable is the address each entry links to:
`/in/`, `/company/`, `/groups/`, `/newsletters/` and `/school/` say what kind of
thing an entry is, and the accessible label repeats it in words ("perks,
Company"). Both are read, and they have agreed on every capture so far.

Three shapes have to be handled:

**The name is written twice.** "perks, Company" for a screen reader, then
"perks" for the screen.

**The profile's own link leads every tab.** It is the page header, not an
interest, and is dropped by handle.

**A newsletter's publisher is an anchor inside the newsletter's own anchor.**
Nested, so it is attached to its newsletter rather than emitted as an entry of
its own.

Categories vary by person: LinkedIn builds the strip from what they actually
follow, so a profile with no newsletters has no Newsletters tab at all. The
category is read from whichever radio is checked rather than from the section's
name, which makes each capture self-describing.

Fields are omitted where the page does not carry them, rather than emitted
holding null.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml.html import HtmlElement


# What an entry links to, and what that makes it. Ordered: `/school/` and
# `/company/` are both organisations and only the path separates them.
ENTITY_PATHS = (
    ("/newsletters/", "newsletter"),
    ("/groups/", "group"),
    ("/school/", "school"),
    ("/company/", "company"),
    ("/in/", "person"),
)
# The category strip. The checked radio names the tab this capture holds.
RADIO = ".//*[@role='radio']"
CATEGORIES = {
    "top voices": "top_voices",
    "companies": "companies",
    "groups": "groups",
    "newsletters": "newsletters",
    "schools": "schools",
}
COUNT = re.compile(r"^([\d,]+)\s+(followers?|members?)$", re.IGNORECASE)
# "· 3rd", the distance from the signed-in account. Dropped: it describes the
# operator's network rather than the person followed.
DEGREE = re.compile(r"^[·•]?\s*\d(?:st|nd|rd|th)\+?$")
CADENCE = re.compile(r"^Published\b", re.IGNORECASE)


def _url(href: str | None, source_url: str) -> str | None:
    if not href or not href.strip():
        return None
    parts = urlsplit(urljoin(source_url, href.strip()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _handle(url: str | None) -> str | None:
    parts = [part for part in urlsplit(url or "").path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "in":
        return parts[1].casefold()
    return None


def _kind(url: str) -> str | None:
    for path, kind in ENTITY_PATHS:
        if path in url:
            return kind
    return None


def _parts(element: HtmlElement) -> list[str]:
    """Visible text runs in order, with the immediate repeats collapsed."""
    parts: list[str] = []
    for text in element.itertext():
        value = " ".join(text.split())
        if value and (not parts or parts[-1] != value):
            parts.append(value)
    return parts


def category(document: HtmlElement) -> str | None:
    """Which category this capture holds, per the strip's checked radio."""
    for radio in document.xpath(RADIO):
        if radio.get("aria-checked") == "true":
            label = " ".join(radio.text_content().split()).casefold()
            return CATEGORIES.get(label, label.replace(" ", "_") or None)
    return None


def _entries(document: HtmlElement, source_url: str, owner: str | None) -> list[HtmlElement]:
    anchors = [
        anchor for anchor in document.xpath(".//a[@href]")
        if _kind(_url(anchor.get("href"), source_url) or "")
    ]
    identifiers = {id(anchor) for anchor in anchors}
    entries: list[HtmlElement] = []
    seen: set[str] = set()
    for anchor in anchors:
        url = _url(anchor.get("href"), source_url)
        # A publisher sits inside its newsletter's anchor; it belongs to that
        # entry rather than standing as one.
        if any(id(parent) in identifiers for parent in anchor.iterancestors()):
            continue
        # Every tab opens with the profile's own header link.
        if owner and _handle(url) == owner:
            continue
        if not url or url in seen:
            continue
        seen.add(url)
        entries.append(anchor)
    return entries


def _publisher(anchor: HtmlElement, source_url: str) -> dict[str, str] | None:
    for nested in anchor.xpath(".//a[@href]"):
        url = _url(nested.get("href"), source_url)
        if url and _kind(url) in {"company", "school", "person"}:
            name = " ".join(nested.text_content().split())
            entry = {"name": name} if name else {}
            return (entry | {"url": url}) or None
    return None


def _entry(anchor: HtmlElement, source_url: str) -> dict[str, object] | None:
    url = _url(anchor.get("href"), source_url)
    if url is None:
        return None
    kind = _kind(url)
    parts = _parts(anchor)
    if not parts:
        return None

    # "perks, Company" then "perks": the label repeats the name with its type
    # appended, so the second run is the name whenever the first introduces it.
    name, rest = parts[0], parts[1:]
    if rest and parts[0].startswith(rest[0]) and parts[0] != rest[0]:
        name, rest = rest[0], rest[1:]

    publisher = _publisher(anchor, source_url) if kind == "newsletter" else None
    published_by = publisher.get("name") if publisher else None

    entry: dict[str, object] = {"name": name, "url": url}
    described: list[str] = []
    for part in rest:
        if DEGREE.match(part) or part == published_by:
            continue
        if match := COUNT.match(part):
            field = "members" if match.group(2).lower().startswith("member") else "followers"
            entry[field] = int(match.group(1).replace(",", ""))
        elif CADENCE.match(part):
            entry["cadence"] = part
        else:
            described.append(part)
    if described:
        # A person's line is their headline; a newsletter's is what it is about.
        entry["description" if kind == "newsletter" else "headline"] = " ".join(described)
    if publisher:
        entry["publisher"] = publisher
    if images := anchor.xpath(".//img/@src"):
        entry["image_url"] = images[0]
    return entry


def extract_interests(
    document: HtmlElement,
    source_url: str,
    *,
    owner: str | None = None,
) -> tuple[str | None, list[dict[str, object]]]:
    """The category this capture holds, and everything followed under it."""
    # Whose page this is, so the header link that opens every tab is not read
    # as somebody they follow. The address names them.
    owner = owner or _handle(source_url)
    entries = [_entry(anchor, source_url) for anchor in _entries(document, source_url, owner)]
    return category(document), [entry for entry in entries if entry]
