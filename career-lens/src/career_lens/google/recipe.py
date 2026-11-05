"""Capture an unscrolled Google SERP and selected destinations incrementally."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from ..capture import detect, overlays, trace, verification
from ..capture.documents import to_document as document
from ..capture.driver import capture_arrived_page
from ..capture.failures import (
    CaptureFailed,
    ProviderCaptureFailed,
    classify_failure,
)
from ..capture.page import PageCapture, capture_page
from ..capture.urls import canonical, classify
from ..capture.writer import Document
from ..linkedin import dom as linkedin_dom
from . import dom as google_dom
from .browser import (
    BrowserEnvironment,
    RESULT_LINKS,
    SearchResult,
    environment,
    open_result,
    read_results,
    resolve_search_redirects,
    resolved_result,
    return_to_serp,
    search,
)
from .challenge import classify_google, wait_for_assist
from .selection_contract import (
    Selection,
    apply_resolution,
    classify_destination,
    decline_reason,
    is_linkedin_url as _is_linkedin_url,
    linkedin_url_on_host,
    plan_selection,
)
from .results_contract import reports_no_results
from .browser_contract import EMPTY_RESULTS_IMAGE
from .capture_contract import destination_failure


CaptureMode = Literal["generic", "linkedin_shallow"]

@dataclass(frozen=True)
class CapturedPage:
    capture_id: str | None
    mode: CaptureMode
    status: str
    url_requested: str
    url_final: str | None
    host: str
    http_status: int | None
    title: str | None
    captured_at: str | None
    text: str | None
    text_characters: int
    completeness: dict[str, object]
    error: dict[str, str] | None = None

    def to_contract(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "mode": self.mode,
            "status": self.status,
            "url_requested": self.url_requested,
            "url_final": self.url_final,
            "host": self.host,
            "http_status": self.http_status,
            "title": self.title,
            "captured_at": self.captured_at,
            "text": self.text,
            "text_characters": self.text_characters,
            "completeness": dict(self.completeness),
            "error": dict(self.error) if self.error is not None else None,
        }

    def to_serp_contract(self) -> dict:
        return {
            "capture_id": self.capture_id,
            "status": self.status,
            "url_final": self.url_final,
            "title": self.title,
            "text": self.text,
            "text_characters": self.text_characters,
            "completeness": dict(self.completeness),
        }


@dataclass(frozen=True)
class ObservedResult:
    search_result: SearchResult
    selected: bool
    selection_reason: str
    capture: CapturedPage | None = None

    @property
    def rank(self) -> int:
        return self.search_result.rank

    def to_contract(self) -> dict:
        item = self.search_result
        return {
            "rank": item.rank,
            "url": item.url,
            "normalised_url": item.normalised_url,
            "host": item.host,
            "title": item.title,
            "snippet": item.snippet,
            "selection": {
                "selected": self.selected,
                "reason": self.selection_reason,
            },
            "capture": (
                self.capture.to_contract() if self.capture is not None else None),
        }


@dataclass(frozen=True)
class RecipeResult:
    serp: CapturedPage
    results: tuple[ObservedResult, ...]
    google_host: str
    serp_url: str
    browser_environment: BrowserEnvironment
    searched_at: str
    failure: dict[str, str] | None = None

    def to_contract(self) -> dict:
        return {
            "search": {
                "google_host": self.google_host,
                "serp_url": self.serp_url,
                "browser_locale": self.browser_environment.locale,
                "browser_timezone": self.browser_environment.timezone,
                "searched_at": self.searched_at,
                "results_examined": len(self.results),
            },
            "failure": dict(self.failure) if self.failure else None,
            "serp": self.serp.to_serp_contract(),
            "results": [item.to_contract() for item in self.results],
        }


def _declined(url: str, linkedin_host) -> str | None:
    """Adapt the shared rule to this runtime's lazily-verified session."""
    return decline_reason(url, linkedin_available=linkedin_host() is not None)


