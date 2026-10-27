"""Company pages, which are several documents wearing one address.

A company's URL gives you its landing tab and nothing else. About, Posts, Jobs
and People live behind their own addresses, and the posts feed holds most of
what the page is worth -- on one company the address alone reached 13 of 80
posts, the rest arriving only as the feed was scrolled.

Tabs are read from the page's own navigation rather than assembled from a list
of names. Which tabs an organisation has varies, and a name invented for one it
does not have is a request that 404s.

They are opened by clicking the link, as everything else here is. LinkedIn
renders the switch client-side, so no navigation event fires and arrival has to
be established by watching the address and then the content.
"""

from __future__ import annotations

import html
import json
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote, urlsplit

from lxml import etree
from lxml import html as lxml_html

from ..capture import interaction, media, trace
from .routes import is_company_home

if TYPE_CHECKING:      # the UI reads the tab rules from here and needs no browser
    from patchright.sync_api import Page

# The path prefixes that name an organisation. Schools and showcase pages carry
# the same navigation and the same tabs.
ORG_PREFIXES = ("company", "school", "showcase")

# Path segments that sit where a slug does without being one.
RESERVED = frozenset({"setup", "search", "admin", "new"})

# The navigation, most specific first. Anchors are filtered by href whichever
# container matched, so a class name that changes costs the shortcut and not the
# tabs -- the last entry finds them wherever they live.
NAV_SELECTORS = (
    "a[data-view-name^='pages-member-nav-'][href]",
    ".org-page-navigation__items a[href]",
    "nav.org-page-navigation a[href]",
    "nav[aria-label*='organization' i] a[href]",
)

# Long enough for a tab to be fetched and drawn on a slow connection. An empty
# capture is worse than a slow one.
ARRIVAL_TIMEOUT_S = 15.0
SETTLE_RANGE = (0.5, 1.1)

# Where the page believes it is. Asked of the page rather than read from
# `page.url`: that is a cached value, refreshed when the browser's navigation
# event is processed, and a loop that polls it without making any other call
# never gives it the chance -- it keeps reporting the previous tab while the new
# one is already on screen.
LOCATION = "() => location.pathname"

ALL = "all"
NONE = "none"


@dataclass(frozen=True)
class Company:
    kind: str
    slug: str
    tab: str
    """The tab the URL named, empty when it named the page itself."""

    @property
    def path(self) -> str:
        return f"/{self.kind}/{self.slug}/"

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com{self.path}"


@dataclass(frozen=True)
class Tab:
    name: str
    path: str

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com{self.path}"


def company_url(url: str) -> Company | None:
    """The organisation page this URL names, or None if it names something else.

    Country subdomains (uk.linkedin.com) are ordinary in Google's results and
    address the same page. Everything else on the domain -- /in/, /posts/,
    /jobs/view/, /pulse/ -- is a single item rather than a profile, and those
    are most of the LinkedIn URLs a search returns.
    """
    parts = urlsplit(url)
    host = parts.netloc.lower().split(":")[0]
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 2 or segments[0].lower() not in ORG_PREFIXES:
        return None
    slug = segments[1]
    if slug.lower() in RESERVED:
        return None
    return Company(segments[0].lower(), slug,
                   segments[2].lower() if len(segments) > 2 else "")


def company_on_page(page: Page) -> Company | None:
    """Read the live route, falling back when the page cannot be evaluated.

    LinkedIn changes company routes client-side. Immediately after browser
    history returns from a child page, Patchright's cached `page.url` can still
    name that child even though the page is visibly back on Home.
    """
    try:
        path = page.evaluate(LOCATION) or ""
        return company_url(f"https://www.linkedin.com{path}")
    except Exception:
        return company_url(page.url)


def wanted_tabs(value: str) -> frozenset[str] | None:
    """Parse the --linkedin-tabs argument.

    None turns the sweep off; an empty set means every tab the page offers,
    which cannot be spelled as a list because we do not know them in advance.
    """
    cleaned = (value or "").strip().lower()
    if cleaned in ("", NONE):
        return None
    if cleaned == ALL:
        return frozenset()
    return frozenset(part.strip() for part in cleaned.split(",") if part.strip())


def _tab_path(href: str, root: str) -> str | None:
    """The tab `href` names, or None if it is not one of this page's tabs.

    One segment past the page's own path and no more: that admits /about/ and
    rejects both the page itself and the deeper links a tab's content contains.
    """
    path = urlsplit(href).path
    if not path.startswith(root):
        return None
    rest = path[len(root):].strip("/")
    if not rest or rest.casefold() == "home" or "/" in rest:
        return None
    return f"{root}{rest}/"


def tabs(page: Page, company: Company) -> list[Tab]:
    """The tabs this page's navigation offers, in the order it shows them.

    The landing tab is not among them: the caller has already captured it.
    """
    root = company.path
    for selector in NAV_SELECTORS + (
        f'nav a[href*="{root}"]', f'a[href*="{root}"]',
    ):
        found = _tabs_under(page, selector, root)
        if found:
            return found
    return []


def _tabs_under(page: Page, selector: str, root: str) -> list[Tab]:
    anchors = page.locator(selector)
    try:
        count = anchors.count()
    except Exception:
        return []
    found: list[Tab] = []
    seen: set[str] = set()
    for index in range(count):
        anchor = anchors.nth(index)
        try:
            path = _tab_path(anchor.get_attribute("href") or "", root)
            if path is None or path in seen:
                continue
            name = anchor.inner_text().strip()
            if name.casefold().startswith("show all"):
                continue
        except Exception:
            continue
        seen.add(path)
        found.append(Tab(name or leaf(path), path))
    return found


def leaf(path: str) -> str:
    """The tab's own segment: /company/x/people/ -> people."""
    return path.rstrip("/").rsplit("/", 1)[-1]


# The Products tab is the one that `memberTabType` does not name, and it is
# LinkedIn's inconsistency rather than an oversight here: the marker is sent by
# the "discover" module, which that tab does not render. Measured on a six-tab
# company capture, the whole log carries exactly five markers -- ABOUT, JOBS,
# LIFE, PEOPLE, POSTS -- and no PRODUCT. What the Products tab does carry is the
# product urn, in the single call that is about products rather than about the
# organisation.
TAB_MARKERS = {"product": ("memberTabType:PRODUCT", "organizationProductUrn")}


# A profile dialog is fetched at one address every dialog shares, carrying none
# of the profile's own path:
#
#     /flagship-web/rsc-action/actions/navigation?screenId=<screen>&sduiid=<screen>
#
# So it needs a marker for the same reason a company tab does. The screen id is
# an interface name rather than a build hash, so it survives a deploy.
#
# An activity tab needs one too, for a different reason: it pushes its address
# rather than navigating, so there is no document request at it. Only Articles
# can have one. The Comments tab is fetched by a call whose variables name
# neither comments nor the tab, and whose `queryId` is a build hash, so there
# is nothing here that would still be true after a deploy.
SECTION_MARKERS = {
    "about-this-member": ("screenId=com.linkedin.sdui.flagshipnav"
                          ".trustverifications.ProfileVerificationDetailsDialog",),
    "activity-articles": ("contentType:ARTICLES",),
}


