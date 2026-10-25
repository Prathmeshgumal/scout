"""Following LinkedIn's "Show all" into the sections a profile keeps folded.

A profile shows a truncated version of most of itself -- two roles of six, three
skills of forty -- and everything past the truncation sits behind a "Show all"
control. Capturing the profile as served captures the truncation, so the report
ends up recording a link to the answer rather than the answer.

This module finds those controls, clicks them, and captures what they open. It
is deliberately the only LinkedIn-specific code in the capture path: `capture`
and `run` stay about pages in general, and the shape of one site's profile stays
here.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator
from urllib.parse import urljoin, urlsplit

from patchright.sync_api import TimeoutError as BrowserTimeoutError

from ..capture import interaction, overlays, trace
from ..capture.failures import raise_if_browser_gone
from . import dom
from .featured_navigation import FEATURED_NEXT, MAX_FEATURED_ADVANCES, horizontally_clipped
from ..capture.page import (FEED_SCROLL_PX, LIST_TOTAL_BUDGET_S,
                            SHOT_TIMEOUT_MS, PageCapture, RequestsInFlight,
                            capture_page, scroll_through, wait_until_settled)
from ..capture.naming import slugify
from .recovery import recover_transient_error
from .readiness import (
    PROFILE_LOADING_SELECTOR, PROFILE_LOADING_PASSES, PROFILE_LOADING_WAIT_MS,
)

if TYPE_CHECKING:      # `report` imports the URL rules from here and needs no browser
    from patchright.sync_api import Page

# Every `details/` section that is about the person, fixed rather than
# configurable, and ordered the way a profile is read rather than the way a
# page happened to render: who they are, what others say, what they produced,
# what they hold, and last the things that are mostly links.
#
# Interests is here because its full page carries useful categories that the
# profile preview omits. Activity is not here, but it is captured -- it lives at
# its own route rather than under `details/`, and is handled further down.
#
# An allowlist rather than "anything matching the path shape", so a `details/`
# route LinkedIn invents cannot enter the run before someone has looked at what
# it does. Adding one is this tuple and a test.
SECTIONS = (
    "experience", "education", "skills", "recommendations",
    "volunteering", "test-scores", "patents", "publications", "projects",
    "certifications", "courses", "languages",
    "honors", "organizations", "interests",
    # Last because it yields the least text: pinned posts and media, so mostly
    # a screenshot. Only some profiles offer it as a page; the rest carry the
    # whole card inline, which `capture_featured` reads where it stands.
    "featured", "services",
)
FEATURED = "featured"

# LinkedIn's route slug is not always its section name. Keep that translation
# at the URL boundary so captures, parser statuses and public request options
# all use one canonical vocabulary.
DETAIL_SECTION_NAMES = {
    **{name: name for name in SECTIONS
       if name not in {"services", "volunteering"}},
    "volunteering-experiences": "volunteering",
}

# The exact path a "Show all" control points at. Anchoring both ends is what
# makes the filter correct rather than approximately correct -- a real profile
# carries three near misses that a looser test collects:
#
#   /in/<p>/overlay/<id>/skill-associations-details/       ends in -details/
#   /in/<p>/opportunities/volunteering/details/?profileId=  contains /details/
#   /in/<p>/overlay/browsemap-recommendations/              contains the word
#
# The first two fail the shape; the third fails it and the allowlist both.
DETAILS_PATH = re.compile(r"^/in/([^/]+)/details/([a-z][a-z-]*)/?$")

# A person's Services card expands into LinkedIn's services application rather
# than a profile `details/` route. The proposal form beneath it is an action,
# not another reading of the section, and is deliberately excluded.
SERVICES_PATH = re.compile(r"^/services/page/([^/]+)/?$")

# A profile itself, which is the only page we look for sections on. Company
# pages have no Experience or Patents and are not in scope.
PROFILE_PATH = re.compile(r"^/in/([^/]+)/?$")

# Activity is the one part of a profile that is not under `details/`, so it
# needs its own shape. `all` is where the "Show all" points and what the feed
# opens on; the other two are reached from the tab strip there, and each pushes
# its own address as it is opened. Reactions is out: it is what they liked
# rather than what they wrote.
ACTIVITY_PATH = re.compile(
    r"^/in/([^/]+)/recent-activity/(all|comments|articles|reactions)/?$")
ACTIVITY_ALL = "all"
ACTIVITY_STEM = "activity"
ACTIVITY = f"{ACTIVITY_STEM}-posts"
ACTIVITY_VIEWS = ("Comments", "Articles", "Reactions")

# Each in-scope view, and the tab that reaches it from inside the feed. The
# feed can be entered through any of them: LinkedIn renders a "Show all" only
# for views that have something in them, so a profile that has commented but
# never posted offers `comments` and no `all` at all. Entering through
# whichever one is there and using the tab strip for the rest reaches the same
# four pages -- insisting on `all` reached none of them.
ACTIVITY_TABS = {
    ACTIVITY_ALL: "Posts",
    "comments": "Comments",
    "articles": "Articles",
    "reactions": "Reactions",
}

# The tab strip on the activity feed. Its tabs are bare buttons carrying no
# role, unlike a section's `role="radio"` filters, so they go on their wording
# inside the group holding them. Scoped to that group because "Comment" is also
# the wording under every post on the page.
ACTIVITY_STRIP = 'main [role="group"]'
ACTIVITY_LANDING = "Posts"
ACTIVITY_RESULTS = (".feed-shared-update-v2, "
                    ".profile-creator-shared-article__container")
TAB_TIMEOUT_MS = 5000

# The feed is served by a different application than the profile, and waited for
# on the strip, which that application draws and a shell does not. Neither the
# character floor nor the spinner test a `details/` section uses would do: the
# profile's own application answers this link by pushing the address and drawing
# nothing behind it, leaving `<main>` hidden with the profile still in it. That
# reads as a rendered column to everything except a reader.
#
# Asking for the document settles it, so the click gets a short grace and then
# the address is fetched properly.
ACTIVITY_SETTLE_MS = 4000
ACTIVITY_READY_MS = 20000

NAV_TIMEOUT_MS = 30000
BACK_TIMEOUT_MS = 20000
DETAILS_BACK_TIMEOUT_MS = 5000
DETAILS_BACK_LABEL = "Navigate back to profile main screen"
PROFILE_SCREEN_READY = r"""() => (
  /^\/in\/[^/]+\/?$/.test(location.pathname)
  && document.querySelector('[data-sdui-screen$=".Profile"]')
  && document.querySelector("a[href*='/details/']")
)"""

# A `details/` page is a client-side route: the navigation commits on a nav bar
# and a spinner, and LinkedIn's own JS fetches and renders the section
# afterwards. So `domcontentloaded` is not "the page is here", and capturing on
# it produced 110KB files whose entire section card was a spinner.
#
# The profile pages did not show this only by luck: `overlays.dismiss` and
# `scroll_through` cost a second or two each, which was enough. A section goes
# from click straight to capture, so it has to wait on purpose.
#
# Waiting on `<main>` having text does *not* work, and that was the first
# attempt: `<main>` also holds the "More profiles for you" rail and the footer's
# language list, ~1800 characters of chrome that are present in the shell. Every
# section passed that test instantly and was captured mid-spinner anyway.
#
# What actually separates the two states is the spinner itself. LinkedIn marks
# it `role="progressbar"`, and it lives inside the primary content column, so
# its absence from that column is the signal. Measured across 15 captures the
# split is exact: 13 mid-load captures have one, both complete ones have none.
#
# The second clause is for the window before the spinner mounts, when the column
# holds a heading and some filter tabs and nothing else. Rendered sections carry
# links -- to companies, schools, people, skill overlays -- and an unrendered
# column carries none; the character floor is the fallback for a section whose
# entries happen to link nowhere. The floor is 200 because the largest chrome-
# only column observed is 84 characters ("Skills / All / Industry Knowledge /
# Tools & Technologies / Interpersonal Skills / Other Skills").
#
# `aria-label` is English, so the column is found by position as well: it is the
# first `<section>` in `<main>`, with the aside and the footer after it.
CONTENT_TIMEOUT_MS = 20000
# The section's own column, excluding the "More profiles for you" rail and the
# footer. Everything below measures this and not the page.
PRIMARY_COLUMN = """document.querySelector('main section[aria-label="Primary content"]')
               || document.querySelector('main section')"""
# The same column as a plain selector, for callers that take one rather than an
# expression. Ordered: the labelled section if the page has it, else the first.
PRIMARY_COLUMN_SELECTORS = ['main section[aria-label="Primary content"]',
                            "main section", "main"]

# `was` is the column size before whatever we just did, or null when there was
# nothing there before. It is what keeps a tab switch honest: the previous tab's
# content is still on screen and still passes every other clause, so without
# this the second capture would be a copy of the first.
CONTENT_READY = """([was]) => {
  const primary = %s;
  if (!primary) return false;
  if (primary.querySelector('[role="progressbar"]')) return false;
  const text = primary.innerText.trim();
  if (was !== null && text.length === was) return false;
  const column = primary.querySelector('[data-testid="lazy-column"]');
  return primary.querySelector('a[href]') !== null
      || Boolean(column && column.children.length);
}""" % PRIMARY_COLUMN

CONTENT_SIZE = """() => {
  const primary = %s;
  return primary ? primary.innerText.trim().length : 0;
}""" % PRIMARY_COLUMN

ACTIVITY_CONTENT_READY = """([was, results]) => {
  const primary = %s;
  if (!primary) return false;
  if (primary.querySelector('[role="progressbar"]')) return false;
  const text = primary.innerText.trim();
  if (was !== null && text.length === was) return false;
  const feed = primary.querySelector('.scaffold-finite-scroll');
  if (!feed) return false;
  const emptyState = feed.querySelector(
      '.scaffold-finite-scroll__empty .artdeco-empty-state');
  return Boolean(emptyState || feed.querySelector(results));
}""" % PRIMARY_COLUMN

# A section list is lazy as well as late: LinkedIn renders a first batch and
# appends the rest when the reader reaches the bottom. `scroll_through` stops as
# soon as a wheel moves nothing, and on a list that has just reached its current
# bottom that is one settle pause too early -- a profile advertising "Skills
# (29)" was captured with 20 of them, which reads as a complete list and is not.
#
# So the list is scrolled repeatedly until the column stops growing. Column text
# length is the measure rather than scroll height because it is what we are
# actually there to collect, and it is indifferent to whether the window or an
# inner element did the scrolling.
#
# Bounded, because an endless list would otherwise hold the run open. Finite
# sections use this cap as a guard. Interests is handled separately on the feed
# distance budget because some of its categories can be effectively endless.
LIST_PASSES = 6
LIST_SETTLE_S = 0.8

# Recommendations is the one section that keeps half of itself behind a tab.
# Received is what renders by default; Given -- the recommendations this person
# wrote for other people, which is often the more telling half -- is a click
# away. Both are the same URL, so it is captured as its own document under its
# own name rather than as a second version of the same one.
#
# The tabs are `role="radio"`, not links, so there is nothing for `find_links`
# to have found. Any other section's filters (Skills' "Industry Knowledge",
# "Tools & Technologies") are narrowings of a list that "All" already holds
# whole, so they would only duplicate what was captured.
TAB_VIEWS = {"recommendations": ("Given",)}

RECOMMENDATIONS = "recommendations"
RECOMMENDATIONS_GIVEN = f"{RECOMMENDATIONS}-given"
RECOMMENDATIONS_HEADING = "Recommendations"

# Interests differs from every other details section in two ways. Its filters
# are separate lists rather than narrowings of one list, and which filters are
# present varies by profile. They are discovered from the radio strip instead
# of being invented here. Some lists can keep growing, so every view uses the
# same distance budget as a feed rather than `exhaust_list`.
INTERESTS = "interests"
INTEREST_TABS = 'main [role="radio"]'
INTERESTS_HEADING = "Interests"
CARD_RADIOS = "[role='radio']"

# Some profile cards expose their details link only after a non-default tab is
# selected.  The headings identify the cards; the tab labels and URLs remain
# live discoveries so a category LinkedIn adds does not require a driver edit.
TABBED_PROFILE_CARDS = {
    RECOMMENDATIONS: RECOMMENDATIONS_HEADING,
    INTERESTS: INTERESTS_HEADING,
}

# The parser assigns Recommendations direction from the capture name, unlike
# Interests, whose checked radio names its category.  If Given was the inline
# tab that revealed Show all, return the details page to Received before the
# base `recommendations` capture is read.
PRIMARY_DETAIL_TABS = {RECOMMENDATIONS: "Received"}

# A description longer than a few lines is clamped with an inline "… more". The
# text is all there -- `innerText` returns it and so does the HTML, which is why
# a role reads the same in the profile capture and the section capture -- but
# the *screenshot* shows it cut off, and a stray "… more" lands in the text and
# the Markdown as though something were missing.
#
# The button is `aria-hidden` with `pointer-events: none`; the span inside it
# carries `pointer-events: auto`, which is the part LinkedIn means to be clicked
# and the part a click has to land on.
#
# A button inside a link is left alone. On the Activity card each post preview
# is wrapped in an anchor to the post, so the click event bubbles from the
# button up to it and navigates -- a live run captured a feed page where
# raulsanz's profile should have been. Twelve of his seventeen clipped
# paragraphs are posts like that; the five that are his own descriptions are
# what we are after, and none of them sits inside a link.
CLIP_BUTTON = '[data-testid="expandable-text-button"]'
IN_A_LINK = "el => el.closest('a') !== null"
CLIP_ROUNDS = 40

# When the account was created, when its contact details and photo were last
# touched, and what LinkedIn has verified about it. None of that is anywhere
# else on the profile, and it is the one part of what a profile says about
# itself that the profile did not write.
#
# Not a `details/` page: the menu item's href is the profile itself, so there is
# no URL to open and it has to be clicked.
ABOUT_MEMBER = "about-this-member"

# The profile's own More button, next to Message and Follow. The other visible
# "More" on the page is outside `main`, which is what separates them.
#
# Any tag, not just `a`. LinkedIn renders this control as an anchor on some
# profiles and a plain div on others -- same attribute, same h2 child, and only
# the tag differs -- so an `a` here silently reported "no verification trigger"
# on a profile whose badge was plainly there. Nothing downstream reads an href;
# the control is clicked, not followed. The attribute is already the selective
# part: both shapes carry exactly one of these inside `main`.
ABOUT_TRIGGER = 'main [aria-haspopup="dialog"]:has(h2)'

# The profile's own More button, and the item under it that opens the same
# dialog the badge does. An unverified member has no badge -- there is no
# `aria-haspopup="dialog"` anywhere on such a profile -- but the account
# history behind it exists all the same, and this is the only way to it.
#
# The action's exact accessible name is stable while its wrapper is not: the
# August 24 SDUI build dropped `data-view-name="profile-overflow-button"` and
# left only the button. Scoped to `main` so the global-nav More control is not
# mistaken for this one; post overflow controls use longer accessible names.
ABOUT_OVERFLOW = 'main button[aria-label="More"]'

# What that item is called. LinkedIn has named it both ways and the wording is
# not something a capture can be pinned to, so it is matched rather than
# spelled: any item naming this profile is the one that opens the dialog.
ABOUT_MENU_ITEM = re.compile(r"about this (profile|member)", re.I)

# Read only to say what the menu did offer when none of it matched, so a run
# after a rename names the new item instead of reporting nothing at all.
ABOUT_MENU_ITEMS = '[role="menuitem"]'

# The screen id is unhashed and not English, unlike the menu item that opens it.
ABOUT_DIALOG = ('dialog[data-testid="dialog"]'
                ':has([data-sdui-screen*="ProfileVerificationDetails"])')
ABOUT_TIMEOUT_MS = 15000
# Closing this panel with Escape leaves the pointer on the verification badge
# that opened it. LinkedIn can then keep its hover label over Contact info.
# Its dialog close control is the physical way out: keep both names LinkedIn
# currently exposes, scoped to this one dialog rather than the page.
ABOUT_CLOSE = (f'{ABOUT_DIALOG} button[aria-label="Dismiss"], '
               f'{ABOUT_DIALOG} button[aria-label="Close"]')

# The Contact info dialog: what a member chose to publish about reaching them.
# The profile carries only the link that opens it, so this is the one way to
# what is behind it.
#
# Matched on the href rather than on a label, because the label is translated
# and the path is not. The same anchor the parser reads the link off.
CONTACT_INFO = "contact-info"
CONTACT_TRIGGER = 'main a[href*="contact-info"]'

# Keyed on the heading, which is what the extractor keys on too. Scoped to a
# dialog so the profile's own "Contact info" link cannot match it.
CONTACT_DIALOG = ('[role="dialog"]:has-text("Contact info"), '
                  'dialog[data-testid="dialog"]:has-text("Contact info")')
CONTACT_TIMEOUT_MS = 15000

# A dialog is visible before it holds anything. LinkedIn renders the frame with
# a loader inside it and fills it from a second request, so reading on `visible`
# stored the spinner and the header above it -- 37 characters, against the 344
# the About this member dialog yields.
#
# The same two questions `CONTENT_READY` asks of a section's column, asked of a
# dialog instead: nothing still loading in it, and something actually in it. The
# body rather than the whole dialog, because the header says "Contact info"
# while the body is still empty and would answer the second question on its own.
# The heading, for the predicate below. `CONTACT_DIALOG` cannot serve there:
# `:has-text()` is Playwright's, not CSS, and `document.querySelector` refuses
# it -- so a predicate built on it is never true and every capture waits out
# the timeout instead of reading a dialog that filled in a second.
CONTACT_HEADING = "Contact info"

# What a member will take: job titles, places, on-site or remote, when they can
# start. The profile card says one place and "+4 more"; the whole answer exists
# only behind its Show details control.
#
# Matched on the control's words, which are English and will not survive a
# translated profile -- the same limitation FEATURED_HEADING carries, and for
# the same reason: the card offers no id, no test attribute and no href.
OPEN_TO_WORK = "open-to-work"
OPEN_TO_WORK_CARD_TEXT = 'main :text("Open to work")'
OPEN_TO_WORK_TRIGGER = (
    'a:has-text("Show details"), button:has-text("Show details")')
OPEN_TO_WORK_DIALOG = ('[role="dialog"]:has-text("Open to Work"), '
                       'dialog[data-testid="dialog"]:has-text("Open to Work")')
OPEN_TO_WORK_HEADING = "Open to Work"

# The profile is on screen again. `main` emptied is the state a closed dialog
# can leave behind -- the view unmounts and renders afresh -- and it is also
# the state a torn-down view leaves, so one predicate covers both.
PROFILE_RENDERED = """() => {
  const main = document.querySelector('main');
  return !!main && main.innerText.trim().length > 0;
}"""

DIALOG_READY = """([heading]) => {
  for (const dialog of document.querySelectorAll('dialog[open], [role="dialog"]')) {
    // Anywhere in the dialog, not in a heading: Contact info titles itself
    // with an h2 and Open to Work does not -- its only heading belongs to the
    // toast region beside it -- so a predicate looking for one waited out the
    // timeout on every profile that was open to work.
    if (!dialog.innerText.includes(heading)) continue;
    if (dialog.querySelector('[role="progressbar"], [data-testid="loader"]'))
      return false;
    const body = dialog.querySelector('[data-testid="dialog-content"]') || dialog;
    return body.innerText.trim().length > 0
        || body.querySelector('a[href]') !== null;
  }
  return false;
}"""
MENU_TIMEOUT_MS = 8000
# What a card or dialog is stored as. A clone rather than the node itself, so
# the nodes this drops are dropped from the bytes and not from the page: the
# site's own code is still holding them, and taking one away underneath it is
# what broke the profile behind the Contact info dialog.
#
# The same mark `page._stored_html` honours on a whole page, so a card and a
# page agree about what was ours and what was served.
OUTER_HTML = """el => {
  const copy = el.cloneNode(true);
  for (const ours of copy.querySelectorAll('[data-career-lens-artifact-only]'))
    ours.remove();
  return copy.outerHTML;
}"""

# The Featured card carries no id and no test attribute, so its own heading is
# what finds it and the card is the section that heading sits in. English, and
# there is nothing else on the page to go on.
FEATURED_HEADING = "Featured"
PROFILE_CARD = "xpath=ancestor::section[1]"
FEATURED_CARD = PROFILE_CARD

# Causes is found the same way and for the same reason: the card carries no id
# of its own -- only an obfuscated class and a componentkey that includes the
# profile's own urn -- so its heading is what finds it and the card is the
# section that heading sits in.
CAUSES = "causes"
CAUSES_HEADING = "Causes"


@dataclass(frozen=True)
class SectionLink:
    """One "Show all" control: where it points, and how to click it."""

    name: str
    url: str
    # The href exactly as written in the DOM, which is what a selector has to
    # match. LinkedIn writes them absolute, but a relative one would resolve to
    # the same `url` and still need its own attribute to be found again.
    href: str


@dataclass
class SectionCapture:
    """A captured section, named for the part of the profile it holds.

    Which profile it came from is the caller's to record: a section is stored
    as a page with `parent_id` pointing at the page it was expanded from, and
    that id does not exist until the parent has been written.
    """

    section: str
    capture: PageCapture
    # Why the column had not finished rendering when it was captured, or None.
    # Kept rather than raised: an under-filled capture that says so is worth
    # more than one that looks like an empty section.
    unrendered: str | None = None


def parse_details(url: str) -> tuple[str, str] | None:
    """`(profile handle, section)` for an in-scope section URL, else None.

    Query strings and trailing slashes are ignored, because LinkedIn is not
    consistent about either and both spellings mean the same page.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not _is_linkedin(parts.netloc):
        return None
    match = DETAILS_PATH.match(parts.path)
    if not match:
        return None
    handle, route_name = match.group(1), match.group(2)
    name = DETAIL_SECTION_NAMES.get(route_name)
    return (handle, name) if name is not None else None


