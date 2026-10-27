"""One request, into a list of captured documents.

A profile is captured whole: the profile page, then each `details/` section
behind its "Show all" control, then the activity views. Every one of those is a
separate page load and becomes its own Document with its own `section`, because
the parser takes them as a mapping of section name to HTML.

A company is captured whole too: the page itself, the events view linked from
it, each tab its navigation offers, and a bounded number of pages of the
job-search results behind Jobs. Nothing extracts from those yet -- the
projection tables are there and empty -- but the pages are held, so the day a
parser exists it reads stored bytes rather than LinkedIn.

An arbitrary URL is still one page load and one Document with section `page`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlsplit

from ..linkedin import company as company_module
from ..linkedin import dom as linkedin_dom
from ..linkedin import sections as linkedin_sectioning
from ..linkedin.recovery import recover_transient_error
from . import (
    detect,
    discovery,
    overlays,
    pace,
    referral,
    trace,
    verification,
)
from .browser import session
from .har import HarRecorder
from .options import DEFAULT_JOB_PAGES, linkedin_capture_sections
from .page import PageCapture, capture_page, wait_for_visuals
from .parking import reject_parked_redirect
from .writer import Document


# Re-exported, not redefined. Both halves are site-neutral and moved out so a
# site package can compose the page primitives without importing this module's
# LinkedIn page rules. The names stay here because `google/recipe.py` and the
# existing tests import them from the driver, and a second spelling is a second
# thing to drift.
from .documents import to_document as document
from .failures import CaptureFailed, classify_failure

from .arrival import ARRIVAL_SETTLE_RANGE  # noqa: F401 -- re-exported
from .arrival import dwell as _dwell
from .arrival import settle_arrival as _settle_arrival

DEFAULT_TEMPO = pace.TEMPOS["human"]


def capture_request(kind: str, url: str, *, browser_profile: str,
                    out_dir: Path,
                    discovery_chance: float = discovery.DEFAULT_GOOGLE_SEARCH_CHANCE,
                    rng=None, tempo=None, page=None,
                    before_search=None,
                    linkedin_sections: frozenset[str] | None = None,
                    job_pages: int | None = None,
                    ) -> Iterator[Document]:
    """Yield each captured document as it completes. Raises CaptureFailed.

    A generator rather than a list, so a caller can make each document durable
    -- and visible -- before the next page is fetched. Note the consequence:
    calling this does nothing at all. The browser opens, the pages load, and
    CaptureFailed is raised, only on iteration.
    """
    if rng is None:
        import random
        rng = random
    if page is None:
        # browser.session() yields a PAGE, not a context -- it starts the
        # driver, opens the page and closes everything on the way out.
        session_options = {"profile": browser_profile}
        if SHOW_CURSOR:
            session_options["show_cursor"] = True
        with session(**session_options) as owned_page:
            yield from _capture_on_page(
                kind, url, owned_page, out_dir, discovery_chance, rng, tempo,
                before_search, linkedin_sections, job_pages)
        return
    yield from _capture_on_page(
        kind, url, page, out_dir, discovery_chance, rng, tempo, before_search,
        linkedin_sections, job_pages)


def _check_arrival(page, response, url_requested: str) -> None:
    """Refuse a page that is rate limited, walled, or not there."""
    from .failures import raise_for_rate_limit

    raise_for_rate_limit(response)
    recover_transient_error(page)
    overlays.dismiss(page)
    challenge = detect.detect(page, response)
    if detect.is_blocking(challenge):
        raise CaptureFailed(
            classify_failure(challenge=challenge, error=None),
            f"{challenge.kind}: {challenge.evidence}")

    status = response.status if response is not None else None
    if status == 404:
        raise CaptureFailed("not_found", f"{url_requested} returned 404")


def _search_results(page, out_dir: Path, *, url: str, tempo=None,
                    job_pages: int | None = None,
                    recorder=None) -> Iterator[Document]:
    """The result pages of one LinkedIn job search."""
    keywords = " ".join(parse_qs(urlsplit(url).query).get("keywords", [""])[0].split())
    if not linkedin_dom.wait_for_job_results(page):
        trace.note("job search: no result cards appeared")
    config = company_module.CompanyConfig(
        scratch_dir=out_dir, tempo=tempo, tabs=None,
        job_pages=job_pages if job_pages is not None else DEFAULT_JOB_PAGES)
    for item in company_module.job_result_pages(
            page, config,
            title=lambda number: f"{keywords} jobs - page {number} | LinkedIn"):
        if not item.capture.ok:
            trace.note(f"job search: {item.section} could not be captured")
            break
        yield document(item.capture, item.section, recorder)
        if item.stop:
            break


def capture_arrived_page(
        kind: str, url_requested: str, page, out_dir: Path, *, response,
        tempo=None,
        provenance: dict[str, object] | None = None,
        linkedin_sections: frozenset[str] | None = None) -> PageCapture:
    """Capture one arrived page with the ordinary request's main-page rules."""
    from .failures import raise_for_rate_limit

    _check_arrival(page, response, url_requested)

    # Before the first pixel is taken. A profile's own sections mount after the
    # document is interactive, so without this the top card is photographed
    # sharp and everything under it as grey bars -- which is what the stored
    # screenshots showed while the parsed data, read from HTML captured
    # twenty seconds later, was perfectly correct.
    wait_for_visuals(
        page,
        scope=linkedin_sectioning.PRIMARY_COLUMN_SELECTORS,
    )
    linkedin_sectioning.open_clipped_text(page)
    # A company page is still opened when Home is not selected -- it names the
    # organisation and lists the available tabs -- but its Home-only carousel
    # and scroll work is skipped, because nothing will read it.
    home_requested = (
        kind != "linkedin_company"
        or linkedin_sections is None
        or "home" in linkedin_sections
    )
    home_carousels = (
        linkedin_dom.load_company_carousels(page, "home")
        if kind == "linkedin_company" and home_requested else None
    )
    main = capture_page(
        page, out_dir, "page", url_requested=url_requested,
        response=response, dismiss=overlays.dismiss, tempo=tempo,
        scroll=home_requested,
        strip=linkedin_dom.strip_irrelevant,
        extract_content=(
            (lambda probe: linkedin_dom.company_carousel_content(
                probe, home_carousels))
            if home_carousels else None
        ),
    )
    # After the page is read and before we leave it: the URLs are signed and
    # short-lived, so an image not taken here cannot be taken later at all.
    if main.ok:
        main.images, main.image_error = linkedin_dom.collect_profile_images(
            page, main.html_bytes, kind, main.final_url or url_requested)
        if main.image_error:
            # In metadata rather than a column of its own: fetching these is
            # recipe-owned, which is what metadata is for (see core.py).
            main.extra["image_error"] = main.image_error

    main.extra["provenance"] = (
        provenance
        if provenance is not None
        else {"arrival_method": "direct", "requested_url": url_requested}
    )
    carousel_progress = linkedin_dom.company_carousel_progress(home_carousels)
    if carousel_progress:
        main.extra["carousels"] = carousel_progress
    if not main.ok:
        raise CaptureFailed(
            classify_failure(challenge=None, error=main.error,
                             http_status=main.status),
            main.error or "the page could not be captured")
    return main