def selected(found: list[Tab], wanted: frozenset[str] | None) -> list[Tab]:
    """Which of the discovered tabs to capture.

    Matched against both the segment and the label, so 'people' and 'People'
    both name the same tab whichever the operator reaches for.
    """
    if wanted is None:
        return []
    if not wanted:
        return found
    return [tab for tab in found
            if leaf(tab.path) in wanted or tab.name.lower() in wanted]


def _tab_anchor(page: Page, tab: Tab):
    """Find the visible navigation control, not another link to the same URL."""
    for selector in NAV_SELECTORS:
        try:
            anchors = page.locator(f"{selector}[href$='{tab.path}']")
            count = anchors.count()
        except Exception:
            continue
        for index in range(count):
            anchor = anchors.nth(index)
            try:
                if anchor.is_visible():
                    return anchor
            except Exception:
                continue

    try:
        anchors = page.locator(f"a[href$='{tab.path}']")
        count = anchors.count()
    except Exception:
        return None
    for index in range(count):
        anchor = anchors.nth(index)
        try:
            if anchor.is_visible() and anchor.inner_text().strip() == tab.name:
                return anchor
        except Exception:
            continue
    return None


def open_tab(page: Page, tab: Tab) -> bool:
    """Click through to `tab`. False if the click never took us there.

    Clicking rather than navigating is not decoration. A bare navigation to the
    address arrives with no referer, on a site that already knows exactly who is
    asking and can see that the address was never linked from anywhere.
    """
    with trace.phase(trace.NET, "open tab") as span:
        try:
            anchor = _tab_anchor(page, tab)
            if anchor is None:
                span.detail = f"{tab.name}: navigation control not found"
                return False
            # A person reads the tab before reaching for it.
            time.sleep(random.uniform(0.3, 0.9))
            interaction.click(page, anchor, timeout=5000)
        except Exception as exc:
            span.detail = f"{tab.name}: could not click ({type(exc).__name__})"
            return False
        arrived = _wait_for(page, tab)
        span.detail = (f"{tab.name} · {tab.path}" if arrived
                       else f"{tab.name}: clicked, never arrived in "
                            f"{ARRIVAL_TIMEOUT_S:.0f}s")
    return arrived


def arrived_at(here: str, target: str) -> bool:
    """Whether `here` is the tab at `target`, or a page underneath it.

    Not equality. Some tabs redirect to a longer address -- Life lands on
    `/company/<slug>/life/<id>/` -- and demanding the exact path waits out the
    timeout on a tab that arrived in under a second.

    The separator is what keeps a sibling from matching: `/company/x/life-at-y`
    does not begin the tab `/company/x/life`.
    """
    here = here.rstrip("/")
    return here == target or here.startswith(target + "/")


def _wait_for(page: Page, tab: Tab) -> bool:
    """Wait for the address to become the tab's, then for its content to settle.

    Two separate waits because they can fail separately: the address changes the
    moment the click is handled, while the content it names is still being
    fetched, and capturing in between records an empty tab as a successful one.
    """
    deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
    target = tab.path.rstrip("/")
    while time.monotonic() < deadline:
        try:
            here = page.evaluate(LOCATION) or ""
        except Exception:
            # The context is torn down and rebuilt mid-route; ask again.
            here = ""
        if arrived_at(here, target):
            break
        time.sleep(0.2)
    else:
        return False

    previous = -1
    while time.monotonic() < deadline:
        time.sleep(random.uniform(*SETTLE_RANGE))
        try:
            current = len(page.inner_text("body"))
        except Exception:
            break
        if current == previous:
            break
        previous = current
    return True


PEOPLE_CAROUSEL = (
    '.org-people__insights-container .artdeco-carousel, '
    '[data-testid="carousel-container"]:has(a[role="checkbox"])'
)
MAX_CAROUSEL_POSITIONS = 20
CAROUSEL_STATE = ".artdeco-pagination__state--a11y"
CAROUSEL_PAGE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
VIEWER_SPECIFIC_PEOPLE_ANALYTICS = {"How you are connected"}
COMPANY_FEED_EMPTY = (
    ".feed-container-theme "
    ".scaffold-finite-scroll__empty .artdeco-empty-state"
)
COMPANY_EVENT_GROUPS = "section.org-event-card__card-spacing"
COMPANY_EVENT_CARDS = ".org-events__list-item--event-card"
COMPANY_CAROUSELS = {
    "home": ("Featured",),
    "about": ("Commitments",),
}
EXTRACT_CAROUSEL_ITEMS = """carousel => Array.from(
  carousel.querySelectorAll('.artdeco-carousel__item, [data-testid="carousel-child-container"] > *')
).map(item => {
  const link = item.querySelector('a[href]')?.href || '';
  const words = (item.innerText || '').replace(/\\s+/g, ' ').trim();
  return {key: `${link}\\n${words}`, html: item.outerHTML};
}).filter(item => item.key.trim() && item.html)"""
EXTRACT_PEOPLE_INSIGHTS = """carousel => Array.from(
  carousel.querySelectorAll('.artdeco-carousel__item-container, [data-testid="carousel-child-container"] > *')
).map(container => {
  const title = container.querySelector('.insight-container__title h3, h3');
  let values = Array.from(container.querySelectorAll(
    '.org-people-bar-graph-element'
  )).map(row => ({
    count: (row.querySelector('strong')?.textContent || '').trim(),
    label: (row.querySelector('.org-people-bar-graph-element__category')
      ?.textContent || '').trim(),
  })).filter(value => value.count && value.label);
  if (!values.length) values = Array.from(container.querySelectorAll('a[role="checkbox"]'))
    .map(row => {
      const parts = Array.from(row.querySelectorAll('p')).map(p => p.textContent.trim());
      return {label: parts[0], count: parts[parts.length - 1]};
    }).filter(value => value.label && /^[\\d,]+$/.test(value.count));
  return {title: (title?.textContent || '').trim(), values};
}).filter(card => card.title && card.values.length)"""
PEOPLE_ANALYTICS_DOCUMENT = """cards => {
  const root = document.documentElement.cloneNode(true);
  const carousel = root.querySelector(
    '.org-people__insights-container .artdeco-carousel');
  const section = document.createElement('section');
  section.setAttribute('data-career_lens', 'people-analytics');
  const heading = document.createElement('h2');
  heading.textContent = 'People analytics';
  section.appendChild(heading);
  for (const card of cards) {
    const article = document.createElement('article');
    const title = document.createElement('h3');
    title.textContent = card.title;
    article.appendChild(title);
    const values = document.createElement('ul');
    for (const value of card.values) {
      const item = document.createElement('li');
      const count = document.createElement('strong');
      count.textContent = value.count;
      item.append(count, document.createTextNode(` ${value.label}`));
      values.appendChild(item);
    }
    article.appendChild(values);
    section.appendChild(article);
  }
  if (carousel) {
    carousel.replaceWith(section);
  } else {
    root.querySelector('body')?.appendChild(section);
  }
  return {
    html: '<!doctype html>' + root.outerHTML,
    text: root.querySelector('body')?.innerText || '',
  };
}"""


def _carousel_page(carousel: object) -> tuple[int, int] | None:
    try:
        marker = carousel.locator(CAROUSEL_STATE).first
        if not marker.count():
            return None
        state = marker.inner_text().strip()
    except Exception:
        return None
    match = CAROUSEL_PAGE.fullmatch(state)
    return ((int(match.group(1)), int(match.group(2))) if match else None)


