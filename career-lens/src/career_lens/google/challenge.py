"""Google-specific, non-interactive challenge observations."""

from __future__ import annotations

from dataclasses import dataclass

from ..capture import captcha_assist, detect
from .browser import RESULT_LINKS


@dataclass(frozen=True)
class GoogleChallenge:
    kind: str
    evidence: str
    url: str
    status: int | None
    blocking: bool


def results_readable(page) -> bool:
    """Whether a results page is on screen, whatever was there before."""
    try:
        return page.locator(RESULT_LINKS).count() > 0
    except Exception:
        return False


def wait_for_assist(page, *, timeout: float | None = None, **polling) -> bool:
    """Give a solver-equipped profile time to answer a challenge.

    Returns whether results came back. A profile without the solver never
    waits: the challenge would stand for the whole budget and the caller would
    reach the same conclusion a good deal later.
    """
    if not captcha_assist.available():
        return False
    budget = captcha_assist.clear_budget() if timeout is None else timeout
    return captcha_assist.wait_until(
        lambda: results_readable(page), timeout=budget, **polling)


def classify_google(page, response) -> GoogleChallenge | None:
    """Return a typed Google wall, never pausing for operator interaction."""
    from ..capture.failures import raise_for_rate_limit

    raise_for_rate_limit(response)
    observed = detect.detect(page, response)
    if observed is None:
        return None
    try:
        readable = page.locator(RESULT_LINKS).count() > 0
    except Exception:
        readable = False
    if readable and (
        observed.kind in {"consent", "google_consent"}
        or observed.evidence.startswith("page text contains ")
    ):
        # The opening SERP text includes titles and snippets from arbitrary
        # providers. Their login/challenge vocabulary is evidence about a
        # result, not about Google. A URL/status wall above readable results
        # remains meaningful and is deliberately not suppressed here.
        return None
    consent = observed.kind in {"consent", "google_consent"}
    return GoogleChallenge(
        kind="consent" if consent else "blocked",
        evidence=f"{observed.kind}: {observed.evidence}",
        url=observed.url,
        status=observed.status,
        blocking=not consent,
    )
