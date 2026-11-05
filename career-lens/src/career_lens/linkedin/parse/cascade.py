"""Reading LinkedIn's own claims about a profile, and finding entries when the
walk to them has changed.

Two mechanisms live here, and neither covers what the other does. The count
contract catches a section LinkedIn numbered; the sweep catches a section it did
not. Certifications is the first and experience the second -- experience carries
no announced total on any profile we hold, so nothing but the sweep guards it.
"""

from __future__ import annotations

import re

from lxml.html import HtmlElement

#: "Show all 5 licenses" -- the number LinkedIn puts on a section's own link.
SHOW_ALL_COUNT = re.compile(r"Show all\s+([\d,]+)\s+\S")

#: The tail of a section link on a profile: /in/<handle>/details/<section>/.
#: That path segment is already the section name this codebase uses, so counts
#: are keyed by href rather than by the displayed noun -- "5 licenses" belongs
#: to `certifications`, and hand-maintaining that translation is a table nobody
#: would keep correct.
DETAILS_SECTION = re.compile(r"/in/[^/]+/details/([a-z][a-z-]*)/?$")
SERVICES_SECTION = re.compile(r"/services/page/[^/]+/?$")
DETAIL_SECTION_NAMES = {"volunteering-experiences": "volunteering"}
MIN_EMPTY_PROFILE_CONTAINERS = 2


def _section_name(route_name: str) -> str:
    """Canonical parser vocabulary for one LinkedIn details-route slug."""
    return DETAIL_SECTION_NAMES.get(route_name, route_name)


def _href_section(href: str) -> str | None:
    """Canonical section proved by one profile-card link, if any."""
    section = DETAILS_SECTION.search(href)
    if section is not None:
        return _section_name(section.group(1))
    return "services" if SERVICES_SECTION.search(href) is not None else None

#: The tail of the activity feed's link. A profile that has never posted
#: carries none of these, which is the same evidence in a different shape.
ACTIVITY_SECTION = re.compile(r"/in/[^/]+/recent-activity/[a-z]+/?$")


def linked_sections(document: HtmlElement) -> set[str]:
    """Every section the profile page offers a "Show all" into.

    `announced_counts` walks these same anchors and keeps only the ones
    stating a number. This keeps the names, because the name is what says the
    section exists at all: LinkedIn renders no empty sections, so a member
    with no patents has no patents link.

    An empty result is not "this member has nothing". It is "we could not
    read a single section link on this page", which is a different fact and
    the reason callers must check it before concluding anything.
    """
    found: set[str] = set()
    for anchor in document.xpath(".//a[@href]"):
        href = (anchor.get("href") or "").split("?")[0]
        section = _href_section(href)
        if section is not None:
            found.add(section)
    return found


def empty_profile_container_count(document: HtmlElement) -> int:
    """Count a mounted profile shell only when every section container is empty.

    The current profile layout reserves several below-card containers before
    scrolling reaches them. Their emptiness is therefore meaningful only on
    the HTML saved after the capture sweep, which the parser's caller records
    separately. A single empty container remains too weak a signal because a
    genuinely sparse profile can have one.
    """
    containers: list[HtmlElement] = []
    screens = document.xpath(".//*[@data-sdui-screen]")
    for screen in screens:
        screen_name = screen.get("data-sdui-screen") or ""
        if not screen_name.endswith(".Profile"):
            continue
        for column in screen.xpath(".//*[@data-testid='lazy-column']"):
            for child in column:
                if not isinstance(child.tag, str):
                    continue
                key = child.get("componentkey") or ""
                if (key.startswith("profileCards")
                        and "TopcardOnly" not in key):
                    containers.append(child)
    if len(containers) < MIN_EMPTY_PROFILE_CONTAINERS:
        return 0
    if any(container.text_content().strip() for container in containers):
        return 0
    return len(containers)


def offers_activity(document: HtmlElement) -> bool:
    """Whether the profile links into an activity feed at all.

    Activity is the one part of a profile that does not live under `details/`,
    so it needs its own look for the same question.
    """
    return any(
        ACTIVITY_SECTION.search((anchor.get("href") or "").split("?")[0])
        for anchor in document.xpath(".//a[@href]")
    )


def offers_contact_info(document: HtmlElement) -> bool:
    """Whether the profile exposes its optional Contact info dialog."""
    return any(
        "contact-info" in (anchor.get("href") or "")
        for anchor in document.xpath(
            ".//main//a[@href] | self::main//a[@href]"
        )
    )