def _wait_for_carousel_page(carousel: object, previous: int, *,
                            pause: bool = True) -> tuple[int, int] | None:
    deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        state = _carousel_page(carousel)
        if state is not None and state[0] != previous:
            if pause:
                time.sleep(random.uniform(*SETTLE_RANGE))
            return state
        time.sleep(0.2)
    return None


def _wait_for_people_carousel(page: Page) -> tuple[object, tuple[int, int] | None] | None:
    """Wait until LinkedIn has mounted the carousel and its page state."""
    deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            carousels = page.locator(PEOPLE_CAROUSEL)
            if carousels.count():
                carousel = carousels.first
                state = _carousel_page(carousel)
                if state is not None or carousel.get_attribute("data-testid") == "carousel-container":
                    return carousel, state
        except Exception:
            pass
        time.sleep(0.2)
    return None


def company_feed_is_empty(page: Page) -> bool:
    """Whether LinkedIn rendered the feed's structural empty-state container."""
    try:
        return bool(page.locator(COMPANY_FEED_EMPTY).count())
    except Exception:
        return False


def company_events_is_empty(page: Page) -> bool:
    """Whether loaded event groups contain no event cards."""
    try:
        groups = page.locator(COMPANY_EVENT_GROUPS).count()
        return groups > 0 and page.locator(COMPANY_EVENT_CARDS).count() == 0
    except Exception:
        return False


def load_company_carousels(page: Page, section: str) -> dict | None:
    """Capture bounded virtualized carousels identified by their UI heading."""
    labels = COMPANY_CAROUSELS.get(section, ())
    captured = []
    for label in labels:
        try:
            heading = page.get_by_role(
                "heading", name=label, exact=True).first
            if not heading.count():
                continue
            scope = heading.locator(
                "xpath=ancestor::*[self::section or contains("
                "concat(' ', normalize-space(@class), ' '), ' artdeco-card ') "
                "or (self::div and .//*[@data-testid='carousel-container'])][1]"
            )
            carousel = scope.locator('.artdeco-carousel, [data-testid="carousel-container"]').first
            if not carousel.count():
                continue
            state = _carousel_page(carousel)
            if state is None and carousel.get_attribute("data-testid") != "carousel-container":
                continue
        except Exception:
            continue

        items: dict[str, str] = {}

        def collect(_position: int) -> None:
            try:
                rendered = carousel.evaluate(EXTRACT_CAROUSEL_ITEMS)
            except Exception:
                return
            for item in rendered if isinstance(rendered, list) else []:
                if not isinstance(item, dict):
                    continue
                key = " ".join(str(item.get("key") or "").split())
                markup = str(item.get("html") or "")
                if key and markup:
                    items.setdefault(key, markup)

        progress = _walk_carousel(page, carousel, state, collect)
        captured.append({
            "label": label,
            "items": list(items.values()),
            **progress,
        })
    return {"carousels": captured} if captured else None


def company_carousel_progress(snapshots: dict | None) -> list[dict]:
    """Return traversal facts without copying captured card markup."""
    progress = []
    for snapshot in (snapshots or {}).get("carousels", []):
        progress.append({
            "label": snapshot.get("label"),
            "positions_visited": snapshot.get("positions_visited"),
            "positions_total": snapshot.get("positions_total"),
            "reached_end": snapshot.get("reached_end"),
        })
    return progress


def _carousel_scope(document, label: str):
    headings = document.xpath(
        ".//*[self::h2 or self::h3][normalize-space()=$label]", label=label)
    for heading in headings:
        node = heading.getparent()
        while node is not None:
            carousels = node.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), "
                "' artdeco-carousel ') or @data-testid='carousel-container']"
            )
            if carousels:
                return node, carousels[0]
            node = node.getparent()
    return None, None


def company_carousel_content(page: Page, snapshots: dict | None,
                             *, document: str | None = None) -> tuple[str, bytes]:
    """Replace virtualized carousels in a stored copy, never in LinkedIn's DOM."""
    source = document if document is not None else page.content()
    if not snapshots or not snapshots.get("carousels"):
        return page.inner_text("body"), source.encode("utf-8")

    root = lxml_html.document_fromstring(source)
    for snapshot in snapshots["carousels"]:
        label = str(snapshot.get("label") or "").strip()
        items = snapshot.get("items") or []
        if not label or not items:
            continue
        scope, original = _carousel_scope(root, label)
        if scope is None:
            body = root.find("body")
            scope = etree.SubElement(body if body is not None else root, "section")
            etree.SubElement(scope, "h2").text = label
        elif original is not None:
            original.getparent().remove(original)
        consolidated = etree.SubElement(scope, "div")
        consolidated.set("class", "artdeco-carousel")
        consolidated.set("data-career_lens", "consolidated-carousel")
        for markup in items:
            try:
                fragments = lxml_html.fragments_fromstring(str(markup))
            except (etree.ParserError, ValueError):
                continue
            for fragment in fragments:
                if isinstance(fragment, etree._Element):
                    consolidated.append(fragment)

    body = root.find("body")
    text = body.text_content() if body is not None else root.text_content()
    stored = lxml_html.tostring(root, encoding="utf-8", method="html",
                               doctype="<!DOCTYPE html>")
    return text, stored


def _walk_carousel(page: Page, carousel: object,
                   state: tuple[int, int] | None, collect,
                   *, max_positions: int = MAX_CAROUSEL_POSITIONS) -> dict:
    """Visit a bounded carousel once, then restore its initial position."""
    if state is None:
        visited = set()
        advances = 0
        reached_end = False
        for _ in range(max(1, max_positions)):
            try:
                signature = carousel.evaluate('''node => Array.from(node.querySelectorAll(
                  '[data-testid="carousel-child-container"] > *'
                )).map(item => [item.textContent, Math.round(item.getBoundingClientRect().x)])''')
            except Exception:
                break
            key = repr(signature)
            if not signature or key in visited:
                break
            visited.add(key)
            collect(len(visited))
            try:
                control = carousel.get_by_role("button", name="Next", exact=True)
                if not control.count() or control.is_disabled():
                    reached_end = True
                    break
                if len(visited) >= max_positions:
                    break
                time.sleep(random.uniform(0.3, 0.9))
                interaction.click(page, control, timeout=5000)
                advances += 1
                time.sleep(random.uniform(*SETTLE_RANGE))
            except Exception:
                break
        for _ in range(advances):
            try:
                control = carousel.get_by_role("button", name="Previous", exact=True)
                if not control.count() or control.is_disabled():
                    break
                interaction.click(page, control, timeout=5000)
                time.sleep(random.uniform(*SETTLE_RANGE))
            except Exception:
                break
        return {"positions_visited": len(visited), "positions_total": None, "reached_end": reached_end}
    first, total = state
    visited: set[int] = set()
    limit = max(1, max_positions)

    while len(visited) < limit:
        current = state[0]
        if current in visited:
            break
        visited.add(current)
        collect(current)
        if current >= total or len(visited) >= limit:
            break
        try:
            control = carousel.get_by_role(
                "button", name="Next", exact=True).first
            if not control.count() or control.is_disabled():
                break
            time.sleep(random.uniform(0.3, 0.9))
            interaction.click(page, control, timeout=5000)
        except Exception:
            break
        changed = _wait_for_carousel_page(carousel, current)
        if changed is None:
            break
        state = changed

    reached_end = state[0] >= total
    while state[0] > first:
        current = state[0]
        try:
            control = carousel.get_by_role(
                "button", name="Previous", exact=True).first
            if not control.count() or control.is_disabled():
                break
            interaction.click(page, control, timeout=5000)
        except Exception:
            break
        changed = _wait_for_carousel_page(carousel, current, pause=False)
        if changed is None:
            break
        state = changed

    return {
        "positions_visited": len(visited),
        "positions_total": total,
        "reached_end": reached_end,
    }