#: One spelling of the arrival line, shared by captures.
_arrival_note = referral.arrival_note


def _needs_requested_host(page, url: str) -> bool:
    """Whether a discovery arrival landed somewhere we should leave.

    A Google result for a LinkedIn profile is routinely a country host --
    `in.linkedin.com`, `sg.linkedin.com`. LinkedIn redirects those to `www`
    when the session cookie reaches them and serves a login wall when it does
    not, and which one happens is not ours to predict: the same URL, in the
    same profile, has done both. Relying on the redirect costs a whole run
    every time it does not come.

    So the host is corrected here rather than hoped for. This is deliberately
    the conspicuous navigation `docs/TODO.md` recorded as the reason for not
    doing it, and the trade has been made the other way now that a run is one
    of a thousand rather than one of a few.

    Only LinkedIn: `capture/urls.py` folds country hosts for LinkedIn profile
    and company roots alone, because on any other site a subdomain is a
    different page and moving to it would be a bug.
    """
    landed = (urlsplit(page.url).hostname or "").lower()
    wanted = (urlsplit(url).hostname or "").lower()
    return landed != wanted and _is_linkedin_host(landed)


def _is_linkedin_host(host: str) -> bool:
    bare = host.split(":", 1)[0]
    return bare == "linkedin.com" or bare.endswith(".linkedin.com")


def _new_recorder(page):
    """A recorder attached to this page's context, or None when logging is off.

    Attached here rather than in session() so both entry paths get it: a
    capture that opens its own browser and one handed a page by a caller.
    """
    if HAR_MODE not in ("headers", "bodies"):
        return None
    recorder = HarRecorder(bodies=HAR_MODE == "bodies")
    try:
        recorder.attach(page.context)
    except Exception:
        # Evidence about the capture must never cost the capture itself.
        return None
    return recorder