def _captured(
    captured: PageCapture,
    *,
    capture_id: str,
    mode: CaptureMode,
    persisted: Document,
    serp: bool = False,
    failure: dict[str, str] | None = None,
) -> CapturedPage:
    screenshot_error = captured.screenshot_error
    if failure is not None:
        status = "failed"
        error = failure
    else:
        status = "partial" if screenshot_error else "succeeded"
        error = ({"kind": "capture_error", "detail": screenshot_error}
                 if screenshot_error else None)
    return CapturedPage(
        capture_id=capture_id,
        mode=mode,
        status=status,
        url_requested=captured.url_requested,
        url_final=captured.final_url,
        host=persisted.host,
        http_status=captured.status,
        title=captured.title,
        captured_at=persisted.captured_at.isoformat(),
        text=captured.text or "",
        text_characters=len(captured.text or ""),
        completeness={
            "scroll_stopped": (
                "not_scrolled" if serp else (captured.scroll_stopped or "unknown")),
            "scroll_growing": False if serp else captured.scroll_growing,
        },
        error=error,
    )


def _destination_failure(
        captured: PageCapture, challenge) -> dict[str, str] | None:
    return destination_failure(captured.status, challenge)


def _failed(item: SearchResult, mode: CaptureMode, kind: str,
            detail: str) -> CapturedPage:
    return CapturedPage(
        capture_id=None,
        mode=mode,
        status="failed",
        url_requested=item.url,
        url_final=None,
        host=item.host,
        http_status=None,
        title=None,
        captured_at=None,
        text=None,
        text_characters=0,
        completeness={"scroll_stopped": "not_started", "scroll_growing": False},
        error={"kind": kind, "detail": detail},
    )