def load_people_analytics(page: Page) -> dict | None:
    """Capture bounded virtualized People insights, then restore the first view."""
    ready = _wait_for_people_carousel(page)
    if ready is None:
        return None
    carousel, state = ready
    cards: dict[str, dict] = {}

    def collect(_position: int) -> None:
        try:
            rendered = carousel.evaluate(EXTRACT_PEOPLE_INSIGHTS)
        except Exception:
            return
        if not isinstance(rendered, list):
            return
        for raw in rendered:
            if not isinstance(raw, dict):
                continue
            title = " ".join(str(raw.get("title") or "").split())
            values = []
            for value in raw.get("values") or []:
                if not isinstance(value, dict):
                    continue
                count = " ".join(str(value.get("count") or "").split())
                label = " ".join(str(value.get("label") or "").split())
                if count and label:
                    values.append({"count": count, "label": label})
            previous = cards.get(title)
            if title and values and (
                    previous is None
                    or len(values) > len(previous["values"])):
                cards[title] = {"title": title, "values": values}

    progress = _walk_carousel(page, carousel, state, collect)
    return {"cards": list(cards.values()), **progress}


def company_people_content(page: Page, analytics: dict) -> tuple[str, bytes]:
    """Replace LinkedIn's virtualized carousel with all captured insights."""
    cards = [
        card for card in analytics.get("cards") or []
        if card.get("title") not in VIEWER_SPECIFIC_PEOPLE_ANALYTICS
    ]
    document = page.evaluate(PEOPLE_ANALYTICS_DOCUMENT, cards)
    return str(document["text"]), str(document["html"]).encode("utf-8")


COMPANY_DATA_OPERATION = "voyagerOrganizationDashCompanies."


def _company_data_response(url: str, slug: str) -> bool:
    parts = urlsplit(url)
    if (parts.path != "/voyager/api/graphql"
            or COMPANY_DATA_OPERATION not in parts.query):
        return False
    variables = parse_qs(parts.query).get("variables", [""])[0]
    match = re.search(r"(?:^|[,(])universalName:([^,)]+)", variables)
    return bool(match and unquote(match.group(1)).lower() == slug.lower())


def _address(location: dict) -> dict[str, str]:
    raw = location.get("address")
    if not isinstance(raw, dict):
        return {}
    names = {
        "line1": "line1",
        "line2": "line2",
        "city": "city",
        "geographic_area": "geographicArea",
        "postal_code": "postalCode",
        "country": "country",
    }
    return {name: str(raw[source]).strip() for name, source in names.items()
            if raw.get(source) is not None and str(raw[source]).strip()}


def company_locations(payload: object) -> list[dict]:
    """Normalize location records from a company Voyager response."""
    found: list[dict] = []
    seen: set[tuple] = set()

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        groups = value.get("groupedLocations")
        if isinstance(groups, list):
            for group in groups:
                if not isinstance(group, dict):
                    continue
                coordinates = group.get("latLong")
                coordinates = coordinates if isinstance(coordinates, dict) else {}
                locations = group.get("locations")
                if not isinstance(locations, list):
                    continue
                for location in locations:
                    if not isinstance(location, dict):
                        continue
                    record = {
                        "name": str(group.get("localizedName") or "").strip(),
                        "description": str(location.get("description") or "").strip(),
                        "headquarters": bool(location.get("headquarter", False)),
                        "address": _address(location),
                    }
                    for name in ("latitude", "longitude"):
                        coordinate = coordinates.get(name)
                        if isinstance(coordinate, (int, float)):
                            record[name] = coordinate
                    key = (
                        record["name"], record["description"],
                        tuple(record["address"].items()),
                        record.get("latitude"), record.get("longitude"),
                    )
                    if key not in seen and any((record["name"],
                                                record["description"],
                                                record["address"])):
                        seen.add(key)
                        found.append(record)
        for child in value.values():
            visit(child)

    visit(payload)
    return found


class CompanyLocationResponses:
    """Hold matching responses until the About tab has finished loading."""

    def __init__(self, page: Page, slug: str):
        self.page = page
        self.slug = slug
        self.responses: list[object] = []
        self.attached = False

        def receive(response: object) -> None:
            try:
                if _company_data_response(response.url, self.slug):
                    self.responses.append(response)
            except Exception:
                pass

        self.receive = receive
        try:
            page.on("response", self.receive)
            self.attached = True
        except Exception:
            pass

    def locations(self) -> list[dict]:
        found: list[dict] = []
        for response in self.responses:
            try:
                raw = response.body().lstrip()
                if raw.startswith(b")]}'"):
                    raw = raw.partition(b"\n")[2]
                found.extend(company_locations(json.loads(raw)))
            except Exception:
                continue
        unique: list[dict] = []
        seen: set[str] = set()
        for location in found:
            key = json.dumps(location, sort_keys=True, ensure_ascii=True)
            if key not in seen:
                seen.add(key)
                unique.append(location)
        return unique

    def close(self) -> None:
        if not self.attached:
            return
        try:
            self.page.remove_listener("response", self.receive)
        except Exception:
            pass
        self.attached = False


def _location_lines(location: dict) -> list[str]:
    address = location.get("address") or {}
    address_line = ", ".join(filter(None, (
        address.get("line1"), address.get("line2"), address.get("city"),
        address.get("geographic_area"), address.get("postal_code"),
        address.get("country"),
    )))
    lines = [location.get("name") or location.get("description") or "Location"]
    if location.get("headquarters"):
        lines.append("Headquarters")
    if location.get("description") and location["description"] != lines[0]:
        lines.append(location["description"])
    if address_line:
        lines.append(address_line)
    if "latitude" in location and "longitude" in location:
        lines.append(f"Coordinates: {location['latitude']}, {location['longitude']}")
    return [str(line) for line in lines if line]


def company_about_content(page: Page, locations: list[dict],
                          carousels: dict | None = None) -> tuple[str, bytes]:
    """Add response-only locations to the ordinary rendered About capture."""
    text, stored = company_carousel_content(page, carousels)
    document = stored.decode("utf-8")
    if not locations:
        return text, stored

    articles = []
    text_locations = []
    for location in locations:
        lines = _location_lines(location)
        text_locations.append("\n".join(lines))
        articles.append("<article><h3>" + html.escape(lines[0]) + "</h3>"
                        + "".join(f"<p>{html.escape(line)}</p>"
                                  for line in lines[1:]) + "</article>")
    location_data = json.dumps(locations, ensure_ascii=True).replace("</", "<\\/")
    section = ("<section data-career_lens=\"company-locations\">"
               "<h2>Company locations</h2>"
               "<script type=\"application/json\">" + location_data + "</script>"
               + "".join(articles)
               + "</section>")
    marker = document.lower().rfind("</body>")
    document = (document[:marker] + section + document[marker:]
                if marker >= 0 else document + section)
    return text + "\n\nCompany locations\n\n" + "\n\n".join(text_locations), document.encode("utf-8")