def parse_services(url: str) -> str | None:
    """The service-page id for a person's full Services section, if any."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not _is_linkedin(parts.netloc):
        return None
    match = SERVICES_PATH.match(parts.path)
    return match.group(1) if match else None


def parse_activity(url: str) -> tuple[str, str] | None:
    """`(profile handle, view)` for an in-scope activity URL, else None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not _is_linkedin(parts.netloc):
        return None
    match = ACTIVITY_PATH.match(parts.path)
    return (match.group(1), match.group(2)) if match else None


def activity_view_name(view: str) -> str:
    """The parser role for one activity view."""
    return ACTIVITY if view == ACTIVITY_ALL else f"{ACTIVITY_STEM}-{view}"


def activity_from_hrefs(hrefs: Iterable[tuple[str, str]]) -> SectionLink | None:
    """The link into the activity feed, from `(raw href, absolute url)` pairs.

    The landing view when the profile offers it, otherwise whichever view it
    does offer -- the tab strip inside the feed is the same either way, so the
    entry point costs nothing but the order the tabs are visited in.

    `ACTIVITY_PATH` is anchored on the four view names, so an individual post
    under `/feed/update/` is not a candidate and never was.
    """
    found: list[tuple[str, str, str]] = []
    for href, url in hrefs:
        parsed = parse_activity(url)
        if parsed is not None:
            found.append((parsed[1], url, href))
    if not found:
        return None
    view, url, href = next(
        (entry for entry in found if entry[0] == ACTIVITY_ALL), found[0])
    return SectionLink(name=activity_view_name(view), url=url, href=href)


