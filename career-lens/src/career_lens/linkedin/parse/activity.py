"""Activity cards, read the way the feed renders them.

One card is one `feed-shared-update-v2`, and every field worth keeping already
sits in a named component inside it -- the actor block, the commentary, the
media, the social counts. Reading those components is the whole technique.
Reconstructing a card from its text lines is not: the activity page also holds a
tab bar and a media-player modal that read as plausible first lines, which is
how the previous parser came to report the modal as a post.

Two details of LinkedIn's markup shape everything here:

**Every label is written twice.** A name renders as
`<span aria-hidden="true">Karthika V S</span><span class="visually-hidden">
Karthika V S</span>`, so naive text extraction returns "Karthika V SKarthika V
S". The visually-hidden copy is dropped before any text is read.

**A card can contain another card.** A repost with commentary nests the original
post's actor and text inside `feed-shared-update-v2__update-content-wrapper`.
That subtree is removed before the outer card is read and parsed separately as
`shared_post`, so the two authors never merge into one.

Times come from the `data-urn`, which carries a millisecond timestamp in its top
bits -- exact, where the rendered "2d" is a bucket. It dates the *activity*: for
a repost that is when it was reposted, while the "3w" beside it is the age of
the original. Both are kept, neither is invented.

Both tabs emit one shape: a card is a `post`, plus the `thread` rendered under
it and this profile's own `comments` lifted out of that thread. The Comments tab
needs the lift because its answer moves -- someone commenting on another's post
is a root comment, someone answering on their own post is a reply nested a level
down, and across four captured profiles it was the reply in 13 of 36 cards. It
also needs `post` to be a key rather than the item itself: on that tab the
author is somebody else, and a top-level `author` reads as this profile's.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit, urlunsplit

from lxml.html import HtmlElement

from .dom import clean_text


CARD = (
    ".//div[contains(@class, 'feed-shared-update-v2')]"
    "[@data-urn"
    " or .//div[contains(@class, 'feed-shared-update-v2__control-menu-container')]]"
)
SHARED = ".//*[contains(@class, 'feed-shared-update-v2__update-content-wrapper')]"
ACTOR = ".//*[contains(@class, 'update-components-actor__container')]"
COMMENT_ENTITY = (
    ".//*[contains(concat(' ', normalize-space(@class), ' '), ' comments-comment-entity ')]"
)
REPLY_ENTITY = (
    ".//*[contains(concat(' ', normalize-space(@class), ' '),"
    " ' comments-comment-entity--reply ')]"
)
HIDDEN = (
    ".//span[contains(@class, 'visually-hidden')]"
    " | .//*[contains(@class, 'see-more-less-toggle')]"
    " | .//*[contains(@class, 'update-components-actor__follow-button')]"
)

# "Loaded 20 Posts posts", the page's own count of what it fetched. Compared
# against the cards actually present, which is fewer whenever LinkedIn has
# recycled the ones scrolled past.
LOADED = re.compile(r"Loaded\s+(\d+)\s+\S+\s+posts", re.IGNORECASE)
# What LinkedIn writes where a tab's contents would be when the person has
# none. A tab that says this was captured and is genuinely empty, which is a
# different fact from a tab we could not read.
EMPTY_FEED = re.compile(r"Nothing to see for now", re.IGNORECASE)
# The Articles tab is not a feed. Each article is a list item holding a link,
# a cover, a title, a clamped preview and a byline -- no urn, no actor block,
# no social counts, because the tab renders none of those.
ARTICLE = ".//li[contains(@class, 'profile-creator-shared-article__container')]"
BYLINE = re.compile(r"^\s*by\s+(?P<author>.+?)\s*(?:•|$)", re.IGNORECASE)
READ_TIME = re.compile(r"(\d+)\s*min read", re.IGNORECASE)
FOLLOWERS = re.compile(r"([\d,]+)\s+followers?", re.IGNORECASE)
COUNT = re.compile(r"([\d,]+)")
# "2d • Edited • Visible to anyone on or off LinkedIn", any part of which may be
# absent. The age is always first.
AGE = re.compile(r"^\s*([\d]+\s*(?:s|m|h|d|w|mo|yr)|\w+)\s*(?:•|$)", re.IGNORECASE)
VISIBILITY = re.compile(r"Visible to (?P<who>.+?)\s*$", re.IGNORECASE)
ACTIVITY_ID = re.compile(r"urn:li:(?:activity|share|ugcPost):(?P<id>\d+)")
REACTION_TYPE = re.compile(r"reactions-icon-type-(?P<kind>[A-Z_]+)")
SPACE = re.compile(r"[^\S\n]+")
BLANK_LINES = re.compile(r"\n{2,}")
BREAK_TAGS = frozenset({"br"})
BLOCK_TAGS = frozenset({"div", "p", "li", "ul", "ol", "section", "article"})
HASHTAG = re.compile(r"^hashtag\s*", re.IGNORECASE)
BADGES = {"verified": "Verified", "premium": "Premium"}
# How the Reactions tab words each of LinkedIn's reactions, in the header above
# the post. Which one this person gave -- not the aggregate across everyone,
# which is `engagement.reactions.types` and a different fact.
REACTIONS = (
    ("likes this", "like"),
    ("loves this", "love"),
    ("celebrates this", "celebrate"),
    ("finds this insightful", "insightful"),
    ("supports this", "support"),
    ("finds this funny", "funny"),
    ("is curious about this", "curious"),
)
ORGANISATION_PATHS = ("/company/", "/school/", "/showcase/")
# Which media component a class name names. Ordered most specific first: an
# article carries an image, and a document carries both.
MEDIA_TYPES = (
    ("update-components-linkedin-video", "video"),
    ("update-components-external-video", "video"),
    ("update-components-document", "document"),
    ("update-components-poll", "poll"),
    ("update-components-celebration", "celebration"),
    ("update-components-article", "article"),
    ("update-components-image", "image"),
)
# LinkedIn's Snowflake-style ids put the epoch milliseconds above the low 22
# bits of sequence and worker.
URN_TIMESTAMP_SHIFT = 22


def _without(element: HtmlElement, expression: str) -> HtmlElement:
    """Return a detached copy with everything `expression` matches removed."""
    copied = deepcopy(element)
    for node in reversed(copied.xpath(expression)):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    return copied


def _text(element: HtmlElement | None) -> str | None:
    if element is None:
        return None
    return clean_text(_without(element, HIDDEN)).strip() or None


def _body_text(element: HtmlElement | None) -> str | None:
    """Post commentary, with its line breaks kept and its links left inline.

    Not `clean_text`: that treats an anchor inside a div as a block, which
    splits a hashtag from its own text -- "#agenticai" becomes "# agenticai",
    and a mention becomes "@ Yellow.ai".
    """
    if element is None:
        return None
    copied = _without(element, HIDDEN)
    pieces: list[str] = []

    def walk(node: HtmlElement) -> None:
        if not isinstance(node.tag, str):
            return
        if node.tag in BREAK_TAGS:
            pieces.append("\n")
        if node.text:
            pieces.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                pieces.append(child.tail)
        if node.tag in BLOCK_TAGS:
            pieces.append("\n")

    walk(copied)
    lines = [SPACE.sub(" ", line).strip() for line in "".join(pieces).split("\n")]
    body = "\n".join(lines)
    return BLANK_LINES.sub("\n\n", body).strip() or None


def _one(element: HtmlElement, class_name: str, *, exact: bool = False) -> HtmlElement | None:
    """First descendant carrying `class_name`.

    Substring by default, because LinkedIn suffixes variants onto a stem
    (`__subtitle--inset`). Set `exact` where a longer class would otherwise
    win: `__sub-description-button-text` precedes `__sub-description` in the
    actor block and holds "Book an appointment", not a date.
    """
    if exact:
        found = element.xpath(
            f".//*[contains(concat(' ', normalize-space(@class), ' '), ' {class_name} ')]"
        )
    else:
        found = element.xpath(f".//*[contains(@class, '{class_name}')]")
    return found[0] if found else None


def _number(value: str | None) -> int | None:
    if not value:
        return None
    match = COUNT.search(value)
    return int(match.group(1).replace(",", "")) if match else None


def _url(href: str | None, source_url: str, *, keep_query: bool = False) -> str | None:
    """Resolve a href, dropping the tracking query LinkedIn appends to actors."""
    if not href or not href.strip():
        return None
    absolute = urljoin(source_url, href.strip())
    if keep_query:
        return absolute
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _activity_id(card: HtmlElement) -> str | None:
    urn = (card.get("data-urn") or "").strip()
    return urn or None


def _activity_at(urn: str | None) -> str | None:
    match = ACTIVITY_ID.search(urn or "")
    if match is None:
        return None
    milliseconds = int(match.group("id")) >> URN_TIMESTAMP_SHIFT
    # A shift that lands outside LinkedIn's lifetime means the id is not the
    # shape assumed here, and a wrong date is worse than none.
    if not 1_100_000_000_000 < milliseconds < 4_000_000_000_000:
        return None
    moment = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _permalink(urn: str | None) -> str | None:
    return f"https://www.linkedin.com/feed/update/{urn}/" if urn else None


def _relation(card: HtmlElement) -> tuple[str, str | None]:
    """Why this card is on this profile: authored, or acted on."""
    header = _text(_one(card, "update-components-header__text-view"))
    if not header:
        return "authored", None
    lowered = header.lower()
    if "repost" in lowered:
        return "reposted", header
    # Checked before "comment": the header for a reply reads "replied to X's
    # comment on this", which the comment test would otherwise swallow.
    if "replied" in lowered:
        return "replied", header
    if "comment" in lowered:
        return "commented", header
    if _reaction(header) or "reacted" in lowered:
        return "reacted", header
    return "other", header


def _reaction(header: str | None) -> str | None:
    """Which reaction the header names, if it names one."""
    lowered = (header or "").casefold()
    for wording, kind in REACTIONS:
        if wording in lowered:
            return kind
    return None


def _badges(container: HtmlElement) -> list[str]:
    badges: list[str] = []
    for icon in container.xpath(".//svg[@data-test-icon]"):
        name = (icon.get("data-test-icon") or "").lower()
        for token, label in BADGES.items():
            if token in name and label not in badges:
                badges.append(label)
    return badges


def _author(container: HtmlElement | None, source_url: str) -> dict[str, object] | None:
    if container is None:
        return None
    title = _one(container, "update-components-actor__title")
    # The degree and badges share the title element; only the name is wanted.
    name = _text(_without(title, ".//*[contains(@class, 'update-components-actor__supplementary-actor-info')]")) if title is not None else None
    link = container.xpath(".//a[contains(@class, 'update-components-actor__meta-link')]/@href")
    profile_url = _url(link[0], source_url) if link else None
    description = _text(_one(container, "update-components-actor__description"))
    # An actor with no photo renders a ghost placeholder holding no image, so
    # the absence of a src is the absence of an avatar.
    avatar = container.xpath(".//*[contains(@class, 'update-components-actor__avatar')]//img/@src")
    is_organisation = bool(profile_url and any(path in profile_url for path in ORGANISATION_PATHS))
    followers = None
    headline = description
    # An organisation's description worker holds its follower count; a person's
    # holds their headline. They are never both.
    if description and (match := FOLLOWERS.search(description)):
        followers = int(match.group(1).replace(",", ""))
        headline = None
    return {
        "name": name,
        "headline": headline,
        "profile_url": profile_url,
        "avatar_url": avatar[0] if avatar else None,
        "badges": _badges(container),
        "followers": followers,
        "is_organization": is_organisation,
    }


def _posted(container: HtmlElement | None) -> dict[str, object]:
    posted: dict[str, object] = {"relative": None, "edited": False, "visibility": None}
    if container is None:
        return posted
    element = _one(container, "update-components-actor__sub-description", exact=True)
    if element is None:
        return posted
    parts = [part.strip() for part in (_text(element) or "").split("•") if part.strip()]
    if parts:
        posted["relative"] = parts[0]
    posted["edited"] = any(part.lower() == "edited" for part in parts)
    # Visibility is written only into the screen-reader copy, so it is read from
    # the whole element rather than the visible half.
    for part in " ".join(element.text_content().split()).split("•"):
        if match := VISIBILITY.match(part.strip()):
            posted["visibility"] = match.group("who")
    return posted


def _commentary(card: HtmlElement, source_url: str) -> dict[str, object]:
    element = _one(card, "update-components-text")
    body = _body_text(element)
    mentions: list[dict[str, str]] = []
    hashtags: list[str] = []
    links: list[dict[str, str]] = []
    _anchors(element, source_url, mentions, hashtags, links)
    return {
        "text": body,
        # The body is clamped behind a "…more" toggle. Saying so beats handing
        # back a partial string that reads as complete.
        "truncated": bool(card.xpath(".//*[contains(@class, 'see-more-less-toggle')]")),
        "mentions": mentions,
        "hashtags": hashtags,
        "links": links,
    }


def _anchors(
    element: HtmlElement | None,
    source_url: str,
    mentions: list[dict[str, str]],
    hashtags: list[str],
    links: list[dict[str, str]],
) -> None:
    """Sort the links in a body into the three kinds LinkedIn renders."""
    if element is None:
        return
    for anchor in element.xpath(".//a[@href]"):
        text = " ".join(anchor.text_content().split())
        url = _url(anchor.get("href"), source_url, keep_query=True)
        if not text or not url:
            continue
        # A hashtag anchor labels itself "hashtag" for screen readers and
        # points at a keyword search, not at the tag.
        if text.lower().startswith("hashtag") or text.startswith("#"):
            tag = HASHTAG.sub("", text).strip()
            if tag and tag not in hashtags:
                hashtags.append(tag)
        elif "/in/" in url or any(path in url for path in ORGANISATION_PATHS):
            mention = {"name": text, "url": _url(anchor.get("href"), source_url) or url}
            if mention not in mentions:
                mentions.append(mention)
        elif {"text": text, "url": url} not in links:
            links.append({"text": text, "url": url})


def _media_type(element: HtmlElement) -> str | None:
    classes = element.get("class") or ""
    for token, kind in MEDIA_TYPES:
        if token in classes:
            return kind
    return None


def _media(card: HtmlElement, source_url: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    seen: set[tuple[str, str | None]] = set()
    for element in card.xpath(".//*[@class]"):
        kind = _media_type(element)
        if kind is None:
            continue
        # Each component wraps itself in several divs sharing the class stem.
        if any(_media_type(parent) == kind for parent in element.iterancestors()):
            continue
        item: dict[str, object] = {"type": kind}
        if kind == "article":
            href = element.get("href") or (element.xpath(".//a/@href") or [None])[0]
            item["url"] = _url(href, source_url, keep_query=True)
            item["title"] = _text(_one(element, "update-components-article__title"))
            item["domain"] = _text(_one(element, "update-components-article__subtitle"))
        elif kind == "image":
            images = element.xpath(".//img/@src")
            item["url"] = images[0] if images else None
            alt = element.xpath(".//img/@alt")
            item["alt"] = alt[0].strip() if alt and alt[0].strip() else None
            if extra := _text(_one(element, "update-components-image__excess-image-count-text")):
                item["more_count"] = _number(extra)
        else:
            item["title"] = _text(element)
            posters = element.xpath(".//video/@poster | .//img/@src")
            item["thumbnail_url"] = posters[0] if posters else None
        key = (kind, str(item.get("url") or item.get("title") or "")[:200])
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _engagement(card: HtmlElement) -> dict[str, object]:
    counts = _one(card, "social-details-social-counts")
    reaction_types: list[str] = []
    reactions: dict[str, object] = {"count": None, "types": reaction_types}
    engagement: dict[str, object] = {
        "reactions": reactions,
        "comments": None,
        "reposts": None,
    }
    if counts is None:
        return engagement
    # LinkedIn omits a count entirely rather than rendering a zero, so an
    # absent item inside a bar that exists means none, not unknown.
    engagement.update({"comments": 0, "reposts": 0})
    reactions["count"] = _number(
        _text(_one(counts, "social-details-social-counts__reactions-count"))
    ) or 0
    for item in counts.xpath(".//*[contains(@class, 'social-details-social-counts__item')]"):
        label = _text(item) or ""
        lowered = label.lower()
        if "comment" in lowered:
            engagement["comments"] = _number(label)
        elif "repost" in lowered:
            engagement["reposts"] = _number(label)
    # The reaction icons name themselves in a test hook -- LIKE, PRAISE,
    # EMPATHY -- which survives the CDN filename changing under us.
    for icon in counts.xpath(".//*[contains(@class, 'reactions-icon')]"):
        for match in REACTION_TYPE.finditer(icon.get("class") or ""):
            kind = match.group("kind").lower()
            if kind not in reaction_types:
                reaction_types.append(kind)
    return engagement


def _thread_parent(entity: HtmlElement) -> HtmlElement | None:
    """The comment this one hangs from, or None when it starts a thread."""
    for ancestor in entity.iterancestors():
        classes = " ".join((ancestor.get("class") or "").split())
        if " comments-comment-entity " in f" {classes} ":
            return ancestor
    return None


def _comment(entity: HtmlElement, source_url: str, owner: str | None) -> dict[str, object]:
    """One comment, with its replies nested under it as the thread shows them.

    A reply is another `comments-comment-entity` *inside* the comment it
    answers, so the parent is read with replies removed -- otherwise the
    reply's author and text are appended to the comment being replied to.
    """
    replies = [
        child for child in entity.xpath(COMMENT_ENTITY) if _thread_parent(child) is entity
    ]
    own = _without(entity, REPLY_ENTITY)
    href = own.xpath(".//a[contains(@class, 'comments-comment-meta__image-link')]/@href")
    profile_url = _url(href[0], source_url) if href else None
    mentions: list[dict[str, str]] = []
    _anchors(_one(own, "comments-comment-item__main-content"), source_url, mentions, [], [])
    badges = [_text(badge) for badge in own.xpath(".//*[contains(@class, 'comments-comment-meta__badge')]")]
    return {
        "author": {
            "name": _text(_one(own, "comments-comment-meta__description-title")),
            "headline": _text(_one(own, "comments-comment-meta__description-subtitle")),
            "profile_url": profile_url,
        },
        "relative": _text(_one(own, "comments-comment-meta__info")),
        "text": _body_text(_one(own, "comments-comment-item__main-content")),
        "truncated": bool(own.xpath(".//*[contains(@class, 'see-more-less-toggle')]")),
        "mentions": mentions,
        "reactions": _number(_text(_one(own, "comments-comment-social-bar__reactions-count--cr"))),
        # LinkedIn badges the comment written by whoever wrote the post.
        "is_post_author": any((badge or "").casefold() == "author" for badge in badges),
        "is_profile_owner": _handle(profile_url) == owner if owner else False,
        "reply_count": _number(_text(_one(own, "comments-comment-social-bar__replies-count--cr"))),
        # "See more replies" means the thread continues past what was captured.
        "replies_truncated": bool(own.xpath(".//*[contains(@class, 'load-replies')]")),
        "replies": [_comment(reply, source_url, owner) for reply in replies],
    }


def _thread(card: HtmlElement, source_url: str, owner: str | None) -> list[dict[str, object]]:
    """The comment threads rendered under a card, as the page shows them."""
    return [
        _comment(entity, source_url, owner)
        for entity in card.xpath(COMMENT_ENTITY)
        if _thread_parent(entity) is None
    ]


def _own_comments(
    thread: list[dict[str, object]],
    parent: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """This profile's own contributions, lifted out of wherever they sit.

    The Comments tab answers "what did this person say", and the answer is a
    root comment when they commented on someone else's post but a reply when
    they answered someone on their own. Hunting both levels is the consumer's
    job only if the shape refuses to do it here.
    """
    found: list[dict[str, object]] = []
    for comment in thread:
        if comment["is_profile_owner"]:
            found.append({
                "text": comment["text"],
                "relative": comment["relative"],
                "reactions": comment["reactions"],
                "truncated": comment["truncated"],
                "mentions": comment["mentions"],
                "is_reply": parent is not None,
                "in_reply_to": None if parent is None else {
                    "name": parent["author"]["name"],
                    "profile_url": parent["author"]["profile_url"],
                },
            })
        found.extend(_own_comments(comment["replies"], comment))
    return found


def _cards(document: HtmlElement, source_url: str) -> list[HtmlElement]:
    """Every top-level card, in the order the feed shows them."""
    found = document.xpath(CARD)
    if not found and any(
        path in urlsplit(source_url).path for path in ORGANISATION_PATHS
    ):
        found = document.xpath(
            ".//*[@role='listitem'][.//h2[normalize-space()='Feed post'] "
            "or starts-with(@componentkey, 'update-card-')]"
        )
    identifiers = {id(card) for card in found}
    return [
        card
        for card in found
        if not any(id(parent) in identifiers for parent in card.iterancestors())
    ]


def _parse_card(
    card: HtmlElement,
    source_url: str,
    *,
    owner: str | None = None,
    nested: bool = False,
) -> dict[str, object]:
    organization_source = any(
        path in urlsplit(source_url).path for path in ORGANISATION_PATHS
    )
    if (
        organization_source
        and not card.xpath(
            ".//*[contains(@class, 'update-components-actor__container')]"
        )
        and (card.xpath(".//h2[normalize-space()='Feed post']")
             or (card.get("componentkey") or "").startswith("update-card-"))
    ):
        return _parse_current_card(card, source_url)
    shared_element = None if nested else _one(card, "feed-shared-update-v2__update-content-wrapper")
    # The reshared original is read first, then removed, so the outer card's
    # actor and commentary cannot pick up the inner post's.
    outer = _without(card, SHARED) if shared_element is not None else card
    container = _one(outer, "update-components-actor__container")
    relation, relation_text = _relation(outer)
    urn = _activity_id(card)

    parsed: dict[str, object] = {
        "author": _author(container, source_url),
        "posted": _posted(container),
        "commentary": _commentary(outer, source_url),
        "media": _media(outer, source_url),
    }
    if nested:
        return parsed

    shared = None
    if shared_element is not None:
        shared = _parse_card(shared_element, source_url, nested=True)
        # A quoted post with no author or text is chrome, not a post.
        if not (shared["author"] or shared["commentary"]["text"]):
            shared = None

    parsed["shared_post"] = shared
    parsed["engagement"] = _engagement(card)
    thread = _thread(card, source_url, owner)
    # One shape for every feed tab: a card is always a post, and a card on the
    # Comments tab is that post plus what this profile said under it. The other
    # tabs carry the same keys empty rather than a different set.
    item: dict[str, object] = {
        "id": urn,
        "permalink": _permalink(urn),
        # The urn dates the post. On the Reactions tab that is when the post
        # was written, not when it was reacted to -- LinkedIn never shows the
        # second, so it is not emitted.
        "activity_at": _activity_at(urn),
        "relation": relation,
        "relation_text": relation_text,
    }
    if reaction := _reaction(relation_text):
        item["reaction"] = reaction
    item["post"] = parsed
    item["comments"] = _own_comments(thread)
    item["thread"] = thread
    return item


def _parse_current_card(card: HtmlElement, source_url: str) -> dict[str, object]:
    """Read LinkedIn's semantic company-feed card without generated classes."""
    urn = _activity_id(card)
    if urn is None:
        for href in card.xpath('.//a[contains(@href, "/feed/update/")]/@href'):
            if match := ACTIVITY_ID.search(urlsplit(href).path):
                urn = match.group(0)
                break
    company_icon = card.xpath(
        ".//*[self::svg or self::div][starts-with(@aria-label, 'View company: ')]"
    )
    company_name = None
    if company_icon:
        company_name = (company_icon[0].get("aria-label") or "").removeprefix(
            "View company: "
        ).strip() or None
    company_link = next(
        (
            anchor for anchor in card.xpath(".//a[@href]")
            if any(path in urlsplit(anchor.get("href") or "").path for path in ORGANISATION_PATHS)
        ),
        None,
    )
    if company_name is None and company_link is not None:
        values = [
            " ".join(node.text_content().split())
            for node in company_link.xpath(".//p")
            if " ".join(node.text_content().split())
        ]
        company_name = values[0] if values else None

    relative = None
    for node in card.xpath(".//p"):
        value = " ".join(node.text_content().split())
        if match := AGE.match(value.replace("â¢", "•")):
            relative = match.group(1)
            break
    visibility = next(
        (
            (node.get("aria-label") or "").removeprefix("Visibility: ").strip()
            for node in card.xpath(".//*[@aria-label]")
            if (node.get("aria-label") or "").startswith("Visibility: ")
        ),
        None,
    )
    commentary_node = next(
        iter(card.xpath(".//*[@data-testid='expandable-text-box']")), None
    )
    commentary_text = _body_text(commentary_node)
    mentions: list[dict[str, str]] = []
    hashtags: list[str] = []
    links: list[dict[str, str]] = []
    _anchors(commentary_node, source_url, mentions, hashtags, links)

    media: list[dict[str, object]] = []
    seen_media: set[str] = set()
    for image in card.xpath(
        ".//img[contains(@src, 'feedshare-') or contains(@src, 'articleshare-')]"
    ):
        url = (image.get("src") or "").strip()
        if not url or url in seen_media:
            continue
        seen_media.add(url)
        alt = (image.get("alt") or "").strip() or None
        media.append({"type": "image", "url": url, "alt": alt})
    profile_url = (
        _url(company_link.get("href"), source_url)
        if company_link is not None else None
    )
    verified = bool(card.xpath(".//*[contains(@aria-label, ' Verified')]"))
    return {
        "id": urn,
        "permalink": _permalink(urn),
        "activity_at": _activity_at(urn),
        "relation": "authored",
        "relation_text": None,
        "post": {
            "author": {
                "name": company_name,
                "headline": None,
                "profile_url": profile_url,
                "avatar_url": None,
                "badges": ["Verified"] if verified else [],
                "followers": None,
                "is_organization": True,
            },
            "posted": {
                "relative": relative,
                "edited": False,
                "visibility": visibility,
            },
            "commentary": {
                "text": commentary_text,
                "truncated": False,
                "mentions": mentions,
                "hashtags": hashtags,
                "links": links,
            },
            "media": media,
            "shared_post": None,
            "engagement": {
                "reactions": {"count": None, "types": []},
                "comments": None,
                "reposts": None,
            },
        },
        "comments": [],
        "thread": [],
    }