JOBS_RESULTS_PATH = "/jobs/search-results/"
SHOW_ALL_JOBS = "Show all jobs"


def open_all_jobs(page: Page) -> bool:
    """Enter the finite results behind a company's Jobs tab by clicking it."""
    with trace.phase(trace.NET, "open all jobs") as span:
        control = None
        for role in ("link", "button"):
            try:
                candidate = page.get_by_role(
                    role, name=SHOW_ALL_JOBS, exact=True).first
                if candidate.count():
                    control = candidate
                    break
            except Exception:
                continue
        if control is None:
            try:
                candidate = page.locator(
                    f'a[href*="{JOBS_RESULTS_PATH}"]'
                ).filter(has_text=re.compile(r"^\s*Show all\s*$", re.I))
                recent = page.get_by_role(
                    "heading", name="Recently posted jobs", exact=True)
                if recent.count():
                    candidate = recent.locator(
                        'xpath=ancestor::*[self::section or self::div]'
                        '[.//a[contains(@href,"/jobs/search-results/")]][1]'
                    ).locator(f'a[href*="{JOBS_RESULTS_PATH}"]').filter(
                        has_text=re.compile(r"^\s*Show all\s*$", re.I))
                else:
                    candidate = None
                if candidate is not None and candidate.count() == 1:
                    control = candidate
            except Exception:
                pass
        if control is None:
            span.detail = "Show all jobs: control not found"
            return False

        try:
            time.sleep(random.uniform(0.3, 0.9))
            interaction.click(page, control, timeout=5000)
        except Exception as exc:
            span.detail = f"Show all jobs: could not click ({type(exc).__name__})"
            return False

        deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if page.evaluate(LOCATION) == JOBS_RESULTS_PATH:
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            span.detail = "Show all jobs: clicked, never reached job results"
            return False

        _wait_for_job_content(page)
        span.detail = JOBS_RESULTS_PATH
        return True


SHOW_ALL_EVENTS = "Show all events"


def open_all_events(page: Page, company: Company) -> bool:
    """Enter the complete events view linked from a company's Home page."""
    target = f"/{company.kind}/{company.slug}/events"
    with trace.phase(trace.NET, "open all events") as span:
        try:
            control = page.get_by_role(
                "link", name=SHOW_ALL_EVENTS, exact=True).first
            if not control.count():
                control = page.locator(f'a[href*="{target}"]').first
            if not control.count():
                span.detail = "Show all events: control not found"
                return False
            time.sleep(random.uniform(0.3, 0.9))
            interaction.click(page, control, timeout=5000)
        except Exception as exc:
            span.detail = ("Show all events: could not click "
                           f"({type(exc).__name__})")
            return False

        deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                if arrived_at(page.evaluate(LOCATION) or "", target):
                    break
            except Exception:
                pass
            time.sleep(0.2)
        else:
            span.detail = "Show all events: clicked, never reached events"
            return False

        previous = -1
        while time.monotonic() < deadline:
            time.sleep(random.uniform(*SETTLE_RANGE))
            try:
                current = len(page.inner_text("body"))
            except Exception:
                break
            if current == previous:
                break
            previous = current
        span.detail = target
        return True


def _job_signature(page: Page) -> str:
    """One stable value that changes when the result list changes page."""
    try:
        first = page.locator('a[href*="/jobs/view/"]').first
        return first.get_attribute("href") or ""
    except Exception:
        return ""


def wait_for_job_results(page: Page) -> bool:
    """Wait until a job result list has cards on screen."""
    return _wait_for_job_content(page)


def _wait_for_job_content(page: Page, previous: str = "") -> bool:
    """Wait until job cards exist, or differ from the preceding page."""
    deadline = time.monotonic() + ARRIVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        current = _job_signature(page)
        if current and current != previous:
            time.sleep(random.uniform(*SETTLE_RANGE))
            return True
        time.sleep(0.2)
    return False


def open_job_page(page: Page, number: int) -> bool:
    """Click a numbered job-results page and wait for its cards to change."""
    if number < 2:
        return True
    try:
        control = page.locator(
            f"button[aria-label='Page {number}']").first
        if not control.count():
            return False
        before = _job_signature(page)
        time.sleep(random.uniform(0.3, 0.9))
        interaction.click(page, control, timeout=5000)
    except Exception:
        return False
    return _wait_for_job_content(page, before)


def _returned_to(here: str, tab: Tab) -> bool:
    """Whether history reached this destination's route semantics.

    Home accepts only its two equivalent routes, not arbitrary descendants.
    Real tabs retain descendant matching because LinkedIn can redirect Life to
    `/company/<slug>/life/<id>/` while still being on the Life tab.
    """
    target = tab.path.rstrip("/")
    destination = company_url(tab.url)
    if destination is not None and destination.tab in {"", "home"}:
        return is_company_home(here, destination.path)
    return arrived_at(here, target)


def return_to_company_tab(page: Page, tab: Tab, *, max_hops: int) -> bool:
    """Walk history back, then wheel to the tab's stable top-page position."""
    def ready(here: str) -> bool | None:
        if not _returned_to(here, tab):
            return None
        try:
            interaction.scroll_to_top(page)
            return True
        except Exception:
            return False

    for _ in range(max(1, max_hops)):
        try:
            here = page.evaluate(LOCATION) or ""
        except Exception:
            here = ""
        state = ready(here)
        if state is not None:
            return state
        try:
            page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            return False
        time.sleep(random.uniform(*SETTLE_RANGE))
    try:
        return bool(ready(page.evaluate(LOCATION) or ""))
    except Exception:
        return False


# Job search is a split view: its result list and selected-job panel scroll
# independently. LinkedIn identifies the result pane by component key and its
# children by job-card component keys, so use that ownership instead of where
# the pane happens to fall on a particular screen.
MARK_JOB_RESULTS_SCROLLER = """() => {
  const marked = document.querySelector('[data-wf-scroller]');
  if (marked) {
    marked.removeAttribute('data-wf-scroller');
    marked.removeAttribute('data-wf-scroller-crop');
  }
  let best = document.querySelector(
    '[data-testid="lazy-column"][componentkey="SearchResultsMainContent"]');
  if (!best || !best.querySelector('[componentkey^="job-card-component-ref-"]'))
    best = [...document.querySelectorAll('[data-testid="lazy-column"]')]
      .find(el => el.querySelector('[componentkey^="job-card-component-ref-"]'));
  if (!best) return null;
  best.setAttribute('data-wf-scroller', '1');
  best.setAttribute('data-wf-scroller-crop', '1');
  // A pagination control lives at the bottom of this pane. Clicking it can
  // carry that position into the next page, so every capture starts at item 1.
  best.scrollTop = 0;
  return {
    top: best.scrollTop,
    height: best.scrollHeight,
    client: best.clientHeight,
    left: Math.round(best.getBoundingClientRect().left),
    right: Math.round(best.getBoundingClientRect().right),
    viewport: window.innerWidth,
  };
}"""