def profile_handle(url: str) -> str | None:
    """The handle in `/in/<handle>`, from a profile URL or a section URL."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = PROFILE_PATH.match(path) or DETAILS_PATH.match(path)
    return match.group(1) if match else None


def is_profile(url: str) -> bool:
    """True for a person profile -- the only page that has sections to expand."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return _is_linkedin(parts.netloc) and PROFILE_PATH.match(parts.path) is not None


def _is_linkedin(netloc: str) -> bool:
    host = netloc.lower().split(":")[0].removeprefix("www.")
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def links_from_hrefs(hrefs: Iterable[tuple[str, str]]) -> list[SectionLink]:
    """In-scope links from `(raw href, absolute url)` pairs, one per section.

    LinkedIn sometimes renders more than one control for the same section --
    a header link and a footer button pointing at the same page -- and one
    capture per section is the intent.
    """
    found: dict[str, SectionLink] = {}
    for href, url in hrefs:
        parsed = parse_details(url)
        name = parsed[1] if parsed is not None else (
            "services" if parse_services(url) is not None else None)
        if name is None or name in found:
            continue
        found[name] = SectionLink(name=name, url=url, href=href)
    # Ordered as SECTIONS lists them, so a report reads the same way whatever
    # order the profile happened to render its sections in.
    return [found[name] for name in SECTIONS if name in found]


def find_links(page: Page) -> list[SectionLink]:
    """The in-scope "Show all" controls on the profile currently open."""
    with trace.phase(trace.WORK, "find sections") as span:
        anchors = page.locator(
            "a[href*='/details/'], a[href*='/services/page/']")
        count = anchors.count()
        hrefs: list[tuple[str, str]] = []
        for index in range(count):
            href = anchors.nth(index).get_attribute("href") or ""
            if href:
                hrefs.append((href, urljoin(page.url, href)))
        links = links_from_hrefs(hrefs)
        span.detail = (f"{len(links)} in scope of {count} details anchors · "
                       f"{', '.join(link.name for link in links) or 'none'}")
    return links


def find_activity(page: Page) -> SectionLink | None:
    """The "Show all" into the activity feed, or None if this profile has none."""
    with trace.phase(trace.WORK, "find activity") as span:
        anchors = page.locator("a[href*='/recent-activity/']")
        count = anchors.count()
        hrefs: list[tuple[str, str]] = []
        for index in range(count):
            href = anchors.nth(index).get_attribute("href") or ""
            if href:
                hrefs.append((href, urljoin(page.url, href)))
        link = activity_from_hrefs(hrefs)
        span.detail = link.url if link else f"none of {count} activity anchors"
    return link


def _same_route(actual: str, expected: str) -> bool:
    """Whether two LinkedIn URLs address the same client-side route."""
    actual_parts = urlsplit(actual)
    expected_parts = urlsplit(expected)
    return (
        actual_parts.hostname == expected_parts.hostname
        and actual_parts.path.rstrip("/") == expected_parts.path.rstrip("/")
    )


def _link_failure_detail(page: Page, target: Any, link: SectionLink,
                         exc: Exception) -> str:
    """Describe a failed saved-link click without retrying the interaction."""
    try:
        matches = target.count()
    except Exception:
        matches = "unknown"
    return (
        f"{type(exc).__name__}: {str(exc).splitlines()[0][:1600]} · "
        f"current={page.url} · expected={link.url} · "
        f"href={link.href!r} · matches={matches}"
    )


def _reveal_featured_link(page: Page, target: Any) -> None:
    from ..capture.pace import sleep_with_reason

    found = _profile_card(page, "Featured")
    if found is None:
        return
    _heading, card = found
    for _ in range(MAX_FEATURED_ADVANCES):
        if not horizontally_clipped(target.bounding_box(), card.bounding_box()):
            return
        control = card.locator(FEATURED_NEXT).first
        if not control.count() or not control.is_visible():
            break
        interaction.click(page, control, timeout=TAB_TIMEOUT_MS)
        sleep_with_reason(0.4, "featured carousel settle")
    if horizontally_clipped(target.bounding_box(), card.bounding_box()):
        raise RuntimeError("Featured detail link remains outside the carousel viewport")


def open_link(page: Page, link: SectionLink, *, attempts: int = 1) -> Any:
    """Click a "Show all" control and wait for the section to load.

    Clicked rather than navigated to, which is the rule `flow.open_result`
    already follows: a click carries a referer and goes through the site's own
    routing, while a bare `goto` arrives with neither and says so. `visit.arrive`
    uses `goto` only because a URL handed to the tool has no page it came from --
    a section link does have one, and it is on screen.

    `attempts` remains one for finite profile sections. Activity opts into one
    retry because its separate application has been observed losing the first
    click while replacing the profile content.
    """
    # The profile can be replaced by LinkedIn's transient error after its
    # section links were discovered. Recover before resolving the saved link,
    # otherwise the locator waits on an anchor that no longer exists and never
    # reaches the retry control.
    recover_transient_error(page)
    with trace.phase(trace.NET, f"open {link.name}") as span:
        response = None
        retried = False
        client_routed = False
        for attempt in range(attempts):
            target = page.locator(f'a[href="{link.href}"]').first
            try:
                if link.name == "featured":
                    _reveal_featured_link(page, target)
                with page.expect_navigation(
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS) as nav:
                    interaction.click(page, target)
                response = nav.value
                break
            except BrowserTimeoutError as exc:
                # LinkedIn sometimes changes only its client-side route, which
                # Playwright does not report as a document navigation. The URL
                # is the durable success signal in that case.
                if _same_route(page.url, link.url):
                    client_routed = True
                    break
                if attempt + 1 >= attempts:
                    span.detail = _link_failure_detail(
                        page, target, link, exc)
                    raise
                # A click can also be lost while the profile application is
                # replacing content. Re-read the anchor after recovery and try
                # once more; activity is the only caller that opts into this.
                retried = True
                recover_transient_error(page)
            except Exception as exc:
                span.detail = _link_failure_detail(page, target, link, exc)
                raise
        recover_transient_error(page)
        detail = f"{link.url} · HTTP {getattr(response, 'status', '?')}"
        if client_routed:
            detail += " · client-side route"
        elif retried:
            detail += " · opened on retry"
        span.detail = detail
    return response


def _refresh_section_control(page: Page, link: SectionLink,
                             config: Any,
                             profile_url: str | None = None) -> SectionLink:
    """Reload the profile once and return its current control for ``link``.

    LinkedIn remounts the profile application after its details Back control.
    The URL can be correct and the screen can look ready while only some of
    the original section anchors have returned.  A saved exact href then has
    zero matches even though the fully loaded profile offered it.  Reloading
    the profile is the same recovery a reader has available; scrolling it once
    through the normal wheel path hydrates its lazy cards before the links are
    discovered again.

    The section name, not the old raw href, is the identity.  LinkedIn is free
    to regenerate query parameters when it remounts the control.
    """
    if not is_profile(page.url):
        back_to_profile(page, profile_url)

    with trace.phase(trace.NET, f"recover {link.name} control") as span:
        page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        recover_transient_error(page)
        if not _wait_for_profile(page, BACK_TIMEOUT_MS):
            raise RuntimeError(
                f"profile did not render after refreshing for {link.name}")

        sweep = scroll_through(page, getattr(config, "tempo", None),
                               confirm_lazy=True)
        if link.name.startswith(f"{ACTIVITY_STEM}-"):
            # Activity does not live under /details/ and therefore is not
            # returned by find_links.  Re-read the profile's current Activity
            # control instead; LinkedIn may choose a different landing tab
            # when it remounts the card, and _capture_activity visits the
            # remaining requested tabs from whichever one it offers.
            current = find_activity(page)
        else:
            current = next(
                (candidate for candidate in find_links(page)
                 if candidate.name == link.name),
                None,
            )
            if current is None and link.name in TABBED_PROFILE_CARDS:
                current = find_tabbed_details_link(
                    page, TABBED_PROFILE_CARDS[link.name], link.name)
        if current is None:
            span.detail = (
                f"not found after refresh · scroll={sweep.pixels}px · "
                f"stopped={sweep.stopped}")
            raise RuntimeError(
                f"{link.name} control was not found after refreshing "
                "and hydrating the profile")
        span.detail = (
            f"reacquired after refresh · scroll={sweep.pixels}px · "
            f"stopped={sweep.stopped}")
        return current


def _open_section(page: Page, link: SectionLink,
                  config: Any) -> tuple[SectionLink, Any]:
    """Open a finite profile section, refreshing the profile once on failure."""
    profile_url = _canonical_profile_url(page.url)
    try:
        return link, open_link(page, link)
    except Exception as exc:
        raise_if_gone(page, exc, during=link.name)
        first_error = exc

    try:
        current = _refresh_section_control(page, link, config, profile_url)
        return current, open_link(page, current)
    except Exception as second:
        raise_if_gone(page, second, during=link.name)
        raise RuntimeError(
            f"{type(first_error).__name__}: {first_error}; "
            "after profile refresh: "
            f"{type(second).__name__}: {second}") from second


def wait_for_content(page: Page, was: int | None = None) -> str | None:
    """Wait for the section itself to render. Returns why it did not, or None.

    `was` is the column size before the click, for a tab switch that replaces
    content already on screen rather than filling an empty column.

    A timeout is not fatal: whatever did render is still captured and the reason
    is recorded, so an under-filled capture says so rather than looking like a
    section that was genuinely empty.
    """
    with trace.phase(trace.NET, "await section content") as span:
        try:
            page.wait_for_function(CONTENT_READY, arg=[was],
                                   timeout=CONTENT_TIMEOUT_MS)
            span.detail = "primary column rendered, no spinner left in it"
            return None
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            span.detail = f"gave up after {CONTENT_TIMEOUT_MS}ms · {reason}"
            return reason