def _capture_on_page(kind: str, url: str, page, out_dir: Path,
                     discovery_chance: float, rng, tempo,
                     before_search,
                     linkedin_sections: frozenset[str] | None = None,
                     job_pages: int | None = None,
                     ) -> Iterator[Document]:
    recorder = _new_recorder(page)
    try:
        yield from _capture_documents(
            kind, url, page, out_dir, discovery_chance, rng, tempo,
            before_search, linkedin_sections, recorder, job_pages)
    finally:
        if recorder is not None:
            recorder.close()


def _capture_documents(kind: str, url: str, page, out_dir: Path,
                       discovery_chance: float, rng, tempo,
                       before_search,
                       linkedin_sections: frozenset[str] | None,
                       recorder,
                       job_pages: int | None = None) -> Iterator[Document]:
    detour = discovery.discover(
        page, url, out_dir, chance=discovery_chance, rng=rng,
        before_search=before_search)
    # How this run reached its target, on every path -- see arrival_note.
    trace.note(_arrival_note(detour))
    if detour is not None and detour.serp is not None:
        yield document(detour.serp, referral.SERP_SECTION, recorder)
    try:
        if detour is None or not detour.arrived:
            with trace.phase(trace.NET, "open target", url):
                response = page.goto(url, wait_until="domcontentloaded")
        elif _needs_requested_host(page, url):
            landed = (urlsplit(page.url).hostname or "").lower()
            with trace.phase(trace.NET, "leave country host") as span:
                span.detail = f"{landed} -> {(urlsplit(url).hostname or '')}"
                # A same-document navigation returns no response. The detour's
                # own is the honest fallback then -- it is the response for
                # these bytes, whatever host the address ended up reading.
                response = (page.goto(url, wait_until="domcontentloaded")
                            or detour.response)
        else:
            response = detour.response
    except Exception as exc:
        if kind == "generic" and classify_failure(challenge=None, error=str(exc)) == "timeout":
            # A redirect can already have committed while its document never
            # reaches DOMContentLoaded. Do not retry a confirmed sale listing.
            yield from reject_parked_redirect(url, page.url)
        raise CaptureFailed(
            classify_failure(challenge=None, error=str(exc)), str(exc)) from exc

    if kind == "generic":
        yield from reject_parked_redirect(
            url, page.url, http_status=(
                response.status if response is not None and response.url == page.url else None))
    response = verification.wait(page, response, rng=rng)

    if tempo is not None:
        _settle_arrival(page, rng)

    if kind == "generic":
        yield from reject_parked_redirect(
            url, page.url, http_status=(
                response.status if response is not None and response.url == page.url else None))

    if kind == "linkedin_job_search":
        _check_arrival(page, response, url)
        yield from _search_results(page, out_dir, url=url, tempo=tempo,
                                   job_pages=job_pages, recorder=recorder)
        return

    main = capture_arrived_page(
        kind, url, page, out_dir, response=response, tempo=tempo,
        provenance=(
            detour.provenance
            if detour is not None and detour.provenance is not None
            else None),
        linkedin_sections=linkedin_sections,
    )

    if kind == "linkedin_person":
        _dwell(main, tempo)
        yield document(main, "profile", recorder)
        # Expanded here, not at the request boundary: the stored selection is
        # what the operator asked for, and the capture sections it implies --
        # about-this-member as profile metadata, the given side of
        # recommendations -- are this layer's business.
        yield from _sections(
            page, out_dir, tempo=tempo,
            requested_sections=linkedin_capture_sections(linkedin_sections),
            recorder=recorder)
        return

    if kind == "linkedin_job":
        _dwell(main, tempo)
        yield document(main, "job", recorder)
        return

    if kind == "linkedin_company":
        # Read from the live route rather than the requested URL: LinkedIn
        # redirects a numeric /company/1283/ reference to the slug, and the
        # tab sweep has to work from where the browser actually is.
        organisation = linkedin_dom.company_on_page(page)
        requested_organisation = linkedin_dom.company_url(url)
        _dwell(main, tempo)
        yield document(main, "company", recorder)
        # Request scope comes from the submitted URL, not LinkedIn's redirect.
        # The live organization still supplies the resolved slug for navigation.
        if (organisation is not None and requested_organisation is not None
                and not requested_organisation.tab):
            yield from _tabs(page, out_dir, company=organisation,
                             tempo=tempo,
                             requested_sections=linkedin_sections,
                             recorder=recorder, job_pages=job_pages)
        return

    _dwell(main, tempo)
    yield document(main, "page", recorder)


