"""Browser-independent reading of a blocking Google challenge.

The worker classifies a challenge from a live page; a caller can
only report bounded page signs. Both then need the same answer to the same
question, so the decision lives here and takes only the signs.
"""

from __future__ import annotations

from ..capture import pressure
from ..capture.detect import (
    CONTENT_FLOOR,
    SOFT_SIGNS,
    STATUS_SIGNS,
    TEXT_SIGNS,
    URL_SIGNS,
    host_of,
)

#: A consent interstitial is not a block: it is dismissed and the results are
#: still readable behind it.
NOT_BLOCKING = frozenset({None, "consent", "google_consent"})


def challenge_kind(
    *,
    url: str,
    head: str,
    text_length: int,
    status: int | None,
) -> str | None:
    """Name what stands between this page and readable results, if anything."""
    lowered_url = url.casefold()
    kind = next((
        candidate
        for candidate, needles in URL_SIGNS
        if any(needle in lowered_url for needle in needles)
    ), None)
    if kind is None and status in STATUS_SIGNS:
        cloudflare = next(
            needles for candidate, needles in TEXT_SIGNS
            if candidate == "cloudflare"
        )
        kind = (
            "cloudflare"
            if any(needle in head for needle in cloudflare)
            else STATUS_SIGNS[status]
        )
    if kind is None:
        substantial = text_length >= CONTENT_FLOOR
        for candidate, needles in TEXT_SIGNS:
            if not any(needle in head for needle in needles):
                continue
            if substantial and candidate in SOFT_SIGNS:
                continue
            kind = candidate
            break
    if kind is not None:
        # The outcome is derivable from the kind alone: NOT_BLOCKING is the
        # whole rule.
        pressure.record(
            kind=kind,
            outcome="passed" if kind in NOT_BLOCKING else "blocked",
            host=host_of(url),
            evidence=f"HTTP {status}" if status else "page signs",
        )
    return kind


def blocking_google_challenge(
    *,
    url: str,
    head: str,
    text_length: int,
    status: int | None,
) -> bool:
    return challenge_kind(
        url=url, head=head, text_length=text_length, status=status,
    ) not in NOT_BLOCKING


__all__ = ["NOT_BLOCKING", "blocking_google_challenge", "challenge_kind"]