def wait_for_dialog_content(page: Page, heading: str) -> str | None:
    """Wait for a dialog to hold something. Returns why it did not, or None.

    Found by its heading rather than by a selector, because the predicate runs
    as page script and the selector that finds this dialog is Playwright's.

    A timeout is not fatal, for the reason `wait_for_content`'s is not:
    whatever did render is still captured and the reason is recorded, so an
    empty capture says so rather than looking like a member who published
    nothing.
    """
    with trace.phase(trace.NET, "await dialog content") as span:
        try:
            page.wait_for_function(DIALOG_READY, arg=[heading],
                                   timeout=CONTENT_TIMEOUT_MS)
            span.detail = "dialog filled, no loader left in it"
            return None
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            span.detail = f"gave up after {CONTENT_TIMEOUT_MS}ms · {reason}"
            return reason


def wait_for_activity_content(page: Page, was: int | None = None) -> str | None:
    """Wait for an activity result or LinkedIn's structural empty state."""
    with trace.phase(trace.NET, "await activity content") as span:
        try:
            page.wait_for_function(
                ACTIVITY_CONTENT_READY, arg=[was, ACTIVITY_RESULTS],
                timeout=CONTENT_TIMEOUT_MS)
            span.detail = "activity result or empty state rendered"
            return None
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            span.detail = f"gave up after {CONTENT_TIMEOUT_MS}ms · {reason}"
            return reason


def exhaust_list(page: Page, tempo: Any = None) -> int:
    """Scroll the section until it stops producing more of itself.

    Returns the characters the column ended up holding. `capture_page` scrolls
    again afterwards on its own account; by then there is nothing left to load,
    so that pass costs one traversal and keeps the screenshot honest.

    Each pass waits on what the page is doing rather than on a fixed pause --
    outstanding requests, a growing column, a spinner. A fixed pause cannot be
    right for both tempos: the wheel rests ~0.08s between flicks under
    `research` and ~0.83s under `human`, so the same sleep that suffices for one
    is a tenth of what the other needs, which is how a section announcing 14
    courses came to be stored holding 10.

    Bounded by `LIST_TOTAL_BUDGET_S` regardless of what the signals say. A list
    that will not settle returns what it has: a short capture is worth keeping,
    and the announced-count contract reports it rather than letting it pass as
    whole.
    """
    with trace.phase(trace.WORK, "load the whole list") as span:
        watch = RequestsInFlight(page)
        ceiling = time.monotonic() + LIST_TOTAL_BUDGET_S
        measure = lambda: _column_size(page)          # noqa: E731
        size = measure()
        passes = 0
        why = "settled"
        try:
            for _ in range(LIST_PASSES):
                if time.monotonic() >= ceiling:
                    why = "ceiling"
                    break
                scroll_through(page, tempo)
                passes += 1
                grown, why = wait_until_settled(
                    page, watch, measure, scope=PRIMARY_COLUMN_SELECTORS,
                    deadline=ceiling)
                if grown <= size:
                    break
                size = grown
            else:
                why = "passes"
                print(f"  ?? still growing after {LIST_PASSES} passes; "
                      "capturing what loaded")
        finally:
            watch.close()
        span.detail = (f"{passes}/{LIST_PASSES} passes · {size} chars in the "
                       f"column · stopped on {why}")
    return size


def _column_size(page: Page) -> int:
    try:
        return int(page.evaluate(CONTENT_SIZE))
    except Exception:
        return 0


def open_clipped_text(page: Page) -> int:
    """Click every "… more" so nothing is captured mid-sentence.

    Returns how many opened. Self-guarding on the host, so the caller can run it
    before any capture without knowing whose page it is.

    Clicked one at a time rather than by index: opening one reflows the page and
    can reveal another, and a list of locators taken up front goes stale the
    moment the first one is used.
    """
    if not _is_linkedin(urlsplit(page.url).netloc):
        return 0
    started_on = page.url
    with trace.phase(trace.WORK, "open clipped text") as span:
        opened = 0
        index = 0                # only moves past the ones we decline to open
        for _ in range(CLIP_ROUNDS):
            buttons = page.locator(CLIP_BUTTON)
            if index >= buttons.count():
                break
            try:
                if buttons.nth(index).evaluate(IN_A_LINK):
                    index += 1
                    continue
                # Dispatched at the span, not clicked at the button, and both
                # halves of that matter. The button is `pointer-events: none`,
                # so a real click on it is delivered to whatever is underneath;
                # on a profile whose Activity card holds a post, that is the
                # post, and the run navigated to it and captured a feed page
                # where the profile should have been. Dispatching skips hit
                # testing entirely, so it cannot land on the wrong element. The
                # span is the target because that is where the handler is -- an
                # event dispatched at the button bubbles up, away from it.
                # `nth(index)`, not `first`: opening one removes it from the
                # page, so the list shrinks under a fixed index while the ones
                # we skipped stay put ahead of it.
                buttons.nth(index).locator("span").first.dispatch_event("click")
                opened += 1
            except Exception:
                break
            if page.url != started_on:
                # Belt and braces. Nothing here should navigate; if something
                # did, the capture that follows must not be of wherever we
                # landed.
                print("  !! opening clipped text left the page; going back")
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=BACK_TIMEOUT_MS)
                except Exception:
                    pass
                break
        span.detail = f"{opened} expanded" + (" (hit the round cap)"
                                              if opened == CLIP_ROUNDS else "")
    return opened


def _open_about_dialog(page: Page, span: Any) -> str | None:
    """Get the About this member dialog open, and say which way in worked.

    None when the profile offers no way in at all, which is ordinary. Raises
    when a way in was there and did not lead to the dialog, because that is a
    fact about the capture rather than about the profile.

    The badge is tried first because it is one click rather than two. The More
    menu is the path for every member LinkedIn has verified nothing about --
    who has no badge, and whose account history exists all the same.
    """
    trigger = page.locator(ABOUT_TRIGGER)
    if trigger.count():
        interaction.click(page, trigger.first, timeout=MENU_TIMEOUT_MS)
        page.locator(ABOUT_DIALOG).first.wait_for(
            state="visible", timeout=ABOUT_TIMEOUT_MS)
        return "verification badge"

    # LinkedIn keeps a hidden responsive copy carrying aria-label="More"
    # beside the visible, text-labelled action.  A CSS attribute lookup finds
    # the hidden copy first and Patchright waits for it until timeout.  Role
    # lookup follows the accessibility tree, so the hidden duplicate is
    # excluded and the action a reader can actually see is selected.
    more = page.locator("main").get_by_role(
        "button", name="More", exact=True)
    if more.count() == 0:
        span.detail = "no verification badge and no More button on this profile"
        return None
    interaction.click(page, more.first, timeout=MENU_TIMEOUT_MS)
    # LinkedIn renders this list as menu items on some builds and as plain
    # buttons on others, the way the badge is an anchor on some and a div on
    # others. Nothing here reads an href; the item is clicked, not followed.
    for role in ("menuitem", "button", "link"):
        item = page.get_by_role(role, name=ABOUT_MENU_ITEM).first
        if item.count():
            interaction.click(page, item, timeout=MENU_TIMEOUT_MS)
            page.locator(ABOUT_DIALOG).first.wait_for(
                state="visible", timeout=ABOUT_TIMEOUT_MS)
            return "More menu"

    span.detail = f"not in the More menu, which offered {_menu_labels(page)}"
    # A menu left open holds the scroll lock, exactly as a dialog does.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return None


def _menu_labels(page: Page) -> str:
    try:
        offered = page.locator(ABOUT_MENU_ITEMS).all_inner_texts()
    except Exception:
        return "nothing this run could read"
    return ", ".join(" ".join(text.split()) for text in offered) or "nothing"


