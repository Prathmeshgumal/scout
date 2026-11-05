"""Occasionally reach a target page through an exact result on a Google SERP.

Site-neutral on purpose. A visitor who types a company's name into Google and
clicks the result leaves a different trail from one who pastes an address, and
LinkedIn is worth reaching either way. What differs between sites is only
what a target *is*: the query a person would
type for it, and whether a given result is that same target rather than a
neighbour. Those two facts are the `ReferralTarget` protocol; everything else
-- the dice, the search, the SERP capture, the six ways this gives up -- is
here and is shared.

The detour is optional in the strongest sense: every failure returns an
attempt that did not arrive, and the caller navigates directly instead. A
Google problem must never cost the capture that Google was only a doorway to.

When a search was reached, its SERP capture is stored as evidence. It is never
an input to extraction: what the site served on its own page is the reading,
and a search result snippet is not that page.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..google.browser import SearchResult, open_result, read_results, search
from ..google.budget import SearchBudgetExhausted
from ..google.challenge import wait_for_assist
from . import detect, overlays
from .page import PageCapture, capture_page

if TYPE_CHECKING:  # `Page` is only ever an annotation here.
    from patchright.sync_api import Page

#: The capture name, and therefore the document section, a stored SERP gets.
#: One spelling for every site: a referral is the same evidence whichever page
#: it was a doorway to.
SERP_SECTION = "discovery-serp"


class ReferralTarget(Protocol):
    """One page a search could be made to arrive at."""

    @property
    def query(self) -> str:
        """What a person looking for this page would type into Google."""

    def matches(self, url: str) -> bool:
        """Whether `url` addresses this exact page rather than a neighbour."""


@dataclass
class ReferralAttempt:
    """What came of the detour, whether or not it worked.

    `arrived` is the only field the caller must branch on. `detail` explains a
    detour that did not arrive -- and a detour that did, when the SERP capture
    failed on the way -- so an operator is never left reading a LinkedIn error
    with nothing pointing at Google.
    """

    serp: PageCapture | None = None
    response: Any = None
    arrived: bool = False
    detail: str | None = None
    provenance: dict[str, object] | None = None


def selected(chance: float, *, rng: Any = random) -> bool:
    """Whether this visit takes the detour.

    The boundaries never consult the generator: `0` and `1` are settings an
    operator chose, not probabilities to sample, and a test that pins one must
    not have to feed a random source it is not exercising.
    """
    if chance <= 0:
        return False
    return chance >= 1 or rng.random() < chance


def matching_result(target: ReferralTarget | None,
                    results: list[SearchResult]) -> SearchResult | None:
    """The best-ranked result that is this exact target, or None.

    Rank order, not relevance: `read_results` yields the SERP's own order, so
    the first match is the one highest on the page -- in practice the first
    result. Deliberately not "whatever ranked first": an ad, a news story or a
    similarly named company can outrank a target, and opening one of those
    would store another entity's page under this request's identity.
    """
    if target is None:
        return None
    return next((result for result in results if target.matches(result.url)),
                None)


def arrival_note(attempt: ReferralAttempt | None) -> str:
    """One line saying how this run reached its target.

    Written on every path, including the quiet ones. Six distinct reasons the
    detour gives up used to be discarded by the caller, so an operator saw
    whatever the fallback navigation raised, attributed to the site, with
    nothing pointing at Google -- and a success was quieter still, leaving
    "did we go through Google?" answerable only from a provenance column.
    """
    if attempt is None:
        return "discovery: not attempted · navigating directly"
    if attempt.arrived:
        rank = (attempt.provenance or {}).get("search_result_rank")
        where = f" (rank {rank})" if rank is not None else ""
        return f"discovery: arrived through Google{where}"
    reason = attempt.detail or "gave up for an unrecorded reason"
    return f"discovery: {reason} · navigating directly"


def attempt(page: Page, target: ReferralTarget, requested_url: str,
            out_dir: Path, *, rng: Any = random,
            before_search=None) -> ReferralAttempt:
    """Search for `target` and click its result; never raise on the way.

    The caller has already decided the detour happens. Every return that is
    not `arrived` is a reason to navigate directly, and the caller does that
    without needing to know which reason it was.
    """
    before_search = before_search or (lambda _host: None)
    try:
        response = search(
            page, target.query, before_submit=before_search, rng=rng)
        overlays.dismiss(page)
    except SearchBudgetExhausted:
        return ReferralAttempt(detail="google search budget exhausted")
    except Exception as exc:
        return ReferralAttempt(
            detail=f"search failed: {type(exc).__name__}: {exc}")

    challenge = detect.detect(page, response)
    # Before the SERP capture, so a challenge the solver answers is stored as
    # the results page it became rather than the wall it was.
    if detect.is_blocking(challenge) and wait_for_assist(page):
        challenge = detect.detect(page, response)
    try:
        serp = capture_page(
            page, out_dir, SERP_SECTION, url_requested=page.url,
            response=response, scroll=False, dismiss=overlays.dismiss)
    except Exception as exc:
        serp = None
        capture_error = f"SERP capture failed: {type(exc).__name__}: {exc}"
    else:
        capture_error = None

    if detect.is_blocking(challenge):
        return ReferralAttempt(
            serp=serp,
            detail=f"blocked: {challenge.kind}: {challenge.evidence}")

    try:
        results = read_results(page)
        match = matching_result(target, results)
    except Exception as exc:
        return ReferralAttempt(
            serp=serp,
            detail=f"results unreadable: {type(exc).__name__}: {exc}")
    if match is None:
        return ReferralAttempt(
            serp=serp, detail=capture_error or "exact result not found")
    try:
        destination = open_result(page, match, rng=rng)
    except Exception as exc:
        return ReferralAttempt(
            serp=serp,
            detail=f"result click failed: {type(exc).__name__}: {exc}")
    return ReferralAttempt(
        serp=serp, response=destination, arrived=True, detail=capture_error,
        provenance={
            "arrival_method": "google_search",
            "requested_url": requested_url,
            "search_engine": "google",
            "search_query": target.query,
            "search_result_rank": match.rank,
        })
