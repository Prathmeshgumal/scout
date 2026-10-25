"""Every page of a LinkedIn company, captured one at a time.

The markup handling all lives in `dom.py` and was ported whole. What is here is
the run structure: which pages to visit, in what order, what a failure costs,
and when to stop. It is the company counterpart of `sections.expand()` and has
the same shape for the same reason -- yielding one capture at a time lets the
caller make each durable, and visible, before the next page is fetched.

Nothing here touches the database. A capture carries screenshot bands and HTML
as bytes, so holding a whole company's worth in a list would keep eight
fully-scrolled LinkedIn pages in memory at once and store none of them if the
run died partway through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..capture import detect, overlays, trace
from ..capture.page import (
    FEED_SCROLL_PX, MAX_SCROLL_PX, PageCapture, capture_page,
)
from . import dom, sections

# The tabs that are feeds rather than documents: they have no end to reach, so
# they are bounded instead of followed. Every other tab has one and is read to
# it -- About already runs to about 4460px, close enough to the feed budget to
# be cut by it if this were applied everywhere.
FEED_SECTIONS = ("posts",)

DEFAULT_JOB_PAGES = 3


@dataclass
class TabCapture:
    """A captured company page, named for the tab it came from."""

    section: str
    capture: PageCapture
    # Set on the last capture before the sweep gives up, which today means a
    # blocking challenge. Navigation failures end the sweep without a capture
    # to hang this on -- they are reported through the journal instead.
    stop: bool = False


@dataclass
class CompanyConfig:
    """What the sweep reads, and nothing else."""

    scratch_dir: Path
    # A pace.Tempo, or None. Typed loosely to keep this dataclass from
    # importing the browser stack, matching sections.ExpandConfig.
    tempo: Any = None
    # dom.wanted_tabs()'s vocabulary: None turns the sweep off, an empty set
    # means every tab the page offers -- which cannot be spelled as a list,
    # because we do not know them in advance.
    tabs: frozenset[str] | None = field(default_factory=frozenset)
    # Pages of job-search results to walk behind the Jobs tab, not individual
    # postings: each is a list of roughly 25 listings.
    job_pages: int = DEFAULT_JOB_PAGES


def expand(page, config: CompanyConfig,
           company: dom.Company) -> Iterator[TabCapture]:
    """Yield every page of this company, one at a time.

    Nothing here raises. A tab that will not open is skipped, and a page we
    cannot get back to costs the remaining tabs rather than the run.

    Every decision to stop is made in this function rather than inside the
    helpers, so that no generator yields from a `finally` block. A consumer
    that stops iterating closes the generator, raising GeneratorExit at the
    suspended yield -- and a `finally` that then yielded would turn that into
    `RuntimeError: generator ignored GeneratorExit`.
    """
    # The caller owns request scope. This is the arrived route: LinkedIn can
    # redirect a root request to Home, while other tabs cannot host the sweep.
    if company.tab not in {"", "home"} or config.tabs is None:
        return

    home = dom.Tab("Home", company.path)
    wants_events = not config.tabs or "events" in config.tabs
    if wants_events and dom.open_all_events(page, company):
        yield from _events(page, config)
        if not dom.return_to_company_tab(page, home, max_hops=3):
            # The browser is somewhere unknown. Reading the nav from here would
            # find the wrong tabs, or none at all.
            trace.note("could not return to the company page from events")
            return

    found = dom.selected(dom.tabs(page, company), config.tabs)
    if not found:
        return

    for tab in found:
        section = dom.leaf(tab.path)
        responses = _locations(page, company, tab)
        if not dom.open_tab(page, tab):
            # A tab that will not open costs that tab; a browser that is gone
            # costs every tab left, and must not be reported as a clean sweep.
            sections.raise_if_gone(page, during=f"the {tab.name} tab")
            if responses is not None:
                # Attached before the click, so it comes off here too: a
                # page-level listener that outlives its tab keeps firing for
                # every later navigation in the run.
                responses.close()
            # Skipped rather than fatal, and said out loud: a run where three
            # of nine tabs failed must not look like one where none did.
            trace.note(f"tab not opened: {tab.name}")
            continue

        captured = _capture_tab(page, config, tab, section, responses)
        challenge = detect.detect(page)
        blocked = challenge is not None and detect.is_blocking(challenge)
        if blocked:
            trace.note(f"blocked on {tab.name}; leaving the rest")
        yield TabCapture(section, captured, stop=blocked)
        if blocked:
            return

        if section == "jobs" and captured.ok and config.job_pages > 0:
            if not dom.open_all_jobs(page):
                continue
            opened = 0
            for item in _job_pages(page, config, tab, captured.title):
                opened += 1
                yield item
                if item.stop:
                    return
            # The job-search view is a different page from the tab. Capturing
            # the remaining tabs from the wrong place would file them under
            # real roles, which is worse than capturing nothing.
            if not dom.return_to_company_tab(page, tab, max_hops=opened + 2):
                sections.raise_if_gone(
                    page, during="returning from the job pages")
                trace.note("could not return to the company Jobs tab")
                return


def _locations(page, company: dom.Company, tab: dom.Tab):
    """Start listening for the About tab's offices before the tab is opened.

    The office list arrives as a Voyager response while the tab loads, so the
    listener has to be attached first. Passive listening only -- never `route`,
    which would cost the browser its stealth.
    """
    if dom.leaf(tab.path) != "about":
        return None
    return dom.CompanyLocationResponses(page, company.slug)


def _capture_tab(page, config: CompanyConfig, tab: dom.Tab, section: str,
                 responses) -> PageCapture:
    """Capture whichever tab is now on screen.

    About and People are the two whose content does not survive an ordinary
    capture: the offices arrive only as a response, and LinkedIn unloads People
    insight cards that scroll out of the carousel. Both are folded into the
    stored document instead.
    """
    with trace.phase(trace.WORK, f"tab {section}") as span:
        locations = []
        if responses is not None:
            locations = responses.locations()
            responses.close()
        analytics = (dom.load_people_analytics(page)
                     if section == "people" else None)
        carousels = dom.load_company_carousels(page, section)
        empty_feed = (
            section in FEED_SECTIONS and dom.company_feed_is_empty(page)
        )

        extract = None
        if section == "about" and (locations or carousels):
            extract = lambda probe: dom.company_about_content(
                probe, locations, carousels=carousels)
        elif section == "people" and analytics is not None:
            extract = lambda probe: dom.company_people_content(probe, analytics)

        sections.open_clipped_text(page)
        captured = capture_page(
            page, config.scratch_dir, section, url_requested=tab.url,
            scroll=not empty_feed,
            dismiss=lambda probe: overlays.dismiss(probe, grace_s=0),
            tempo=config.tempo,
            scroll_budget=(FEED_SCROLL_PX if section in FEED_SECTIONS
                           else MAX_SCROLL_PX),
            strip=dom.strip_irrelevant,
            read_selector=(
                None if empty_feed or section not in FEED_SECTIONS
                else sections.ACTIVITY_RESULTS
            ),
            extract_content=extract)
        if analytics is not None:
            captured.extra["people_analytics"] = {
                "cards_captured": len(analytics["cards"]),
                "positions_visited": analytics["positions_visited"],
                "positions_total": analytics["positions_total"],
                "reached_end": analytics["reached_end"],
            }
        carousel_progress = dom.company_carousel_progress(carousels)
        if carousel_progress:
            captured.extra["carousels"] = carousel_progress
        span.detail = f"{captured.text_chars} chars ({captured.scroll_stopped})"
        return captured


def _events(page, config: CompanyConfig) -> Iterator[TabCapture]:
    """The full events view, which is linked from Home rather than in the nav.

    The caller has already navigated here and owns getting back; this only
    captures what is on screen. Yields exactly one capture.
    """
    overlays.dismiss(page, grace_s=0)
    challenge = detect.detect(page)
    blocked = challenge is not None and detect.is_blocking(challenge)
    with trace.phase(trace.WORK, "tab events") as span:
        sections.open_clipped_text(page)
        empty = dom.company_events_is_empty(page)
        captured = capture_page(
            page, config.scratch_dir, "events", url_requested=page.url,
            scroll=not empty,
            dismiss=lambda probe: overlays.dismiss(probe, grace_s=0),
            tempo=config.tempo, scroll_budget=MAX_SCROLL_PX,
            strip=dom.strip_irrelevant)
        span.detail = f"{captured.text_chars} chars"
    yield TabCapture("events", captured, stop=blocked)


def _job_pages(page, config: CompanyConfig, tab: dom.Tab,
               company_title: str | None) -> Iterator[TabCapture]:
    """Bounded pages of the job-search results behind "Show all jobs".

    Each is a list of roughly 25 listings, not one posting. An absent next page
    means the finite list ended early, which ends the loop and not the sweep.
    The caller has already clicked through and owns getting back.
    """
    for number in range(1, max(0, config.job_pages) + 1):
        if number > 1 and not dom.open_job_page(page, number):
            trace.note(f"jobs page {number} not available")
            return
        overlays.dismiss(page, grace_s=0)
        challenge = detect.detect(page)
        blocked = challenge is not None and detect.is_blocking(challenge)

        with trace.phase(trace.WORK, f"jobs page {number}") as span:
            scroller = dom.prepare_job_results(page)
            if scroller is None:
                trace.note(f"jobs page {number}: result list not found")
            captured = capture_page(
                page, config.scratch_dir, f"jobs-page-{number}",
                url_requested=page.url,
                dismiss=lambda probe: overlays.dismiss(probe, grace_s=0),
                tempo=config.tempo, scroll_budget=MAX_SCROLL_PX,
                strip=dom.strip_irrelevant,
                extract_content=dom.job_results_content)
            # LinkedIn's own title names whichever job it happened to select,
            # which differs between runs of the same page.
            captured.title = dom.job_results_title(tab, company_title, number)
            captured.extra["has_more"] = (
                dom.job_results_has_more(page, number) if captured.ok else None
            )
            span.detail = f"{captured.text_chars} chars"

        yield TabCapture(f"jobs-page-{number}", captured, stop=blocked)
        if blocked:
            trace.note(f"blocked on jobs page {number}; leaving the rest")
            return