def prepare_job_results(page: Page) -> dict | None:
    """Mark and reset LinkedIn's left job-list scroller for capture."""
    try:
        return page.evaluate(MARK_JOB_RESULTS_SCROLLER)
    except Exception:
        return None


JOB_RESULTS_HAS_MORE = r"""expectedPage => {
  const list = document.querySelector(
    '[data-testid="lazy-column"][componentkey="SearchResultsMainContent"]');
  if (!list || !list.querySelector(
      '[componentkey^="job-card-component-ref-"]')) return null;

  const pagination = list.querySelector(
    '[data-testid="pagination-controls-list"]');
  if (!pagination) return false;

  const enabled = button => !button.disabled
    && button.getAttribute('aria-disabled') !== 'true';
  const pageNumber = button => {
    const match = (button.getAttribute('aria-label') || '')
      .match(/^Page\s+(\d+)$/);
    return match ? Number(match[1]) : null;
  };
  const current = pagination.querySelector(
    'button[aria-current="true"], button[aria-current="page"]');
  const currentPage = current ? pageNumber(current) : null;
  if (currentPage !== expectedPage) return null;

  const numbered = [...pagination.querySelectorAll(
    'button[aria-label^="Page "]')];
  if (numbered.some(button => enabled(button)
      && pageNumber(button) > currentPage)) return true;

  const next = pagination.querySelector('button[aria-label="Next"]');
  return Boolean(next && enabled(next));
}"""


def job_results_has_more(page: Page, number: int) -> bool | None:
    """Return whether a higher job-results page is currently available."""
    try:
        value = page.evaluate(JOB_RESULTS_HAS_MORE, number)
    except Exception:
        return None
    return value if isinstance(value, bool) else None


EXTRACT_JOB_RESULTS = """() => {
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const alumni = /\\b(?:school|company) alumni(?: who| that)? work here$/i;
  const readable = el => {
    const copy = el.cloneNode(true);
    for (const hidden of copy.querySelectorAll('[aria-hidden="true"]')) {
      hidden.remove();
    }
    return clean(copy.textContent);
  };
  const list = [...document.querySelectorAll('[data-testid="lazy-column"]')]
    .filter(el => el.getAttribute('componentkey') === 'SearchResultsMainContent')
    .sort((a, b) => a.getBoundingClientRect().left
                    - b.getBoundingClientRect().left)[0];
  if (!list) return [];

  const found = new Map();
  for (const card of list.querySelectorAll(
      '[componentkey^="job-card-component-ref-"]')) {
    const key = card.getAttribute('componentkey') || '';
    const id = key.replace('job-card-component-ref-', '');
    if (!/^\\d+$/.test(id) || found.has(id)) continue;

    const dismiss = [...card.querySelectorAll('[aria-label]')].find(el =>
      /^Dismiss .+ job$/.test(clean(el.getAttribute('aria-label'))));
    let title = dismiss
      ? clean(dismiss.getAttribute('aria-label')).replace(/^Dismiss | job$/g, '')
      : '';
    const lines = [...card.querySelectorAll('p')]
      .map(readable).filter(line => line && !alumni.test(line));
    if (!title && lines.length) {
      title = lines[0].replace(/ \\(Verified job\\).*$/, '');
    }
    if (!title) continue;
    found.set(id, {id, title, details: lines.slice(1)});
  }
  return [...found.values()];
}"""


def job_results_content(page: Page) -> tuple[str, bytes]:
    """Return only semantic job cards, without the selected-job/account pane."""
    rows = page.evaluate(EXTRACT_JOB_RESULTS)
    if not rows:
        raise RuntimeError("LinkedIn job result cards not found")

    text_rows = []
    articles = []
    for row in rows:
        job_id = str(row.get("id", ""))
        title = " ".join(str(row.get("title", "")).split())
        if not re.fullmatch(r"\d+", job_id) or not title:
            continue
        details = [" ".join(str(value).split())
                   for value in row.get("details", [])
                   if str(value).strip() and not re.search(
                       r"\b(?:school|company) alumni(?: who| that)? work here$",
                       " ".join(str(value).split()), re.IGNORECASE)]
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        text_rows.append("\n".join((title, *details, url)))
        detail_html = "".join(f"<p>{html.escape(value)}</p>"
                              for value in details)
        articles.append(
            f'<article><h2><a href="{url}">{html.escape(title)}</a></h2>'
            f"{detail_html}</article>")
    if not articles:
        raise RuntimeError("LinkedIn job result cards were empty")
    document = ("<!doctype html><html><head><meta charset=\"utf-8\">"
                "<title>Job listings</title></head><body><main>"
                "<h1>Job listings</h1>" + "".join(articles)
                + "</main></body></html>")
    return "\n\n".join(text_rows), document.encode("utf-8")


def job_results_title(tab: Tab, company_title: str | None, number: int) -> str:
    """A stable artifact title, independent of LinkedIn's selected job."""
    base = (company_title or "").partition("|")[0].strip()
    base = re.sub(r"^\(\d+\)\s*", "", base)
    for suffix in (": Jobs", " - Jobs", " Jobs"):
        if base.lower().endswith(suffix.lower()):
            base = base[:-len(suffix)].strip()
            break
    if not base:
        company = company_url(tab.url)
        base = company.slug.replace("-", " ").title() if company else "Company"
    return f"{base} jobs - page {number} | LinkedIn"


# Whatever identifies the account running the browser rather than the subject
# being read. Kept mounted for LinkedIn's component tree, hidden from the live
# view, and omitted from the stored HTML copy.
#
# It is on the page twice: the bar and the message list a reader sees, and the
# payload the app boots from, which restates the same identity as data. The
# payload goes whole rather than searched by field name, so that a field added
# later cannot quietly start being stored again.
STRIP_ACCOUNT = """() => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  const TALLEST_BAR = 120;
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  for (const sel of ['#global-nav', '#msg-overlay']) {
    for (const el of document.querySelectorAll(sel)) hide(el);
  }

  // LinkedIn's newer messaging overlay lives inside the interop portal's
  // shadow root. Keep the portal itself available for profile dialogs.
  const interop = document.querySelector('#interop-outlet');
  const shadowMessaging = interop?.shadowRoot?.querySelector('#msg-overlay');
  if (shadowMessaging) hide(shadowMessaging);

  // The bar is the one header spanning the top of the window. A profile's
  // other headers are dialog headings, and they measure zero.
  const root = document.querySelector('#root');
  if (root) {
    for (const el of root.querySelectorAll('header')) {
      const box = el.getBoundingClientRect();
      if (box.top <= 1 && box.width >= window.innerWidth * 0.9
          && box.height > 0 && box.height <= TALLEST_BAR) hide(el);
    }
  }

  // Feed and post pages mirror the signed-in account in a sidebar. Identify
  // the module by its viewer-only actions rather than its generated classes,
  // wording or the account's own name. Other asides contain page content and
  // the LinkedIn footer, so they stay.
  const identityActions = [
    'a[href*="profileFormEntryPoint=identityModuleMirror"]',
    'a[href*="upsellSlotId=IDENTITY_MODULE"]',
    'a[href*="/mynetwork/invite-connect/connections"]',
  ].join(',');
  for (const aside of document.querySelectorAll('aside')) {
    if (aside.querySelector(identityActions)) hide(aside);
  }

  // The feed composer carries the viewer's profile picture and profile link.
  // Its classes and component keys are generated, but the profile-picture and
  // draft mounts are stable. Walk only to their small card, identified by its
  // own publishing controls, and never through the main content boundary.
  const viewerPicture = document.querySelector(
      '#shareboxProfilePictureComponentRef');
  const draft = document.querySelector('#draft-text-replaceable-component');
  if (viewerPicture && draft) {
    let composer = draft;
    for (let up = 0; composer && up < 8; up++) {
      if (composer.tagName === 'MAIN' || composer.tagName === 'BODY') break;
      const publishingControl = composer.querySelector(
          'a[href="/article/new/"], #video-medium, #image-medium');
      if (composer.contains(viewerPicture) && publishingControl) {
        const shell = composer.parentElement;
        const target = (shell && shell.children.length === 1
                        && shell.tagName !== 'MAIN' && shell.tagName !== 'BODY')
          ? shell : composer;
        hide(target);
        break;
      }
      composer = composer.parentElement;
    }
  }

  // The boot payload, whatever it happens to hold.
  for (const el of document.querySelectorAll(
      'code[id^="bpr-guid-"], code[id^="datalet-bpr-guid-"]')) hide(el);
  return gone;
}"""