def capture_about_member(page: Page, config: Any = None) -> SectionCapture | None:
    """The About this member panel, captured on its own. None if not offered.

    The dialog rather than the page it sits over. Opening it dims and freezes
    the profile behind, so capturing the page would store a greyed-out profile
    with a box on top and a second copy of the profile's HTML -- against the
    dialog's own 7KB.

    A profile that does not offer it is ordinary rather than an error: the menu
    differs by who is reading and who is read. One that offers it and then does
    not produce it is recorded, because that is a fact about the capture.
    """
    if not is_profile(page.url):
        return None

    started = time.monotonic()
    capture = PageCapture(
        # The dialog has no address; the profile is where it was read from.
        url_requested=page.url,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    with trace.phase(trace.WORK, f"open {ABOUT_MEMBER}") as span:
        try:
            route = _open_about_dialog(page, span)
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"would not open · {type(exc).__name__}"
            _close_about(page)
            capture.load_ms = int((time.monotonic() - started) * 1000)
            return SectionCapture(ABOUT_MEMBER, capture)
        if route is None:
            return None
        span.detail = f"opened from the {route}"

        panel = page.locator(ABOUT_DIALOG).first
        try:
            capture.final_url = page.url
            capture.title = page.title()
            capture.text = panel.inner_text()
            capture.text_chars = len(capture.text)
            capture.html_bytes = panel.evaluate(OUTER_HTML).encode("utf-8")
            capture.screenshot_bytes = [panel.screenshot(timeout=SHOT_TIMEOUT_MS)]
            capture.ok = True
            span.detail = (f"{capture.text_chars} chars · "
                           f"{len(capture.html_bytes)} bytes of html")
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"opened but could not be read · {type(exc).__name__}"
    capture.load_ms = int((time.monotonic() - started) * 1000)
    if capture.ok and config is not None:
        _dwell(capture, config)
    _close_about(page)
    return SectionCapture(ABOUT_MEMBER, capture)


def capture_contact_info(page: Page, config: Any = None) -> SectionCapture | None:
    """The Contact info dialog, captured on its own. None if not offered.

    The panel rather than the page it sits over, for the reason the About this
    member dialog is captured that way: opening it dims and freezes the profile
    behind, so capturing the page would store a greyed-out profile with a box
    on top and a second copy of the profile's HTML.

    A profile that publishes no contact details offers no link, and that is
    ordinary rather than an error. One that offers the link and then does not
    produce the dialog is recorded, because that is a fact about the capture.
    """
    if not is_profile(page.url):
        return None

    profile_url = page.url
    started = time.monotonic()
    capture = PageCapture(
        # The dialog is served over the profile on some builds and at an
        # address of its own on others. The profile is where it was read from
        # either way.
        url_requested=profile_url,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    with trace.phase(trace.WORK, f"open {CONTACT_INFO}") as span:
        trigger = page.locator(CONTACT_TRIGGER)
        if trigger.count() == 0:
            span.detail = "no contact info link on this profile"
            return None
        panel = page.locator(CONTACT_DIALOG).first
        try:
            interaction.click(page, trigger.first, timeout=MENU_TIMEOUT_MS)
            panel.wait_for(state="visible", timeout=CONTACT_TIMEOUT_MS)
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"would not open · {type(exc).__name__}"
            _close_dialog(page, CONTACT_DIALOG, profile_url)
            capture.load_ms = int((time.monotonic() - started) * 1000)
            return SectionCapture(CONTACT_INFO, capture)
        span.detail = "opened from the top card"

    # Before capturing, not after: the screenshot, the text and the HTML are
    # all taken from whatever is on screen at that moment.
    unrendered = wait_for_dialog_content(page, CONTACT_HEADING)
    if unrendered:
        print(f"  ?? {CONTACT_INFO} had not rendered after "
              f"{CONTENT_TIMEOUT_MS // 1000}s; capturing what is there")

    # Before anything is read off it, so the bytes, the text, the markdown and
    # the picture all agree about what was there.
    dom.strip_dialog_noise(page)

    with trace.phase(trace.WORK, f"read {CONTACT_INFO}") as span:
        try:
            capture.final_url = page.url
            capture.title = page.title()
            capture.text = panel.inner_text()
            capture.text_chars = len(capture.text)
            capture.html_bytes = panel.evaluate(OUTER_HTML).encode("utf-8")
            capture.screenshot_bytes = [panel.screenshot(timeout=SHOT_TIMEOUT_MS)]
            capture.ok = True
            span.detail = (f"{capture.text_chars} chars · "
                           f"{len(capture.html_bytes)} bytes of html")
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"opened but could not be read · {type(exc).__name__}"
    capture.load_ms = int((time.monotonic() - started) * 1000)
    if capture.ok and config is not None:
        _dwell(capture, config)
    _close_dialog(page, CONTACT_DIALOG, profile_url)
    return SectionCapture(CONTACT_INFO, capture, unrendered)


def capture_open_to_work(page: Page,
                         config: Any = None) -> SectionCapture | None:
    """The Open to Work dialog, captured on its own. None if not offered.

    A member who is not looking has no card and no control, which is ordinary
    rather than an error -- most profiles are in that state. One that offers
    the control and then does not produce the dialog is recorded, because that
    is a fact about the capture.
    """
    if not is_profile(page.url):
        return None

    profile_url = page.url
    started = time.monotonic()
    capture = PageCapture(
        url_requested=profile_url,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    with trace.phase(trace.WORK, f"open {OPEN_TO_WORK}") as span:
        card_text = page.locator(OPEN_TO_WORK_CARD_TEXT)
        if card_text.count() == 0:
            span.detail = "this profile is not open to work"
            return None
        trigger = card_text.first.locator(PROFILE_CARD).locator(
            OPEN_TO_WORK_TRIGGER,
        )
        if trigger.count() == 0:
            span.detail = "this profile is not open to work"
            return None
        panel = page.locator(OPEN_TO_WORK_DIALOG).first
        try:
            interaction.click(page, trigger.first, timeout=MENU_TIMEOUT_MS)
            panel.wait_for(state="visible", timeout=CONTACT_TIMEOUT_MS)
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"would not open · {type(exc).__name__}"
            _close_dialog(page, OPEN_TO_WORK_DIALOG, profile_url)
            capture.load_ms = int((time.monotonic() - started) * 1000)
            return SectionCapture(OPEN_TO_WORK, capture)
        span.detail = "opened from the card"

    # Before capturing, not after: the screenshot, the text and the HTML are
    # all taken from whatever is on screen at that moment.
    unrendered = wait_for_dialog_content(page, OPEN_TO_WORK_HEADING)
    if unrendered:
        print(f"  ?? {OPEN_TO_WORK} had not rendered after "
              f"{CONTENT_TIMEOUT_MS // 1000}s; capturing what is there")

    dom.strip_dialog_noise(page)

    with trace.phase(trace.WORK, f"read {OPEN_TO_WORK}") as span:
        try:
            capture.final_url = page.url
            capture.title = page.title()
            capture.text = panel.inner_text()
            capture.text_chars = len(capture.text)
            capture.html_bytes = panel.evaluate(OUTER_HTML).encode("utf-8")
            capture.screenshot_bytes = [panel.screenshot(timeout=SHOT_TIMEOUT_MS)]
            capture.ok = True
            span.detail = (f"{capture.text_chars} chars · "
                           f"{len(capture.html_bytes)} bytes of html")
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"opened but could not be read · {type(exc).__name__}"
    capture.load_ms = int((time.monotonic() - started) * 1000)
    if capture.ok and config is not None:
        _dwell(capture, config)
    _close_dialog(page, OPEN_TO_WORK_DIALOG, profile_url)
    return SectionCapture(OPEN_TO_WORK, capture, unrendered)


def _close_dialog(page: Page, selector: str, profile_url: str) -> None:
    """Leave the profile as the sections after this one expect to find it.

    Two ways this can end somewhere else. A dialog left open holds the scroll
    lock, so every section below would be looked for on a page that cannot
    move -- and on builds serving the control as an address rather than
    intercepting it, the click leaves an overlay URL on screen instead.
    """
    try:
        page.keyboard.press("Escape")
        page.locator(selector).first.wait_for(state="hidden",
                                              timeout=MENU_TIMEOUT_MS)
    except Exception:
        pass
    if page.url != profile_url:
        try:
            page.goto(profile_url, wait_until="domcontentloaded",
                      timeout=ABOUT_TIMEOUT_MS)
        except Exception:
            pass
    # Not just back at the address: back on screen. Every section after this
    # one looks for its control with a single instantaneous count and reads a
    # miss as "this profile does not have that", so returning while the profile
    # is still rendering records an absence that was never true.
    try:
        page.wait_for_function(PROFILE_RENDERED, timeout=MENU_TIMEOUT_MS)
    except Exception:
        print("  ?? the profile had not rendered again after a dialog closed")


def capture_featured(page: Page) -> SectionCapture | None:
    """The Featured card, read off the profile. None if there is none to read.

    Most profiles carry the whole card inline and offer no "Show all", so there
    is no page to navigate to and the card has to be captured where it stands.
    The ones that do offer a page are captured through it instead, and the
    caller decides which of the two applies.

    The card rather than the profile it sits on, for the reason the About panel
    is captured on its own: the surrounding page is already stored, and storing
    it twice with one card highlighted stores neither well.
    """
    if not is_profile(page.url):
        return None

    started = time.monotonic()
    capture = PageCapture(
        # The card has no address of its own on these profiles.
        url_requested=page.url,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    with trace.phase(trace.WORK, f"read {FEATURED}") as span:
        heading = page.locator("main").get_by_role(
            "heading", name=FEATURED_HEADING, exact=True)
        if heading.count() == 0:
            span.detail = "no Featured card on this profile"
            return None
        card = heading.first.locator(FEATURED_CARD)
        try:
            interaction.scroll_to_target(page, card, timeout=MENU_TIMEOUT_MS)
            capture.final_url = page.url
            capture.title = page.title()
            capture.text = card.inner_text()
            capture.text_chars = len(capture.text)
            capture.html_bytes = card.evaluate(OUTER_HTML).encode("utf-8")
            capture.screenshot_bytes = [card.screenshot(timeout=SHOT_TIMEOUT_MS)]
            capture.ok = True
            span.detail = (f"{capture.text_chars} chars · "
                           f"{len(capture.html_bytes)} bytes of html")
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"there but could not be read · {type(exc).__name__}"

    capture.load_ms = int((time.monotonic() - started) * 1000)
    return SectionCapture(FEATURED, capture)


def capture_causes(page: Page) -> SectionCapture | None:
    """The Causes card, read off the profile. None if there is none to read.

    LinkedIn offers no causes page at all -- no "Show all", no `details/`
    route -- so unlike Featured there is no second path for this one. The card
    where it stands is the only capture there can be, which is exactly why it
    needs to be one: read as a field on the profile blob it had no HTML of its
    own to re-read, no screenshot to look at, and no row to open.
    """
    if not is_profile(page.url):
        return None

    heading = page.locator("main").get_by_role(
        "heading", name=CAUSES_HEADING, exact=True)
    if heading.count() == 0:
        return None
    return _capture_inline_card(
        page, heading.first.locator(PROFILE_CARD), CAUSES)


def capture_inline_recommendations(page: Page) -> Iterator[SectionCapture]:
    """Both tabs of the Recommendations card when it has no details page.

    A short Recommendations list has no "Show all" link, so `find_links`
    cannot reach it. Received is visible in the profile capture, but Given is
    still a separate radio tab and otherwise disappears from the run. Each is
    stored as the card alone, under the same names the details-page path uses.
    """
    if not is_profile(page.url):
        return

    heading = page.locator("main").get_by_role(
        "heading", name=RECOMMENDATIONS_HEADING, exact=True)
    if heading.count() == 0:
        return
    card = heading.first.locator(PROFILE_CARD)

    received = card.get_by_role("radio", name="Received", exact=True).first
    received_unrendered = None
    if received.count() and received.get_attribute("aria-checked") != "true":
        opened, received_unrendered = _open_inline_tab(
            page, card, "Received")
        if not opened:
            return
    open_clipped_text(page)
    yield _capture_inline_card(
        page, card, RECOMMENDATIONS, received_unrendered,
        scroll_target=heading.first)

    opened, unrendered = _open_inline_tab(page, card, "Given")
    if not opened:
        return
    open_clipped_text(page)
    yield _capture_inline_card(
        page, card, RECOMMENDATIONS_GIVEN, unrendered,
        scroll_target=heading.first)


def _open_inline_tab(page: Page, card: Any,
                     label: str) -> tuple[bool, str | None]:
    """Select an inline card tab and wait for its content to be replaced."""
    with trace.phase(trace.WORK, f"open the inline {label} tab") as span:
        tab = card.get_by_role("radio", name=label, exact=True).first
        if tab.count() == 0:
            span.detail = "not on this card"
            return False, None
        try:
            before = card.inner_text()
            interaction.click(page, tab, timeout=TAB_TIMEOUT_MS)
            deadline = time.monotonic() + TAB_TIMEOUT_MS / 1000
            while time.monotonic() < deadline:
                if (tab.get_attribute("aria-checked") == "true"
                        and card.inner_text() != before):
                    span.detail = "selected and content replaced"
                    return True, None
                time.sleep(0.1)
        except Exception as exc:
            span.detail = f"would not open · {type(exc).__name__}"
            return False, None

        reason = f"TimeoutError: inline {label} content did not change"
        span.detail = f"gave up after {TAB_TIMEOUT_MS}ms"
        return True, reason


def _radio_views(tabs: Any) -> tuple[str | None, list[str]]:
    """The selected label and unique labels from a live radio strip."""
    selected = None
    labels: list[str] = []
    seen: set[str] = set()
    for index in range(tabs.count()):
        tab = tabs.nth(index)
        try:
            label = " ".join(tab.inner_text().split())
            identity = slugify(label)
        except Exception:
            continue
        if not label or identity in seen:
            continue
        seen.add(identity)
        labels.append(label)
        if tab.get_attribute("aria-checked") == "true":
            selected = label
    return selected, labels


def _card_details_link(page: Page, card: Any,
                       section_name: str) -> SectionLink | None:
    """The real in-scope details link visible in one profile-card state."""
    anchors = card.locator("a[href*='/details/']")
    hrefs: list[tuple[str, str]] = []
    for index in range(anchors.count()):
        href = anchors.nth(index).get_attribute("href") or ""
        if href:
            hrefs.append((href, urljoin(page.url, href)))
    return next(
        (link for link in links_from_hrefs(hrefs)
         if link.name == section_name),
        None,
    )


def _profile_card(page: Page, heading_name: str) -> tuple[Any, Any] | None:
    """A profile card and the heading used to approach it."""
    heading = page.locator("main").get_by_role(
        "heading", name=heading_name, exact=True)
    if heading.count() == 0:
        return None
    heading = heading.first
    return heading, heading.locator(PROFILE_CARD)


def find_tabbed_details_link(page: Page, heading_name: str,
                             section_name: str) -> SectionLink | None:
    """Find a details control that appears only under a profile-card tab.

    LinkedIn can render a short default tab with no Show all while a longer
    sibling tab exposes the section's real details URL.  Labels and URLs are
    discovered rather than enumerated here.  This makes the interaction work
    for any radio-tabbed profile card and for categories LinkedIn adds later.
    """
    found = _profile_card(page, heading_name)
    if found is None:
        return None
    _heading, card = found

    link = _card_details_link(page, card, section_name)
    if link is not None:
        return link

    selected, views = _radio_views(card.locator(CARD_RADIOS))
    active = selected
    for view in views:
        if view == selected:
            continue
        opened, _unrendered = _open_inline_tab(page, card, view)
        if not opened:
            continue
        active = view
        link = _card_details_link(page, card, section_name)
        if link is not None:
            return link
    if selected is not None and active != selected:
        _open_inline_tab(page, card, selected)
    return None


def capture_inline_interests(page: Page) -> Iterator[SectionCapture]:
    """Capture every inline Interests tab when none offers a details page."""
    if not is_profile(page.url):
        return
    found = _profile_card(page, INTERESTS_HEADING)
    if found is None:
        return
    heading, card = found
    selected, views = _radio_views(card.locator(CARD_RADIOS))
    if not views:
        return
    selected = selected or views[0]

    open_clipped_text(page)
    yield _capture_inline_card(
        page, card, INTERESTS, scroll_target=heading)

    for view in views:
        if view == selected:
            continue
        opened, unrendered = _open_inline_tab(page, card, view)
        if not opened:
            continue
        open_clipped_text(page)
        yield _capture_inline_card(
            page, card, f"{INTERESTS}-{slugify(view)}", unrendered,
            scroll_target=heading)


def _capture_inline_card(page: Page, card: Any, name: str,
                         unrendered: str | None = None, *,
                         scroll_target: Any | None = None) -> SectionCapture:
    """Read one state of a profile card without duplicating the whole page."""
    started = time.monotonic()
    capture = PageCapture(
        url_requested=page.url,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    with trace.phase(trace.WORK, f"read inline {name}") as span:
        try:
            target = card if scroll_target is None else scroll_target
            interaction.scroll_to_target(page, target, timeout=MENU_TIMEOUT_MS)
            capture.final_url = page.url
            capture.title = page.title()
            capture.text = card.inner_text()
            capture.text_chars = len(capture.text)
            capture.html_bytes = card.evaluate(OUTER_HTML).encode("utf-8")
            capture.screenshot_bytes = [card.screenshot(timeout=SHOT_TIMEOUT_MS)]
            capture.ok = True
            span.detail = (f"{capture.text_chars} chars · "
                           f"{len(capture.html_bytes)} bytes of html")
        except Exception as exc:
            capture.error = f"{type(exc).__name__}: {exc}"
            span.detail = f"there but could not be read · {type(exc).__name__}"
    capture.load_ms = int((time.monotonic() - started) * 1000)
    return SectionCapture(name, capture, unrendered)


def _close_about(page: Page) -> None:
    """Leave the profile as the sections after this one expect to find it.

    A dialog left open holds the scroll lock, so every section below would be
    looked for on a page that cannot move.
    """
    close = page.locator(ABOUT_CLOSE)
    try:
        if close.count():
            interaction.click(page, close.first, timeout=MENU_TIMEOUT_MS)
            page.locator(ABOUT_DIALOG).first.wait_for(
                state="hidden", timeout=MENU_TIMEOUT_MS)
            return
    except Exception:
        # Escape remains the recovery route if LinkedIn changes or loses the
        # close control while the dialog is being read.
        pass
    try:
        page.keyboard.press("Escape")
        page.locator(ABOUT_DIALOG).first.wait_for(state="hidden",
                                                  timeout=MENU_TIMEOUT_MS)
    except Exception:
        pass


def open_tab(page: Page, label: str) -> bool:
    """Switch a section to one of its tabs. False if it has no such tab.

    Absence is ordinary rather than exceptional: a person who has written no
    recommendations has no Given tab to click.
    """
    with trace.phase(trace.WORK, f"open the {label} tab") as span:
        try:
            tab = page.get_by_role("radio", name=label, exact=True).first
            if tab.count() == 0:
                span.detail = "not on this page"
                return False
            interaction.click(page, tab, timeout=5000)
            span.detail = "clicked"
            return True
        except Exception as exc:
            span.detail = f"would not open · {type(exc).__name__}"
            return False


def select_primary_detail_tab(
    page: Page, section_name: str,
) -> tuple[bool, str | None]:
    """Select the parser's primary tab when discovery entered through another.

    Returns whether a switch was made and, when it was, the same readiness
    warning a normal tab capture carries. Sections whose captures identify
    their own active category need no configured primary tab.
    """
    label = PRIMARY_DETAIL_TABS.get(section_name)
    if label is None:
        return False, None
    tab = page.get_by_role("radio", name=label, exact=True).first
    if tab.count() == 0 or tab.get_attribute("aria-checked") == "true":
        return False, None
    before = page.evaluate(CONTENT_SIZE)
    interaction.click(page, tab, timeout=TAB_TIMEOUT_MS)
    return True, wait_for_content(page, was=before)


def interest_views(page: Page) -> tuple[str | None, list[str]]:
    """The selected Interests category and all categories this profile offers.

    Labels are the only stable identity LinkedIn exposes. Switching a category
    changes neither the URL nor history, and the backing fetch addresses are
    opaque build-time paths. Empty labels and duplicate radios are ignored.
    """
    return _radio_views(page.locator(INTEREST_TABS))


def wait_for_feed(page: Page, timeout_ms: int = ACTIVITY_READY_MS) -> str | None:
    """Wait for the feed's application to draw. Returns why it did not, or None.

    Not fatal, for the reason `wait_for_content` is not: whatever is there is
    still captured, and a capture that says it was taken early is worth more
    than one that reads as a person who has never posted.
    """
    with trace.phase(trace.NET, "await feed") as span:
        try:
            page.locator(ACTIVITY_STRIP).get_by_role(
                "button", name=ACTIVITY_LANDING, exact=True).first.wait_for(
                    state="visible", timeout=timeout_ms)
            span.detail = "tab strip drawn"
            return None
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            span.detail = f"gave up after {timeout_ms}ms · {reason}"
            return reason


def fetch_feed(page: Page) -> tuple[Any, str | None]:
    """Ask for the address the click only pushed. Returns the response and why
    the feed still did not draw, or None if it did."""
    with trace.phase(trace.NET, "fetch feed") as span:
        try:
            response = page.reload(wait_until="domcontentloaded",
                                   timeout=NAV_TIMEOUT_MS)
            span.detail = f"HTTP {getattr(response, 'status', '?')}"
        except Exception as exc:
            span.detail = f"would not load · {type(exc).__name__}"
            return None, f"{type(exc).__name__}: {exc}"
    return response, wait_for_feed(page)


def open_activity_tab(page: Page, label: str) -> bool:
    """Switch the activity feed to a direct tab or an item under More."""
    with trace.phase(trace.WORK, f"open the {label} tab") as span:
        try:
            strip = page.locator(ACTIVITY_STRIP)
            tab = strip.get_by_role(
                "button", name=label, exact=True).first
            if tab.count():
                interaction.click(page, tab, timeout=TAB_TIMEOUT_MS)
                span.detail = "clicked"
                return True
            more = strip.get_by_role("button", name="More", exact=True).first
            if more.count() == 0:
                span.detail = "not on this page"
                return False
            interaction.click(page, more, timeout=TAB_TIMEOUT_MS)
            for role in ("menuitem", "button"):
                item = page.get_by_role(role, name=label, exact=True).first
                if item.count():
                    interaction.click(page, item, timeout=TAB_TIMEOUT_MS)
                    span.detail = "clicked from More"
                    return True
            page.keyboard.press("Escape")
            span.detail = "not in More"
            return False
        except Exception as exc:
            span.detail = f"would not open · {type(exc).__name__}"
            return False


def _wait_for_profile(page: Page, timeout_ms: int) -> bool:
    try:
        page.wait_for_function(PROFILE_SCREEN_READY, timeout=timeout_ms)
    except Exception:
        return False
    return is_profile(page.url)


def _canonical_profile_url(url: str) -> str | None:
    handle = profile_handle(url)
    if handle is None:
        return None
    parts = urlsplit(url)
    return parts._replace(
        path=f"/in/{handle}/", query="", fragment="").geturl()


def _click_details_back(page: Page) -> tuple[bool, bool]:
    if parse_details(page.url) is None:
        return False, False
    try:
        control = page.get_by_role(
            "button", name=DETAILS_BACK_LABEL, exact=True).first
        if not control.count():
            return False, False
        interaction.click(page, control, timeout=DETAILS_BACK_TIMEOUT_MS)
    except Exception:
        return True, _wait_for_profile(page, BACK_TIMEOUT_MS)
    return True, _wait_for_profile(page, BACK_TIMEOUT_MS)


def back_to_profile(page: Page, expected_profile_url: str | None = None) -> None:
    """Return through LinkedIn's UI, recovering if browser history is unsafe."""
    if is_profile(page.url):
        return
    # A details URL carries its profile handle. A service page does not, so
    # its caller supplies the profile it was opened from. Canonicalising the
    # supplied value both validates it and removes any query/fragment before a
    # recovery navigation.
    profile_url = (
        _canonical_profile_url(expected_profile_url)
        if expected_profile_url is not None
        else _canonical_profile_url(page.url)
    )
    with trace.phase(trace.NET, "back to profile") as span:
        attempted, returned = _click_details_back(page)
        if returned:
            span.detail = "clicked LinkedIn back control"
        elif not attempted:
            try:
                page.go_back(wait_until="domcontentloaded", timeout=BACK_TIMEOUT_MS)
            except Exception:
                pass
            if is_profile(page.url):
                span.detail = f"history fallback, {BACK_TIMEOUT_MS}ms budget"
        if not is_profile(page.url) and profile_url is not None:
            landed = page.url
            page.goto(profile_url, wait_until="domcontentloaded",
                      timeout=BACK_TIMEOUT_MS)
            if not _wait_for_profile(page, BACK_TIMEOUT_MS):
                raise RuntimeError(
                    f"profile recovery landed on {page.url}, not {profile_url}")
            source = "LinkedIn back control" if attempted else "history"
            span.detail = f"profile recovery after {source} left {landed}"
    if not is_profile(page.url):
        raise RuntimeError(f"went back and landed on {page.url}, not a profile")


@dataclass(frozen=True)
class ExpandConfig:
    """What section expansion actually reads, which is two things.

    The old RunConfig carried budget rules, a run label and output paths, and
    `expand` touches none of them -- `run.py` consulted those around the call.

    `tempo` is optional because the ported code already treats it that way:
    both sites that use it check for None first, so expansion without pacing
    works and simply omits the human-like gaps between sections.
    """

    scratch_dir: Path
    # A pace.Tempo, or None. Typed loosely to keep this dataclass from
    # importing the browser stack -- section expansion is imported by tests
    # that never launch Chrome.
    tempo: Any = None
    # None preserves the historical behavior of reading every available
    # section. An empty set deliberately captures only the base profile.
    requested_sections: frozenset[str] | None = None


def _wants(config: Any, name: str) -> bool:
    selected = getattr(config, "requested_sections", None)
    return selected is None or name in selected


def will_expand(page: Page, config: Any) -> bool:
    """Whether there is anything here to expand, before any budget is consulted.

    Asked by `expand` and by its caller both -- the caller because deciding
    there is nothing to do must come before spending a database round trip on
    the section budget, and `expand` because it is callable on its own and a
    guard that lives only in the caller is a guard one caller has.
    """
    return bool(getattr(config, "expand_sections", True)) and is_profile(page.url)


def raise_if_gone(page: Page, exc: BaseException | None = None,
                  *, during: str) -> None:
    """Turn a fatal browser/session loss into a failed run, not a skipped page."""
    raise_if_browser_gone(page, exc, during=during)
    try:
        from .session import _read_identity

        observation = _read_identity(page)
    except Exception:
        # A section timeout is still allowed to cost only its section when the
        # page cannot provide independent evidence that the whole session died.
        return
    if observation.state == "signed_out":
        from ..capture.failures import CaptureFailed

        detail = observation.detail or "LinkedIn session is signed out"
        raise CaptureFailed(
            "login_required", f"{detail} during {during}") from exc
    if observation.state == "challenge":
        from ..capture.failures import CaptureFailed

        detail = observation.detail or "LinkedIn challenge"
        raise CaptureFailed("blocked", f"{detail} during {during}") from exc


def wait_for_profile_loading(page: Page) -> str | None:
    """Resolve mounted loading cards before treating missing links as final."""
    if not is_profile(page.url):
        return None
    for _ in range(PROFILE_LOADING_PASSES):
        pending = page.locator(PROFILE_LOADING_SELECTOR)
        if pending.count() == 0:
            return None
        with trace.phase(trace.WAIT, "wait for profile sections") as span:
            try:
                interaction.scroll_to_target(page, pending.first, timeout=MENU_TIMEOUT_MS)
                page.wait_for_function(
                    "selector => document.querySelector(selector) === null",
                    arg=PROFILE_LOADING_SELECTOR, timeout=PROFILE_LOADING_WAIT_MS,
                )
            except Exception as exc:
                raise_if_gone(page, exc, during="waiting for profile sections")
                span.detail = "profile sections still loading; rechecking before discovery"
    if page.locator(PROFILE_LOADING_SELECTOR).count():
        return "Profile sections remained loading after bounded readiness checks"
    return None


def capture_inline_profile_section(page: Page, name: str, label: str) -> SectionCapture | None:
    heading = page.locator("main").get_by_role("heading", name=label, exact=True)
    if not heading.count():
        return None
    return _capture_inline_card(page, heading.first.locator(PROFILE_CARD), name)


def expand(page: Page, config: Any, slug: str) -> Iterator[SectionCapture]:
    """Open every in-scope section of the profile on `page` and capture each.

    Yields the captures one at a time; the caller stores each as it arrives, as
    it already does for every other capture. That keeps this testable without a
    database -- and it matters more than it reads: a `PageCapture` carries the
    screenshot bands and the HTML as bytes, so returning a whole profile's
    worth in a list held six fully-scrolled LinkedIn lists in memory at once
    and stored none of them if the run died partway through.

    `slug` names the parent page and is used only for the scratch files
    Playwright screenshots through -- the payloads come back as bytes and the
    scratch directory is emptied when the run ends.

    Whether we are allowed to expand at all is decided once per profile, by the
    caller, before this is called. Deciding it per section would truncate a
    profile partway through at a moment that depends on where in the run the
    limit was reached, which is exactly what a separate section limit exists to
    avoid.

    Nothing here raises. A section that will not open is recorded as a failed
    capture, and a profile we cannot get back to costs its remaining sections
    rather than the run.
    """
    from ..capture.pace import sleep_with_reason  # local: pace imports nothing from here

    if not will_expand(page, config):
        return

    # The account can be logged out after the main profile capture completes
    # but before its folded sections begin.  Check at this boundary rather than
    # waiting for the first missing Show-all control to time out.
    raise_if_gone(page, during="starting profile sections")

    try:
        recover_transient_error(page)
    except Exception as exc:
        raise_if_gone(page, exc, during="opening the profile")
        print(f"  !! profile error would not recover "
              f"({type(exc).__name__}: {exc})")
        return

    scratch = Path(config.scratch_dir)
    profile_url = _canonical_profile_url(page.url)
    inline_sections: set[str] = set()

    # Read before anything is opened, and not only to iterate: whether the
    # profile offers Featured as a page decides how Featured is captured.
    try:
        loading_error = wait_for_profile_loading(page)
        links = find_links(page)
    except Exception as exc:
        raise_if_gone(page, exc, during="reading the section list")
        print(f"  !! could not read sections ({type(exc).__name__}: {exc})")
        links = []
        loading_error = "Profile section discovery could not verify loading completion"

    # Remember Activity before opening anything.  It is intentionally captured
    # last because its feed has no natural end, but LinkedIn can remount an
    # incomplete profile after returning from a details page.  Rediscovering it
    # only at the end then turns an observed section into a silent absence.
    # Keeping the control lets the same bounded refresh/hydration recovery used
    # by finite sections reacquire it without inventing a direct feed URL.
    activity_link = None
    if any(_wants(config, name) for name in (
        ACTIVITY, "activity-comments", "activity-articles",
        "activity-reactions",
    )):
        try:
            activity_link = find_activity(page)
        except Exception as exc:
            raise_if_gone(page, exc, during="reading the activity control")
            print(f"  !! could not remember activity "
                  f"({type(exc).__name__}: {exc})")

    # These first, while the profile is still the page on screen. Everything
    # below navigates away and comes back, and none is findable from anywhere
    # else once it has.
    if _wants(config, "about-this-member"):
        about = capture_about_member(page, config)
        if about is not None:
            print(f"    + {about.section}")
            yield about

    if _wants(config, CONTACT_INFO):
        contact = capture_contact_info(page, config)
        if contact is not None:
            print(f"    + {contact.section}")
            yield contact

    if _wants(config, OPEN_TO_WORK):
        looking = capture_open_to_work(page, config)
        if looking is not None:
            print(f"    + {looking.section}")
            yield looking

    # No second path for this one, so no link to check for: LinkedIn has no
    # causes page to offer. The card where it stands is the only capture there
    # can be.
    if _wants(config, CAUSES):
        card = capture_causes(page)
        if card is not None:
            print(f"    + {card.section}")
            yield card
            _dwell(card.capture, config)

    if (_wants(config, FEATURED)
            and not any(link.name == FEATURED for link in links)):
        card = capture_featured(page)
        if card is not None:
            inline_sections.add(FEATURED)
            print(f"    + {card.section}")
            yield card
            _dwell(card.capture, config)

    # A profile card can reveal its Show all only after a non-default radio is
    # selected. Discover those live controls before falling back to the short
    # inline card. The details page, when offered, owns the capture because it
    # can then visit every remaining tab without truncating a long category.
    for section_name, heading_name in TABBED_PROFILE_CARDS.items():
        if (not _wants(config, section_name)
                or any(link.name == section_name for link in links)):
            continue
        link = find_tabbed_details_link(page, heading_name, section_name)
        if link is not None:
            links.append(link)
    links.sort(key=lambda link: SECTIONS.index(link.name))

    if (_wants(config, RECOMMENDATIONS)
            and not any(link.name == RECOMMENDATIONS for link in links)):
        for card in capture_inline_recommendations(page):
            inline_sections.add(RECOMMENDATIONS)
            print(f"    + {card.section}")
            yield card
            _dwell(card.capture, config)

    if (_wants(config, INTERESTS)
            and not any(link.name == INTERESTS for link in links)):
        for card in capture_inline_interests(page):
            inline_sections.add(INTERESTS)
            print(f"    + {card.section}")
            yield card
            _dwell(card.capture, config)

    for name, label in (("experience", "Experience"), ("education", "Education"),
                        ("skills", "Skills")):
        if not _wants(config, name) or any(link.name == name for link in links):
            continue
        card = capture_inline_profile_section(page, name, label)
        if card is not None:
            yield card
            inline_sections.add(name)
            _dwell(card.capture, config)
    if loading_error:
        for name in SECTIONS:
            if (_wants(config, name) and name not in inline_sections
                    and not any(link.name == name for link in links)):
                yield SectionCapture(name, PageCapture(
                    url_requested=profile_url, final_url=page.url,
                    error=loading_error,
                ), loading_error)

    links = [link for link in links if _wants(config, link.name)]
    for index, link in enumerate(links):
        # A person can log this profile out while the previous section is being
        # read.  Returning to LinkedIn's public profile is already enough
        # evidence to stop; do not spend one locator timeout discovering it.
        raise_if_gone(page, during=f"starting {link.name}")
        print(f"    + {link.name}")
        try:
            link, response = _open_section(page, link, config)
        except Exception as exc:
            raise_if_gone(page, exc, during=link.name)
            yield SectionCapture(link.name, PageCapture(
                url_requested=link.url, error=f"{type(exc).__name__}: {exc}"))
            print(f"  !! {link.name} would not open ({type(exc).__name__}: {exc})")
            # A click that navigated and then timed out leaves us on the section
            # anyway. Without this the next link is looked for on the wrong page
            # and every remaining section fails for a reason that is not theirs.
            if not is_profile(page.url):
                try:
                    if link.name == "services":
                        back_to_profile(page, profile_url)
                    else:
                        back_to_profile(page)
                except Exception as back:
                    # Giving up on the rest of the profile is the right answer
                    # when we simply cannot get back to it, and the wrong one
                    # when there is no browser left to get back with.
                    raise_if_gone(page, back, during=f"returning from {link.name}")
                    break
            continue

        # Before capturing, not after: the screenshot, the text and the HTML are
        # all taken from whatever is on screen at that moment.
        switched, unrendered = select_primary_detail_tab(page, link.name)
        if not switched:
            unrendered = wait_for_content(page)
        if unrendered:
            print(f"  ?? {link.name} had not rendered after "
                  f"{CONTENT_TIMEOUT_MS // 1000}s; capturing what is there")
        # Load the whole list before anything is read off it. A details page
        # renders a first batch and appends the rest as the reader reaches the
        # bottom, and `capture_page`'s own scroll confirms lazy growth once --
        # enough for a page that has finished, not for one still fetching. This
        # is the step that was missing: `exhaust_list` existed for exactly this
        # and nothing called it, so a section announcing 22 publications was
        # stored holding 10 and read as complete.
        #
        # Interests keeps its own distance budget: some of its categories are
        # effectively endless, so exhausting them is not a goal.
        if link.name != INTERESTS:
            exhaust_list(page, config.tempo)

        # After the list is whole, so entries that arrived in a later batch are
        # opened too.
        open_clipped_text(page)

        scroll_options = ({"scroll_budget": FEED_SCROLL_PX}
                          if link.name == INTERESTS else {})
        capture = capture_page(page, scratch, f"{slug}--{link.name}",
                               url_requested=link.url, response=response,
                               dismiss=lambda p: overlays.dismiss(p, grace_s=0),
                               tempo=config.tempo,
                               confirm_lazy=(
                                   not unrendered and link.name != INTERESTS),
                               strip=dom.strip_irrelevant,
                               **scroll_options)
        yield SectionCapture(link.name, capture, unrendered)

        _dwell(capture, config)
        if link.name == INTERESTS:
            yield from _capture_interest_tabs(page, scratch, slug, link, config)
        else:
            yield from _capture_tabs(page, scratch, slug, link, config)

        try:
            if link.name == "services":
                back_to_profile(page, profile_url)
            else:
                back_to_profile(page)
        except Exception as exc:
            # Without the profile there is nothing left to click. The sections
            # already captured stand; the rest are simply absent, which the
            # report shows by leaving their "Show all" links outbound.
            print(f"  !! could not return to the profile ({type(exc).__name__}: {exc}); "
                  f"abandoning its remaining sections")
            break

        if index < len(links) - 1 and config.tempo is not None:
            sleep_with_reason(
                config.tempo.result_gap(), "before next section", name="section gap",
                detail=f"tempo '{config.tempo.name}' between_results="
                       f"{config.tempo.between_results} · section {index + 1}/{len(links)}")

    # Last, because it is the only one that cannot be finished. A feed keeps
    # producing, so this leaves on a budget rather than on having reached the
    # end, and anything with an end deserves to be captured whole first.
    if any(_wants(config, name) for name in (
        ACTIVITY, "activity-comments", "activity-articles",
        "activity-reactions",
    )):
        yield from _capture_activity(
            page, scratch, slug, config, activity_link)


def _capture_activity(page: Page, scratch: Path, slug: str,
                      config: Any,
                      initial_link: SectionLink | None = None,
                      ) -> Iterator[SectionCapture]:
    """The activity feed and the tabs beside it: what this person wrote.

    Bounded rather than exhausted. `exhaust_list` scrolls a section until it
    stops growing, which is right for a list of jobs and wrong for a feed that
    has no last item; this takes `FEED_SCROLL_PX` instead, which is what a
    company's Posts tab already takes and for the same reason.

    Nothing here raises. A profile we cannot reach the feed from keeps the
    sections already captured.
    """
    if not is_profile(page.url):
        return
    discovery_error = None
    try:
        link = find_activity(page)
    except Exception as exc:
        raise_if_gone(page, exc, during="reading activity")
        discovery_error = exc
        link = None

    if link is None and initial_link is not None:
        try:
            link = _refresh_section_control(
                page, initial_link, config, _canonical_profile_url(page.url))
        except Exception as exc:
            raise_if_gone(page, exc, during="recovering activity")
            reasons = []
            if discovery_error is not None:
                reasons.append(
                    f"{type(discovery_error).__name__}: {discovery_error}")
            reasons.append(f"after profile refresh: {type(exc).__name__}: {exc}")
            error = "; ".join(reasons)
            yield SectionCapture(initial_link.name, PageCapture(
                url_requested=initial_link.url, error=error))
            print(f"  !! {initial_link.name} control disappeared "
                  f"({error})")
            return
    elif link is None:
        if discovery_error is not None:
            print(f"  !! could not read activity "
                  f"({type(discovery_error).__name__}: {discovery_error})")
        return

    print(f"    + {link.name}")
    try:
        # `open_link` resolves the saved href and its shared click path wheels
        # in either direction through the target's real scroll ancestor.
        response = open_link(page, link, attempts=2)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        yield SectionCapture(link.name, PageCapture(
            url_requested=link.url, error=error))
        print(f"  !! {link.name} would not open ({type(exc).__name__}: {exc})")
        _back_from_activity(page)
        return

    # The click sets the address and LinkedIn's own routing runs, but on these
    # profiles that is all it does. A short grace first, so a profile whose
    # application does render the route is not fetched twice.
    stalled = wait_for_feed(page, ACTIVITY_SETTLE_MS)
    if stalled is not None:
        response, stalled = fetch_feed(page)

    if _wants(config, link.name):
        feed = _capture_feed(page, scratch, slug, link.name, link.url,
                             response=response, stalled=stalled,
                             tempo=config.tempo)
        yield feed
        _dwell(feed.capture, config)

    # Every view except the one we came in through. Landing on `all` makes this
    # the old three-tab loop; landing anywhere else puts Posts back into it,
    # which is the view that would otherwise be lost with the entry point.
    landed = parse_activity(link.url)
    entered = landed[1] if landed is not None else ACTIVITY_ALL
    for view_key, view in ACTIVITY_TABS.items():
        if view_key == entered:
            continue
        name = activity_view_name(view_key)
        if not _wants(config, name):
            continue
        before = page.evaluate(CONTENT_SIZE)
        if not open_activity_tab(page, view):
            continue
        print(f"    + {name}")
        # `page.url` rather than a URL built here: opening a tab pushes its own
        # address, so the page already knows where it is.
        tab = _capture_feed(page, scratch, slug, name, page.url, was=before,
                            tempo=config.tempo)
        yield tab
        _dwell(tab.capture, config)

    _back_from_activity(page)


def _retry_feed(page: Page, name: str, *, was: int | None,
                reason: str) -> str | None:
    """Click LinkedIn's retry control for a failed feed, then wait again.

    Returns None once the feed rendered, or why it still has not. `reason`
    comes back unchanged when there was no error state to retry, so a feed
    that simply timed out keeps the description of what actually happened.

    Nothing raises: a view that will not load costs its own capture, never the
    profile's remaining sections.
    """
    try:
        recovered = recover_transient_error(page)
    except Exception as exc:
        print(f"    !! {name} error state would not recover "
              f"({type(exc).__name__}: {exc})")
        return reason
    if not recovered:
        return reason
    print(f"    ~ {name} showed LinkedIn's error state; retried")
    # `was` again, not the error page's own length: the question is still
    # whether the column changed from what the previous view left behind.
    return wait_for_activity_content(page, was=was)


def _capture_feed(page: Page, scratch: Path, slug: str, name: str, url: str,
                  *, response: Any = None, was: int | None = None,
                  stalled: str | None = None,
                  tempo: Any = None) -> SectionCapture:
    """One view of the feed, captured on the feed budget.

    `stalled` is a reason the feed never drew, which stands instead of waiting
    on the column as well: the second wait would only spend its own timeout
    finding out what the first one already established.
    """
    unrendered = stalled or wait_for_activity_content(page, was=was)
    if unrendered:
        # A feed that failed to load is not the same as one that is empty, and
        # LinkedIn says so: it replaces the column with an error state offering
        # a Refresh. Clicking it costs one attempt and turns a 260-character
        # error page back into the feed. Never fatal -- if it will not recover
        # we store what is on screen, exactly as before.
        unrendered = _retry_feed(page, name, was=was, reason=unrendered)
    if unrendered:
        # Without the timeout in it: two different waits reach this, and naming
        # one of their budgets would be wrong half the time.
        print(f"  ?? {name} had not rendered; capturing what is there")
    open_clipped_text(page)
    capture = capture_page(page, scratch, f"{slug}--{name}",
                           url_requested=url, response=response,
                           dismiss=lambda p: overlays.dismiss(p, grace_s=0),
                           tempo=tempo,
                           scroll_budget=FEED_SCROLL_PX,
                           read_selector=ACTIVITY_RESULTS,
                           strip=dom.strip_irrelevant)
    return SectionCapture(name, capture, unrendered)


def _back_from_activity(page: Page) -> None:
    """Return to the profile, however many tabs deep the feed ended up.

    One press per tab opened plus the feed itself, because a tab switch pushes
    history rather than navigating and so has to be unwound the same way.
    """
    for _ in range(len(ACTIVITY_TABS)):
        if is_profile(page.url):
            return
        try:
            page.go_back(wait_until="domcontentloaded", timeout=BACK_TIMEOUT_MS)
        except Exception:
            break
    if not is_profile(page.url):
        print(f"  !! left the activity feed at {page.url}, not the profile")


def _capture_tabs(page: Page, scratch: Path, slug: str,
                  link: SectionLink, config: Any) -> Iterator[SectionCapture]:
    """Capture the section's other tabs, for the one section that has them.

    Each tab is its own section page: another list fetched, another set of
    artifacts stored, and its own `section` event in the journal.

    A tab that is not there, or will not switch, is not an error -- it is a
    person with nothing under it -- so nothing is recorded for it. Recording a
    failed capture would put "recommendations-given (unavailable)" in the report
    of everyone who has never written one.
    """
    for view in TAB_VIEWS.get(link.name, ()):
        before = page.evaluate(CONTENT_SIZE)
        if not open_tab(page, view):
            continue

        name = f"{link.name}-{view.lower()}"
        print(f"    + {name}")

        unrendered = wait_for_content(page, was=before)
        if unrendered:
            print(f"  ?? {name} had not rendered after "
                  f"{CONTENT_TIMEOUT_MS // 1000}s; capturing what is there")
        open_clipped_text(page)

        # `page.url` rather than `link.url`: the tab is the same address, and
        # saying so is more honest than implying a second one exists.
        capture = capture_page(page, scratch, f"{slug}--{name}",
                               url_requested=page.url,
                               dismiss=lambda p: overlays.dismiss(p, grace_s=0),
                               tempo=config.tempo,
                               confirm_lazy=not unrendered,
                               strip=dom.strip_irrelevant)
        yield SectionCapture(name, capture, unrendered)
        _dwell(capture, config)


def _capture_interest_tabs(page: Page, scratch: Path, slug: str,
                           link: SectionLink,
                           config: Any) -> Iterator[SectionCapture]:
    """Capture every additional Interests category this profile exposes.

    The selected category was captured by the caller as `interests`. Category
    switches replace content in place, so each remaining label becomes the
    identity of its own section capture while retaining the page's real URL.
    """
    selected, views = interest_views(page)
    for view in views:
        if view == selected:
            continue
        before = page.evaluate(CONTENT_SIZE)
        if not open_tab(page, view):
            continue

        name = f"{INTERESTS}-{slugify(view)}"
        print(f"    + {name}")
        unrendered = wait_for_content(page, was=before)
        if unrendered:
            print(f"  ?? {name} had not rendered after "
                  f"{CONTENT_TIMEOUT_MS // 1000}s; capturing what is there")
        open_clipped_text(page)

        capture = capture_page(page, scratch, f"{slug}--{name}",
                               url_requested=link.url,
                               dismiss=lambda p: overlays.dismiss(p, grace_s=0),
                               tempo=config.tempo,
                               scroll_budget=FEED_SCROLL_PX,
                               strip=dom.strip_irrelevant)
        yield SectionCapture(name, capture, unrendered)
        _dwell(capture, config)


def _dwell(capture: PageCapture, config: Any) -> None:
    """Read a section at the same rate the run reads anything else."""
    from ..capture.pace import CHARS_PER_SECOND, DWELL_BOUNDS, sleep_with_reason

    if config.tempo is None:
        return
    tempo = config.tempo
    sleep_with_reason(
        tempo.dwell(capture.text_chars), "reading", name="dwell",
        detail=(f"{capture.text_chars} chars @ {CHARS_PER_SECOND}/s "
                f"x scale {tempo.dwell_scale} (tempo '{tempo.name}') "
                f"x jitter 0.7-1.4, clamped to "
                f"{DWELL_BOUNDS[0] * tempo.dwell_scale:.1f}-{DWELL_BOUNDS[1]:.0f}s"))
