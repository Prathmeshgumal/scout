"""Featured: the posts, links and documents a profile pins to the top of itself.

The generic reader returned the section's own heading -- `{"name": "Featured"}`
-- while the content sat in the fallback snapshot, 98KB of it on one captured
profile and about half of everything the JSON still kept as raw text.

Each featured item is one anchor pointing at what it features: a post
(`/feed/update/<urn>`), an article (`/pulse/`), a newsletter, or an outside
address. Everything the card shows is inside that anchor -- the kind, the body,
its hashtags, its thumbnail and its counts -- so the anchor is the entry and
nothing here needs a class name, which is as well, because this section is
served by the application whose class names are build hashes.

Two layouts arrive: the section's own page, and a fragment lifted from the
profile page with no `<main>` around it. Anchoring on the links rather than on
the page's furniture reads both.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from lxml.html import HtmlElement

from .activity import _activity_at, _permalink
from .details import parts


# What a featured item can point at. An outside address is featured too, and
# is recognised by not being LinkedIn's own.
INTERNAL = (
    ("/feed/update/", "post"),
    ("/pulse/", "article"),
    ("/newsletters/", "newsletter"),
    ("/posts/", "post"),
)
# The word the card puts above the item. Trusted over the address, because a
# person can feature a link to their own post.
KINDS = {"post", "link", "image", "document", "video", "article", "newsletter"}
# "2K, number of reactions" -- the accessible label beside the shortened count.
REACTIONS = re.compile(r"^(?P<count>[\d.,KkMm]+)\s*,\s*number of reactions$", re.IGNORECASE)
# "132 comments · 4 reposts", either half of which may be missing.
COMMENTS = re.compile(r"([\d,]+)\s+comments?", re.IGNORECASE)
REPOSTS = re.compile(r"([\d,]+)\s+reposts?", re.IGNORECASE)
HASHTAG = re.compile(r"^#\w")
ACTIVITY_URN = re.compile(r"urn:li:(?:activity|share|ugcPost):\d+")
SEARCH = "/search/results/"
REDIRECTOR = "/safety/go/"


def _absolute(href: str | None, source_url: str) -> str | None:
    if not href or not href.strip():
        return None
    url = urljoin(source_url, href.strip())
    if REDIRECTOR in url:
        target = parse_qs(urlsplit(url).query).get("url")
        if target:
            return target[0]
    parts_ = urlsplit(url)
    return urlunsplit((parts_.scheme, parts_.netloc, parts_.path, "", ""))


def _kind_from_url(url: str) -> str | None:
    for path, kind in INTERNAL:
        if path in url:
            return kind
    host = urlsplit(url).netloc.casefold()
    if host and "linkedin.com" not in host:
        return "link"
    return None


def _is_item(url: str | None) -> bool:
    """Whether this address is something a profile features, not chrome."""
    if not url or SEARCH in url:
        return False
    return _kind_from_url(url) is not None


def _count(value: str | None) -> int | None:
    digits = (value or "").replace(",", "").strip()
    return int(digits) if digits.isdigit() else None


def _item(anchor: HtmlElement, source_url: str) -> dict[str, object] | None:
    url = _absolute(anchor.get("href"), source_url)
    if url is None:
        return None
    values = parts(anchor)
    kind = _kind_from_url(url)
    body: list[str] = []
    hashtags: list[str] = []
    engagement: dict[str, object] = {}

    for index, value in enumerate(values):
        if index == 0 and value.casefold() in KINDS:
            kind = value.casefold()
        elif HASHTAG.match(value):
            if value not in hashtags:
                hashtags.append(value)
        elif match := REACTIONS.match(value):
            # LinkedIn shortens the number it draws ("2K") and spells the same
            # figure out for a screen reader. Neither is exact, so the label is
            # kept as written rather than guessed into an integer.
            engagement["reactions_label"] = match.group("count")
        elif COMMENTS.search(value) or REPOSTS.search(value):
            if (comments := COMMENTS.search(value)) and (n := _count(comments.group(1))) is not None:
                engagement["comments"] = n
            if (reposts := REPOSTS.search(value)) and (n := _count(reposts.group(1))) is not None:
                engagement["reposts"] = n
        elif engagement.get("reactions_label") == value:
            # The drawn count repeats the accessible one.
            continue
        else:
            body.append(value)

    entry: dict[str, object] = {}
    if kind:
        entry["kind"] = kind
    entry["url"] = url
    if match := ACTIVITY_URN.search(anchor.get("href") or ""):
        urn = match.group(0)
        entry["id"] = urn
        entry["permalink"] = _permalink(urn)
        if when := _activity_at(urn):
            entry["posted_at"] = when
    if body:
        # A featured post keeps its shape: the breaks are how it was written.
        entry["text"] = "\n".join(body)
    if hashtags:
        entry["hashtags"] = hashtags
    if engagement:
        entry["engagement"] = engagement
    if images := anchor.xpath(".//img/@src"):
        entry["image_url"] = images[0]
    return entry if entry.get("text") or entry.get("id") else None


def extract_featured(document: HtmlElement, source_url: str) -> list[dict[str, object]]:
    """Every pinned item, in the order the section shows them."""
    anchors = [
        anchor for anchor in document.xpath(".//a[@href]")
        if _is_item(_absolute(anchor.get("href"), source_url))
    ]
    identifiers = {id(anchor) for anchor in anchors}
    items: list[dict[str, object]] = []
    seen: set[str] = set()
    for anchor in anchors:
        # Hashtags and outbound links sit inside the item they belong to.
        if any(id(parent) in identifiers for parent in anchor.iterancestors()):
            continue
        entry = _item(anchor, source_url)
        if entry is None or entry["url"] in seen:
            continue
        seen.add(str(entry["url"]))
        items.append({"position": len(items) + 1} | entry)
    return items