# The suggestions LinkedIn puts beside what you asked for: other people and
# other organisations, chosen for the account rather than for the subject.
#
# A profile names the whole category. Its rails are `profileAside<slug>` and
# its own cards are `profileCards<slug>`, so the prefix takes a rail added
# later along with the three there are today.
PROFILE_RAILS_ID = "profileAside"

# A company page offers no such id. Affiliated pages, which is about the
# organisation and stays, has markup identical to the two that go, and the only
# ids are render-order counters that differ on every load. So these go on their
# headings, and that is the limitation: another language keeps them.
#
# Prefixes rather than whole, because two are named after what they sit beside
# and so read differently on every page.
IRRELEVANT_RAILS = (
    # Beside a profile, where the id above should have caught them already.
    # Kept in case that naming changes.
    "more profiles for you",
    "explore premium profiles",
    "you might like",
    "people you may know",
    # In a profile's own column rather than its rail.
    "people who follow",
    # On a company page.
    "people also follow",
    "pages people also viewed",
    "people also viewed",
    "ex-coworkers who work here",
    "recommended jobs for you",
    "view employees in recommended jobs",
    "see jobs where you’re a top applicant",
    "unlock insights on",
    "school alumni",
)

# The heading names the rail; the section around it is the rail. Bounded so a
# heading sitting loose cannot walk up into the document, and guarded against
# `main` so a mis-match can never take the content with it.
STRIP_RAILS = """([rails, railId]) => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  const main = document.querySelector('main');
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  for (const el of document.querySelectorAll(`[id^="${railId}"]`)) {
    if (!main || !el.contains(main)) hide(el);
  }

  for (const head of document.querySelectorAll('h1, h2, h3, h4')) {
    const text = (head.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase();
    if (!rails.some(rail => text.startsWith(rail))) continue;
    let node = head;
    for (let up = 0; node && up < 8; up++) {
      if (node === main || node.tagName === 'BODY') break;
      const otherHeading = [...node.querySelectorAll('h1, h2, h3, h4')]
        .some(h => h !== head && h.tagName <= head.tagName && !rails.some(rail =>
          (h.textContent || '').replace(/\\s+/g, ' ').trim().toLowerCase().startsWith(rail)));
      if (otherHeading) break;
      if (node.tagName === 'SECTION' ||
          (node.tagName === 'DIV' && node.querySelector('a, button, img'))) {
        if (!main || !node.contains(main)) hide(node);
        break;
      }
      node = node.parentElement;
    }
  }
  return gone;
}"""


STRIP_ALUMNI = r"""() => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  const alumni = /\b(?:school|company) alumni(?: who| that)? work here$/i;
  const clean = text => (text || '').replace(/\s+/g, ' ').trim();
  let gone = 0;
  const hide = el => {
    if (el.closest('[data-career-lens-artifact-only]')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  for (const el of document.querySelectorAll('a, p, li, span')) {
    const label = clean(el.textContent);
    if (!alumni.test(label)) continue;
    let target = el;
    for (let up = 0; up < 6; up++) {
      const parent = target.parentElement;
      if (!parent || parent.tagName === 'MAIN' || parent.tagName === 'BODY'
          || clean(parent.textContent) !== label) break;
      target = parent;
    }
    hide(target);
  }
  return gone;
}"""


# The advertising workers. Their contents are a separate document and never reach
# the stored HTML, so this is for the pictures.
#
# `data-ad-banner` first, which a company page sets and which no wording can
# break. A profile offers only the accessibility label, and the two shells do
# not agree on it -- "advertisement" against "Sidebar advertisement container"
# -- so that one is matched loosely, and another language would keep it.
#
# Hidden with the box around them, which alone would leave a hole of exactly
# their size. That box is whatever is no taller than the frame: the wrappers
# measure 252px around a 250px frame, and the rail above jumps to 3676px. The
# rail itself is out of reach, and must be: it holds Affiliated pages too.
AD_BOX_SLACK_PX = 40

STRIP_ADS = """(slack) => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };
  for (const pixel of document.querySelectorAll(
      'img[data-test-third-party-tracking-pixel]')) {
    hide(pixel);
  }
  for (const frame of document.querySelectorAll(
      'iframe[data-ad-banner], iframe[title*="advertis" i]')) {
    const box = frame.getBoundingClientRect();
    let node = frame;
    for (let up = 0; up < 6; up++) {
      const parent = node.parentElement;
      if (!parent || parent.tagName === 'BODY') break;
      if (parent.getBoundingClientRect().height > box.height + slack) break;
      node = parent;
    }
    hide(node);
  }
  return gone;
}"""


# What is there for a screen reader rather than a reader: the tray popups
# arrive in, and the menu that jumps past the page. Both measure zero, so
# neither reaches a screenshot, but innerText carries them.
#
# The newer skip controls are React-owned. Removing them leaves LinkedIn's
# event tree stale and prevents later dialogs from mounting, so they are hidden
# in the live page and marked for removal from the stored HTML copy.
STRIP_SCAFFOLDING = """() => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  const main = document.querySelector('main');
  let gone = 0;
  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  // The older application names both, and neither name is English.
  for (const el of document.querySelectorAll(
      '#artdeco-toasts__wormhole, .global-nav__a11y-menu')) hide(el);

  // The newer one names only the tray, through the heading inside it.
  for (const head of document.querySelectorAll('[data-testid="toasts-title"]')) {
    const tray = head.closest('section') || head;
    if (!main || !tray.contains(main)) hide(tray);
  }

  // Its skip links carry no attribute, so they go on their wording. The group
  // goes whole where it holds nothing else; how many there are differs by page.
  const skips = [...document.querySelectorAll('a, button')].filter(
      el => /^Skip to /.test((el.textContent || '').replace(/\\s+/g, ' ').trim()));
  const groups = new Set(skips.map(el => el.parentElement).filter(
      group => group && [...group.children].every(child => skips.includes(child))));
  for (const group of groups) hide(group);
  for (const el of skips) {
    if (![...groups].some(group => group.contains(el))) hide(el);
  }
  return gone;
}"""