def _sections(page, out_dir: Path, *, tempo=None,
              requested_sections: frozenset[str] | None = None, recorder=None,
              ) -> Iterator[Document]:
    """Every `details/` section and activity view behind this profile.

    A section that fails is skipped rather than fatal: a profile whose Patents
    page times out is still a profile with an Experience section, and the
    parser records `not_captured` for what is missing. Losing the whole capture
    over one section would be losing eight good pages to save none.

    Skipped sections are logged. The database learns about them eventually --
    a section that never arrives becomes `not_captured` in the parser's output
    -- but that is a fact discovered later, in another process, from an
    absence. At capture time the operator is the one who can act, and a run
    where three of nine sections failed must not look identical to one where
    none did.
    """
    from ..linkedin import sections as sectioning

    # scratch_dir, not out_dir: the field was renamed when ExpandConfig was
    # cut down to what expand() actually reads. Named rather than positional,
    # so the next rename fails loudly here instead of silently binding to
    # whatever field happens to come first.
    config = sectioning.ExpandConfig(
        scratch_dir=out_dir, tempo=tempo,
        requested_sections=requested_sections)
    found = 0
    skipped: list[str] = []
    for section in sectioning.expand(page, config, "section"):
        if not section.capture.ok:
            skipped.append(section.section)
            if section.unrendered:
                with trace.phase(trace.WORK, "section not captured") as span:
                    span.detail = f"{section.section}: {section.unrendered}"
            continue
        found += 1
        yield document(section.capture, section.section, recorder)
    if skipped:
        with trace.phase(trace.WORK, "sections skipped") as span:
            span.detail = f"{len(skipped)} of {found + len(skipped)}: " \
                          f"{', '.join(skipped)}"


# Set by cli.py at worker startup, the same way trace.configure() is. Module
# level because the setting has to reach here from a CLI flag through a
# Procrastinate task that carries neither.
TABS: frozenset[str] | None = frozenset()
SHOW_CURSOR = False
# "off" | "headers" | "bodies". Off by default: request logging is evidence
# about a capture rather than part of it, and nothing in capture -> extract ->
# query reads it, so an operator turns it on deliberately. This module-level
# default is what an unconfigured caller gets, so it has to agree with the CLI.
HAR_MODE = "off"
LINKEDIN_GOOGLE_SEARCH_CHANCE = discovery.DEFAULT_GOOGLE_SEARCH_CHANCE


def configure(*, tabs: frozenset[str] | None,
              show_cursor: bool = False, har: str = "off",
              linkedin_google_search_chance: float = (
                  discovery.DEFAULT_GOOGLE_SEARCH_CHANCE)) -> None:
    """Set browser behavior for this worker process."""
    global TABS, SHOW_CURSOR, LINKEDIN_GOOGLE_SEARCH_CHANCE
    global HAR_MODE
    TABS = tabs
    SHOW_CURSOR = show_cursor
    HAR_MODE = har
    LINKEDIN_GOOGLE_SEARCH_CHANCE = linkedin_google_search_chance


def _tabs(page, out_dir: Path, *, company: linkedin_dom.Company,
          tempo=None,
          requested_sections: frozenset[str] | None = None, recorder=None,
          job_pages: int | None = None,
          ) -> Iterator[Document]:
    """Every tab, the events view, and the job result pages of one company.

    A tab that fails is skipped for the same reason a section is: a company
    whose Life tab times out is still a company with an About and a People
    page, and the projection records what is missing.

    Skipped tabs are logged. A run where three of nine tabs failed must not
    look identical to one where none did.
    """
    tabs = TABS
    if requested_sections is not None:
        requested_tabs = requested_sections - {"home"}
        if tabs is None or not requested_tabs:
            tabs = None
        elif tabs:
            tabs = tabs & requested_tabs
            if not tabs:
                tabs = None
        else:
            tabs = requested_tabs
    config = company_module.CompanyConfig(
        scratch_dir=out_dir, tempo=tempo, tabs=tabs,
        job_pages=job_pages if job_pages is not None else DEFAULT_JOB_PAGES)
    skipped: list[str] = []
    found = 0
    for item in company_module.expand(page, config, company):
        if not item.capture.ok:
            skipped.append(item.section)
            continue
        found += 1
        yield document(item.capture, item.section, recorder)
    if skipped:
        with trace.phase(trace.WORK, "tabs skipped") as span:
            span.detail = f"{len(skipped)} of {found + len(skipped)}: " \
                          f"{', '.join(skipped)}"