def offers_open_to_work(document: HtmlElement) -> bool:
    """Whether the profile offers the control for its Open to Work dialog.

    LinkedIn gives this card no stable id or route.  The capture interaction
    therefore uses its visible "Show details" control, and extraction uses the
    same evidence to distinguish an ordinary absent card from a dialog the
    browser failed to capture.
    """
    for control in document.xpath(
        ".//main//*[self::a or self::button] | "
        "self::main//*[self::a or self::button]"
    ):
        label = " ".join(control.text_content().split()).casefold()
        if "show details" not in label:
            continue
        cards = control.xpath("ancestor::section[1]")
        if cards and "open to work" in " ".join(
            cards[0].text_content().split()
        ).casefold():
            return True
    return False


#: The activity feed's tab strip. Its tabs are bare buttons carrying no role,
#: so they are found by the group that holds them -- every post on the feed
#: below carries a Like and a Follow, and reading those as tabs would report
#: every member as offering every tab.
ACTIVITY_TAB_STRIP = './/*[@role="group"]//button'


def activity_tabs(document: HtmlElement) -> set[str]:
    """The tabs this member's activity feed offers, folded for comparison.

    LinkedIn builds the strip from what the member has actually produced, so
    one who has published no articles has no Articles tab -- the same evidence
    the profile page gives for its sections, one level down. Raul's feed
    offers Images where another member's offers Articles.

    Empty means the strip could not be read, not that there are no tabs.
    """
    return {
        " ".join(button.text_content().split()).casefold()
        for button in document.xpath(ACTIVITY_TAB_STRIP)
        if button.text_content().strip()
    }


#: A link only a real entry carries: an employer, an issuer, or a school.
#:
#: One marker rather than a table per section, because measurement did not
#: support a table -- experience links either, since people work at
#: universities. A section whose entries carry neither, such as honors or
#: organizations, scores nothing here and is left to the count contract. That
#: is the intended division of labour rather than a gap.
ENTITY_LINK = ".//a[contains(@href, '/company/') or contains(@href, '/school/')]"


def announced_counts(document: HtmlElement) -> dict[str, int]:
    """Every section total the main profile page states, keyed by section.

    Only some sections carry one. Organizations, certifications and honors do;
    experience, skills, featured and interests say "Show all" with no number.
    A section absent from this mapping has made no claim, so there is nothing
    to hold it to -- a real hole rather than an oversight, and the reason
    experience needs a mechanism that does not depend on being counted.
    """
    counts: dict[str, int] = {}
    for anchor in document.xpath(".//a[@href]"):
        href = (anchor.get("href") or "").split("?")[0]
        section = _href_section(href)
        if section is None:
            continue
        stated = SHOW_ALL_COUNT.search(" ".join(anchor.text_content().split()))
        if stated is None:
            continue
        counts[section] = int(stated.group(1).replace(",", ""))
    return counts


def _depth(element: HtmlElement) -> int:
    depth = 0
    parent = element.getparent()
    while parent is not None:
        depth += 1
        parent = parent.getparent()
    return depth


def repeating_entries(scope: HtmlElement, marker: str) -> list[HtmlElement]:
    """The text-bearing children of the level whose children carry `marker`.

    The level is scored by (number of marked children, depth): a level holding
    four entries beats one holding two, and where two tie the deeper one wins,
    since the shallower is a wrapper around the same entries.

    Then **every** text-bearing child of that level is returned, not only the
    marked ones. The marker is how the level is found; it is not a filter. An
    employer with no LinkedIn page is still an experience, and dropping it would
    produce a partial section nothing downstream could detect -- experience
    carries no announced count, so there is no second opinion to catch it.
    Filtering by the marker was measured on a real profile and returned five of
    six entries, silently.

    Two marked children are required. One entry is not a repetition, and
    accepting it would let any wrapper holding a single link claim to be the
    entry level.
    """
    best: HtmlElement | None = None
    best_key: tuple[int, int] | None = None
    for node in scope.iter():
        if not isinstance(node.tag, str):
            continue
        marked = [
            child for child in node
            if isinstance(child.tag, str) and child.xpath(marker)
        ]
        if len(marked) < 2:
            continue
        key = (len(marked), _depth(node))
        if best_key is None or key > best_key:
            best, best_key = node, key
    if best is None:
        return []
    return [
        child for child in best
        if isinstance(child.tag, str) and child.text_content().strip()
    ]