def _failure_kind(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "timeout"
    return classify_failure(challenge=None, error=str(error))


def _reports_no_results(page) -> bool:
    """Whether Google explicitly rendered an empty-result state."""
    try:
        body = page.inner_text("body").casefold()
    except Exception:
        return False
    if reports_no_results(body):
        return True
    try:
        return page.locator(EMPTY_RESULTS_IMAGE).first.is_visible()
    except Exception:
        return False


#: Causes that end the whole run rather than one destination.
#:
#: Every other failure is a fact about the page it happened on: the destination
#: is recorded as failed and the next one is still worth fetching. These two are
#: facts about the run. A blob store that would not take a verified object will
#: not take the next one either, and a cancel is about the search, not about the
#: page that happened to be open when it arrived -- swallowing it here would
#: record a "failed" destination and then go and fetch the rest anyway.
ENDS_THE_RUN = frozenset({"storage_error", "cancelled"})


def run(
    page,
    *,
    query: str,
    top_n: int,
    out_dir: Path,
    before_search,
    linkedin_host,
    persist,
    progress=None,
    rng=random,
    tempo=None,
) -> RecipeResult:
    """Run one query, making each useful capture durable before continuing."""
    with trace.phase(trace.NET, "google navigation", "initial query") as span:
        response = search(
            page, query, before_submit=before_search, rng=rng)
        span.detail = "initial query submitted"
    searched_at = datetime.now(UTC).isoformat()
    serp_url = page.url
    google_host = (urlsplit(serp_url).hostname or "").lower()
    observed_environment = environment(page)
    challenge = classify_google(page, response)
    # Before the capture, not after: a challenge the solver answers leaves a
    # readable results page, and that page is the evidence worth storing.
    if challenge is not None and challenge.blocking:
        with trace.phase(trace.WORK, "google captcha assist") as span:
            cleared = wait_for_assist(page)
            if cleared:
                serp_url = page.url
                # The navigation response describes the challenge page. Once
                # an extension changes the document, its old status (often
                # 429) is no longer evidence about what is currently readable.
                challenge = classify_google(page, None)
                cleared = challenge is None or not challenge.blocking
            span.detail = "challenge answered" if cleared else "still blocked"

    try:
        with trace.phase(trace.WORK, "google serp capture") as span:
            serp_page = capture_page(
                page, out_dir, "serp", url_requested=serp_url,
                response=response, scroll=False, dismiss=overlays.dismiss,
                strip=google_dom.strip_personal,
            )
            serp_document = document(serp_page, "serp")
            serp_id = persist(serp_document)
            span.detail = f"{serp_id} · {len(serp_page.text or '')} chars"
    except Exception as error:
        if challenge is not None and challenge.blocking:
            raise CaptureFailed("blocked", challenge.evidence) from error
        raise
    serp_capture = _captured(
        serp_page, capture_id=serp_id, mode="generic",
        persisted=serp_document, serp=True)

    if challenge is not None and challenge.blocking:
        return RecipeResult(
            serp=serp_capture, results=(), google_host=google_host,
            serp_url=serp_url, browser_environment=observed_environment,
            searched_at=searched_at,
            failure={"kind": "blocked", "detail": challenge.evidence},
        )

    with trace.phase(trace.WORK, "google serp parse") as span:
        found = read_results(page)
        span.detail = f"{len(found)} unique organic results"
    if not found and not _reports_no_results(page):
        rendered_candidates = page.locator(RESULT_LINKS).count() > 0
        return RecipeResult(
            serp=serp_capture, results=(), google_host=google_host,
            serp_url=serp_url, browser_environment=observed_environment,
            searched_at=searched_at,
            failure={
                "kind": "parser_drift",
                "detail": (
                    "Google rendered result candidates that could not be parsed"
                    if rendered_candidates
                    else "Google returned no recognisable results state"
                ),
            },
        )
    if top_n == 0:
        found = resolve_search_redirects(page, found)
    with trace.phase(trace.WORK, "google result selection") as span:
        plan = plan_selection(
            found, top_n=top_n,
            linkedin_available=linkedin_host() is not None)
        observations = [
            ObservedResult(item.search_result, item.selected, item.selection_reason)
            for item in plan
        ]
        selected = sum(1 for item in plan if item.selected)
        # How deep into the ranking the run went, which is what `top_n` bounds.
        consumed = sum(
            1 for item in plan if item.selection_reason != "beyond_top_n")
        span.detail = (
            f"{selected} selected of {consumed} in the top {top_n} · "
            f"{len(found)} examined")

    def publish_progress():
        if progress is not None:
            progress(RecipeResult(
                serp=serp_capture, results=tuple(observations),
                google_host=google_host, serp_url=serp_url,
                browser_environment=observed_environment, searched_at=searched_at,
            ).to_contract())

    publish_progress()
    selected_indexes = [
        index for index, item in enumerate(observations) if item.selected]
    for ordinal, index in enumerate(selected_indexes):
        observation = observations[index]
        item = observation.search_result
        mode = classify_destination(item.normalised_url)
        try:
            verified_linkedin_host = (
                linkedin_host() if _is_linkedin_url(item.normalised_url)
                else None
            )
            declined = _declined(item.normalised_url, linkedin_host)
            if declined is not None:
                observations[index] = ObservedResult(item, False, declined)
                continue
            with trace.phase(
                    trace.NET, "google result click",
                    f"rank {item.rank} · {item.host}"):
                destination_response = open_result(page, item, rng=rng)
            destination_response = verification.wait(
                page, destination_response, rng=rng)
            if destination_response is not None and destination_response.status in {
                    404, 408, 500, 502, 503, 504, 999}:
                with trace.phase(trace.NET, "google result refresh", f"rank {item.rank}"):
                    page.wait_for_timeout(1000)
                    destination_response = page.reload(wait_until="domcontentloaded")
                    destination_response = verification.wait(page, destination_response, rng=rng)
            if item.opaque_redirect:
                item = resolved_result(item, page.url)
                mode = classify_destination(item.normalised_url)
                # What this destination is could not be known before the click.
                # The shared rule re-decides it, and may hand a given-up slot to
                # a later result; captures already taken are left alone.
                resolved_plan = apply_resolution(
                    [Selection(entry.search_result, entry.selected,
                               entry.selection_reason)
                     for entry in observations],
                    index,
                    item,
                    linkedin_available=linkedin_host() is not None,
                )
                for position, entry in enumerate(resolved_plan):
                    current = observations[position]
                    unchanged = (
                        position != index
                        and entry.selected == current.selected
                        and entry.selection_reason == current.selection_reason
                    )
                    if unchanged:
                        continue
                    observations[position] = ObservedResult(
                        entry.search_result, entry.selected,
                        entry.selection_reason)
                    if entry.selected and position != index:
                        selected_indexes.append(position)
            if observations[index].selected:
                with trace.phase(
                        trace.WORK, "google result capture",
                        f"rank {item.rank} · {mode}") as span:
                    destination_challenge = None
                    if (mode == "linkedin_shallow"
                            or _is_linkedin_url(page.url)):
                        verified_linkedin_host = linkedin_host()
                        if verified_linkedin_host is None:
                            raise CaptureFailed(
                                "login_required",
                                "the assigned LinkedIn session is unavailable")
                        linkedin_route = (
                            item.normalised_url
                            if mode == "linkedin_shallow"
                            else page.url
                        )
                        authenticated_url = linkedin_url_on_host(
                            linkedin_route, verified_linkedin_host)
                        if authenticated_url != page.url:
                            destination_response = page.goto(
                                authenticated_url,
                                wait_until="domcontentloaded")
                    if mode == "linkedin_shallow":
                        entity_kind = classify(canonical(page.url))
                        destination = capture_arrived_page(
                            entity_kind, item.url, page, out_dir,
                            response=destination_response, tempo=tempo,
                            provenance={
                                "arrival_method": "google_search",
                                "requested_url": item.url,
                            },
                        )
                    else:
                        # Unlike capture_arrived_page, the generic result path
                        # enters capture_page directly. Clear arrival banners
                        # before its scrolling screenshots; its own dismissal
                        # pass is deliberately after scrolling so it can catch
                        # overlays triggered by the scroll itself.
                        overlays.dismiss(page)
                        destination_challenge = detect.detect(
                            page, destination_response)
                        destination = capture_page(
                            page, out_dir, f"result-{item.rank:03d}",
                            url_requested=item.url,
                            response=destination_response,
                            scroll=True, dismiss=overlays.dismiss, tempo=tempo,
                            strip=(linkedin_dom.strip_irrelevant
                                   if _is_linkedin_url(page.url) else None),
                        )
                    if not destination.ok:
                        raise RuntimeError(destination.error or "capture failed")
                    destination_document = document(
                        destination, f"result-{item.rank:03d}")
                    capture_id = persist(destination_document)
                    span.detail = (
                        f"rank {item.rank} · {capture_id} · "
                        f"{len(destination.text or '')} chars")
                observations[index] = ObservedResult(
                    item, True, "top_n",
                    _captured(
                        destination, capture_id=capture_id, mode=mode,
                        persisted=destination_document,
                        failure=_destination_failure(
                            destination, destination_challenge)),
                )
        except CaptureFailed as error:
            if error.rate_limit or error.failure_kind in ENDS_THE_RUN:
                raise
            if mode == "linkedin_shallow" and error.failure_kind in {
                    "blocked", "login_required", "account_mismatch"}:
                raise ProviderCaptureFailed(
                    "linkedin", error.failure_kind, error.detail) from error
            observations[index] = ObservedResult(
                item, True, "top_n",
                _failed(
                    item, mode, error.failure_kind,
                    error.detail or str(error)),
            )
        except Exception as error:
            observations[index] = ObservedResult(
                item, True, "top_n",
                _failed(item, mode, _failure_kind(error), str(error)),
            )
        finally:
            publish_progress()

        if ordinal == len(selected_indexes) - 1:
            continue
        if return_to_serp(page, serp_url):
            continue
        detail = "could not restore the saved Google results page"
        for remaining in selected_indexes[ordinal + 1:]:
            later = observations[remaining]
            later_mode = classify_destination(
                later.search_result.normalised_url)
            observations[remaining] = ObservedResult(
                later.search_result, True, "top_n",
                _failed(
                    later.search_result, later_mode,
                    "browser_error", detail),
            )
        break

    publish_progress()
    return RecipeResult(
        serp=serp_capture,
        results=tuple(observations),
        google_host=google_host,
        serp_url=serp_url,
        browser_environment=observed_environment,
        searched_at=searched_at,
    )