# The job search pins this account-specific prompt over the bottom of its
# result column. Its classes are generated; the exact heading and the small
# subtree containing it are the stable boundary.
STRIP_JOB_ALERTS = """() => {
  const host = location.hostname;
  if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return 0;
  const label = 'Get job alerts for this search';
  const clean = text => (text || '').replace(/\\s+/g, ' ').trim();
  const main = document.querySelector('main');
  let gone = 0;

  const hide = el => {
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };

  for (const head of document.querySelectorAll('h1, h2, h3, h4')) {
    if (clean(head.textContent) !== label) continue;
    let node = head;
    let exact = null;
    for (let up = 0; node && up < 6; up++, node = node.parentElement) {
      if (node === main || node.tagName === 'BODY') break;
      if (clean(node.textContent) !== label) break;
      exact = node;
      if (node !== head && node.querySelector(
          'button, input[type="checkbox"], [role="switch"]')) break;
    }
    if (exact && exact !== main) {
      hide(exact);
    }
  }
  return gone;
}"""


PROFILE_IMAGE_ROLES = {"linkedin_person", "linkedin_company"}


def collect_profile_images(
        page: Page, html_bytes: bytes | None, kind: str,
        url: str) -> tuple[dict[str, object], str | None]:
    """Fetch the profile's own photo and cover while the page is still open.

    Returns the images by artifact kind, and a reason if either was wanted and
    could not be taken. A profile with no cover is neither -- it is simply a
    profile with no cover.

    The URLs are read from the HTML that was just stored rather than from the
    live DOM, so the same reading can be tested against a fixture. Only the
    fetch needs the browser.
    """
    if kind not in PROFILE_IMAGE_ROLES or not html_bytes:
        return {}, None

    try:
        document = lxml_html.fromstring(html_bytes)
        targets = _image_targets(document, kind, url)
    except Exception as exc:  # noqa: BLE001 -- a page we cannot read is not a crash
        return {}, f"{type(exc).__name__}: {exc}"

    found: dict[str, object] = {}
    problems: list[str] = []
    for artifact, url in (("profile_image", targets.profile_image),
                          ("cover_image", targets.cover_image)):
        if url is None:
            continue
        with trace.phase(trace.NET, f"fetch {artifact}", url[:80]):
            try:
                found[artifact] = media.fetch(page, url)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"{artifact}: {type(exc).__name__}: {exc}")
    return found, "; ".join(problems) or None


def _image_targets(document, kind: str, url: str):
    """The two URLs worth fetching, read the way each recipe reads its page."""
    from .parse.company_common import extract_company_header
    from .parse.dom import primary_content
    from .parse.extractors import _profile_heading
    from .parse.images import company_images, person_images

    if kind == "linkedin_company":
        return company_images(extract_company_header(document, url), document)
    # The handle is what tells the owner's heading from a section's: the top
    # card links to the profile's own path.
    handle = urlsplit(url).path.strip("/").split("/")[-1]
    return person_images(document, _profile_heading(primary_content(document), handle))


# What rides along inside a dialog that is not about the member: the account's
# own notification region, what LinkedIn is selling, and a pitch card carrying
# the reader's photograph rather than the member's.
#
# Removed rather than hidden. A dialog capture stores `outerHTML` directly, so
# a hidden node is still in the bytes -- and the dialog is closed straight
# after, so there is nothing left to put back.
STRIP_DIALOG_NOISE = """() => {
  // No host guard, unlike STRIP_ADS: that one is called on any page, and this
  // is called only by the dialog captures, which have already established the
  // page is a LinkedIn profile.
  let gone = 0;
  // Hidden and marked, never removed. `OUTER_HTML` drops marked nodes from a
  // detached clone, so the stored bytes lose them while the live tree keeps
  // every node LinkedIn's own code is holding a reference to. Removing them
  // for real worked on the dialog and broke the profile underneath it: closing
  // the dialog unmounted a subtree whose children had gone, which took the
  // profile view with it, and the next section found an empty `main` and
  // reported the member was not open to work.
  const drop = (el) => {
    if (!el || !el.isConnected || !el.parentElement) return;
    if (el.hasAttribute('data-career-lens-artifact-only')) return;
    el.setAttribute('data-career-lens-artifact-only', '');
    el.style.setProperty('display', 'none', 'important');
    gone++;
  };
  // The block a node belongs to: LinkedIn lays a dialog out as rows under one
  // column, so the row is whichever child of that column holds this node.
  const rowOf = (node) => {
    while (node.parentElement) {
      const testid = node.parentElement.getAttribute('data-testid');
      if (testid === 'lazy-column' || testid === 'dialog-content') break;
      node = node.parentElement;
    }
    return node;
  };
  for (const dialog of document.querySelectorAll('dialog[open], [role="dialog"]')) {
    // Says how many notifications this account has. Ours, not the member's,
    // and it is the only heading in a dialog that has none of its own -- which
    // is how one came to be stored as a document titled "0 notifications".
    for (const title of dialog.querySelectorAll('[data-testid="toasts-title"]')) {
      drop(title.closest('section') || title);
    }
    // Keyed on the upsell's own address, because the copy moves: the reply
    // multiplier alone has been served as 4.6x and as 5.7x.
    for (const link of dialog.querySelectorAll(
        'a[href*="/premium/products"], a[href*="upsellSlotId="]')) {
      drop(rowOf(link));
    }
    // A pitch card carrying a photograph. The member's own picture is content
    // and stays where it is; this one sits inside a control rather than beside
    // a detail, and that is what tells the two apart.
    const body = dialog.querySelector('[data-testid="dialog-content"]');
    for (const card of (body || dialog).querySelectorAll('[role="button"]')) {
      if (card.querySelector('img')) drop(card);
    }
  }
  return gone;
}"""


def strip_dialog_noise(page: Page) -> int:
    """Take LinkedIn's own furniture out of a dialog before it is stored.

    Called by the dialog captures rather than from `strip_irrelevant`, which
    runs on pages: a page carries no dialog, so every rule here would be paid
    on every capture to find nothing.
    """
    try:
        return int(page.evaluate(STRIP_DIALOG_NOISE))
    except Exception:
        # A page that cannot be asked is one mid-navigation. Storing the
        # dialog with its furniture beats failing the capture over it.
        return 0


def strip_irrelevant(page: Page) -> int:
    """Hide account furniture, rails, ads, scaffolding and job alerts.

    Safe on any page and at any point: it does nothing away from LinkedIn, and
    hiding what is already hidden is not counted twice. Both matter, because it runs
    repeatedly -- the message list arrives late, so one pass would miss it.
    """
    gone = 0
    for script, argument in ((STRIP_ACCOUNT, None),
                             (STRIP_RAILS, [list(IRRELEVANT_RAILS),
                                            PROFILE_RAILS_ID]),
                             (STRIP_ALUMNI, None),
                             (STRIP_ADS, AD_BOX_SLACK_PX),
                             (STRIP_SCAFFOLDING, None),
                             (STRIP_JOB_ALERTS, None)):
        try:
            gone += int(page.evaluate(script) if argument is None
                        else page.evaluate(script, argument))
        except Exception:
            # A page that cannot be asked is a page mid-navigation. The next
            # call gets it, and there is always one before anything is stored.
            continue
    return gone
