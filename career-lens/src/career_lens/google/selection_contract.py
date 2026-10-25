"""Browser-independent contract for choosing which results to open.

This module deliberately has no browser, database, API, or Pydantic imports.
`top_n` accounting is subtle enough that two implementations would drift, so
both runtimes decide selection here and only the opening is theirs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from ..capture.urls import canonical, classify
from .destinations import EXCLUDED_REASON, is_excluded
from .results_contract import SearchResult

CaptureMode = Literal["generic", "linkedin_shallow"]

#: A result Google ranked but this run did not reach.
BEYOND_TOP_N = "beyond_top_n"
#: A result this run opened, or intends to open.
TOP_N = "top_n"
#: A LinkedIn result with no session to read it with.
LINKEDIN_UNAVAILABLE = "linkedin_unavailable"


@dataclass(frozen=True)
class Selection:
    """One examined result and why it was or was not opened."""

    search_result: SearchResult
    selected: bool
    selection_reason: str


def classify_destination(url: str) -> CaptureMode:
    try:
        kind = classify(canonical(url))
    except Exception:
        return "generic"
    return ("linkedin_shallow"
            if kind in {"linkedin_person", "linkedin_company"}
            else "generic")


def is_linkedin_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def linkedin_url_on_host(url: str, verified_host: str) -> str:
    """Keep one LinkedIn route while moving it to the verified session host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    target = verified_host.strip().rstrip(".").lower()
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        raise ValueError(f"not a LinkedIn URL: {url}")
    if not (target == "linkedin.com" or target.endswith(".linkedin.com")):
        raise ValueError(f"not a LinkedIn verified host: {verified_host}")
    return urlunsplit((parts.scheme, target, parts.path, parts.query, ""))


def decline_reason(url: str, *, linkedin_available: bool) -> str | None:
    """Why this destination will not be opened, or None to open it.

    The reasons count differently against `top_n`, which is what
    `consumes_slot` decides.
    """
    if is_excluded(url):
        return EXCLUDED_REASON
    if is_linkedin_url(url) and not linkedin_available:
        return LINKEDIN_UNAVAILABLE
    return None


def consumes_slot(reason: str) -> bool:
    """Whether declining for this reason still uses up one of the `n`.

    `top_n` is a depth into the ranking, not a quota of successful captures:
    "the top five results" means the five Google put first, and a run that
    skipped ranks 1, 2, 4, 5, 6 and 7 to reach a fifth capture at rank 8 has
    read the top eight, whatever it was asked for.

    `linkedin_unavailable` is an exception. It is not
    a decision about the destination but a gap in this run -- the session that
    would have read it was not assigned -- so it hands its worker to the next
    result. Jobs Intelligence similarly hands on keyword-listing slots so it
    can reach company boards and actual job pages.
    """
    return reason == EXCLUDED_REASON


def plan_selection(
    results: Sequence[SearchResult],
    *,
    top_n: int,
    linkedin_available: bool,
) -> tuple[Selection, ...]:
    """Decide, before any click, which ranks this run will open."""
    consumed = 0
    observations: list[Selection] = []
    for item in results:
        declined = decline_reason(
            item.normalised_url, linkedin_available=linkedin_available)
        if consumed >= top_n:
            observations.append(Selection(item, False, BEYOND_TOP_N))
        elif declined is not None:
            consumed += consumes_slot(declined)
            observations.append(Selection(item, False, declined))
        else:
            consumed += 1
            observations.append(Selection(item, True, TOP_N))
    return tuple(observations)


def apply_resolution(
    observations: Sequence[Selection],
    index: int,
    resolved: SearchResult,
    *,
    linkedin_available: bool,
) -> tuple[Selection, ...]:
    """Re-decide one result after its opaque destination became knowable.

    Google hid this destination behind its own redirect, so what it is could
    not be known before the click. It is known now, and a destination we would
    have declined is still declined -- nothing about the page it landed on is
    captured. A slot given up for an unavailable session passes to the
    next result, and it passes to the first result that takes it, opened or
    skipped.
    """
    updated = list(observations)
    updated[index] = Selection(resolved, True, TOP_N)
    declined = decline_reason(
        resolved.normalised_url, linkedin_available=linkedin_available)
    if declined is None:
        return tuple(updated)
    updated[index] = Selection(resolved, False, declined)
    if consumes_slot(declined):
        return tuple(updated)
    for later_index in range(index + 1, len(updated)):
        later = updated[later_index]
        if later.selection_reason != BEYOND_TOP_N:
            continue
        later_decline = decline_reason(
            later.search_result.normalised_url,
            linkedin_available=linkedin_available)
        if later_decline is not None:
            updated[later_index] = Selection(
                later.search_result, False, later_decline)
            if consumes_slot(later_decline):
                break
            continue
        updated[later_index] = Selection(later.search_result, True, TOP_N)
        break
    return tuple(updated)


__all__ = [
    "BEYOND_TOP_N",
    "EXCLUDED_REASON",
    "LINKEDIN_UNAVAILABLE",
    "TOP_N",
    "CaptureMode",
    "Selection",
    "apply_resolution",
    "classify_destination",
    "consumes_slot",
    "decline_reason",
    "is_linkedin_url",
    "linkedin_url_on_host",
    "plan_selection",
]