def _handle(url: str | None) -> str | None:
    """The `/in/<handle>` segment that names one person."""
    parts = [part for part in urlsplit(url or "").path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "in":
        return parts[1].casefold()
    return None


def _envelope(document: HtmlElement, source_url: str, items: list[dict[str, object]]) -> dict[str, object]:
    """The items, plus what the page said it was going to show."""
    # Numbered first so the reading order of the JSON matches the page's.
    items = [{"position": position} | item for position, item in enumerate(items, start=1)]
    loaded = LOADED.search(document.text_content())
    announced = int(loaded.group(1)) if loaded else None
    return {
        "source_url": source_url,
        "loaded": announced,
        "captured": len(items),
        # LinkedIn recycles cards scrolled past, so a page that announces 20
        # commonly holds fewer by the time the HTML is read.
        "complete": None if announced is None else len(items) >= announced,
        "items": items,
    }


def states_it_is_empty(document: HtmlElement) -> bool:
    """Whether the capture says outright that there is nothing in it.

    Read so that a person with no comments -- or no recommendations -- is
    reported as a section with none, rather than as one whose contents we
    failed to understand. The two are indistinguishable by entry count alone,
    and only one of them is a defect.
    """
    return bool(EMPTY_FEED.search(document.text_content()))


def extract_activity(document: HtmlElement, source_url: str) -> dict[str, object]:
    """Read one activity tab as an ordered list of cards, with its provenance."""
    # Whose activity this is, so a comment written by them can be told from a
    # comment written under them. Names collide and are not links; the handle
    # in the tab's own address is exact.
    owner = _handle(source_url)
    items = [
        _parse_card(card, source_url, owner=owner)
        for card in _cards(document, source_url)
    ]
    return _envelope(document, source_url, items)


def _article(container: HtmlElement, source_url: str) -> dict[str, object] | None:
    """One article, as its card on the Articles tab shows it.

    Nothing here has a `data-urn`, so the `/pulse/` slug is the identity: it is
    stable, unique, and carries LinkedIn's own suffix. The tab shows no date and
    no counts, so neither is emitted -- the article's own page holds those, and
    fetching it is a capture decision rather than a parsing one.
    """
    links = container.xpath(".//a[@href]")
    if not links:
        return None
    anchor = links[0]
    # The tracking id identifies the session that saw the card, not the article.
    url = _url(anchor.get("href"), source_url)
    slug = [part for part in urlsplit(url or "").path.split("/") if part]
    title = _text(_one(container, "profile-creator-shared-article__title"))
    cover = container.xpath(".//img[contains(@class, 'profile-creator-shared-article__image')]/@src")
    # The preview sits between the title and the byline under an obfuscated
    # class, so it is found by position and by being the screen copy.
    previews = [
        child for child in anchor.xpath("./div[@aria-hidden='true']")
        if "profile-creator-shared-article__title" not in (child.get("class") or "")
    ]
    byline = _text((anchor.xpath("./p") or [None])[0])
    author = BYLINE.match(byline or "")
    minutes = READ_TIME.search(byline or "")
    if not (title or url):
        return None
    return {
        "slug": slug[-1] if slug else None,
        "url": url,
        "title": title,
        "preview_text": _body_text(previews[0]) if previews else None,
        # Clamped to two lines by the card, so the preview always stops mid
        # sentence rather than ending.
        "truncated": bool(previews and "is-collapsed" in (previews[0].get("class") or "")),
        "cover_image_url": cover[0] if cover else None,
        "author_name": author.group("author") if author else None,
        "read_time_minutes": int(minutes.group(1)) if minutes else None,
        # Kept whole beside the parsed halves: it is one unclassed string, so a
        # wording change should cost the split fields and not the line itself.
        "byline": byline,
    }


def extract_articles(document: HtmlElement, source_url: str) -> dict[str, object]:
    """Read the Articles tab, which renders a list of articles and not a feed."""
    articles = [_article(container, source_url) for container in document.xpath(ARTICLE)]
    return _envelope(document, source_url, [article for article in articles if article])


def empty_activity() -> dict[str, object]:
    """The same shape, for a tab that was never captured."""
    return {
        "source_url": None,
        "loaded": None,
        "captured": 0,
        "complete": None,
        "items": [],
    }
