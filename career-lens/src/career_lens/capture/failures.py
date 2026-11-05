"""What a capture failure is, and which of `core.FAILURE_KINDS` it is.

Site-neutral on purpose. Every recipe -- LinkedIn, Google -- ends
in one of the same typed causes, because the retry policy branches on them and
a cause invented at a call site cannot be branched on. Lifted out of
`capture/driver.py` unchanged so a site package can classify a refusal without
importing LinkedIn's page rules to do it.
"""

from __future__ import annotations


class CaptureFailed(RuntimeError):
    """A capture that did not produce usable documents, with a typed cause.

    The cause is one of core.FAILURE_KINDS. Typed rather than free text because
    v2's retry policy branches on it -- a different identity after a challenge,
    a wait after a rate limit, never after not_found -- and a cause invented at
    the call site cannot be branched on.
    """

    def __init__(self, failure_kind: str, detail: str):
        super().__init__(f"{failure_kind}: {detail}")
        self.failure_kind = failure_kind
        self.detail = detail
        self.rate_limit = None


def raise_for_rate_limit(response) -> None:
    """Only a top-level navigation response, never an unrelated subresource."""
    if response is None or response.status != 429:
        return
    failure = CaptureFailed("blocked", "rate_limited: http 429")
    headers = response.headers
    failure.rate_limit = {
        "url": response.url,
        "retry_after": next((str(value) for key, value in headers.items()
                             if key.lower() == "retry-after"), None),
    }
    raise failure


class ProviderCaptureFailed(CaptureFailed):
    """A nested provider, rather than the outer recipe, rejected its session."""

    def __init__(self, provider: str, failure_kind: str, detail: str):
        super().__init__(failure_kind, detail)
        self.provider = provider


class BrowserGone(RuntimeError):
    """The page, browser context or browser disappeared during a capture."""


def browser_gone(page, exc: BaseException | str | None = None) -> bool:
    """Whether browser infrastructure, rather than one page action, failed.

    Either signal may arrive first: Playwright can raise ``TargetClosedError``
    before marking the page closed, while a browser killed between calls is
    visible only through ``page.is_closed()``.
    """
    if exc is not None:
        if type(exc).__name__ == "TargetClosedError":
            return True
        detail = str(exc)
        if "has been closed" in detail or "Target closed" in detail:
            return True
    probe = getattr(page, "is_closed", None)
    if probe is None:
        return False
    try:
        return bool(probe())
    except Exception:
        return True


def raise_if_browser_gone(
        page, exc: BaseException | str | None = None, *, during: str) -> None:
    """Stop a run whose remaining work has become impossible."""
    if browser_gone(page, exc):
        cause = exc if isinstance(exc, BaseException) else None
        raise BrowserGone(f"the browser closed during {during}") from cause


# Substrings that identify a timeout in a driver message. Matched rather than
# caught by type because Patchright raises one error class for many conditions
# and the message is what distinguishes them.
TIMEOUT_MARKERS = ("Timeout", "timed out", "exceeded")

# Which detect.Challenge kinds mean "signed out" rather than "challenged". The
# difference matters: a login wall is fixed by signing the profile in, and a
# checkpoint is fixed by a person solving it. Taken from detect.py's own
# vocabulary -- these are the exact strings it emits, not paraphrases.
LOGIN_KINDS = ("linkedin_authwall", "login_gate", "generic_login")
NOT_FOUND_KINDS = ("linkedin_not_found",)


def classify_failure(*, challenge, error: str | None,
                     http_status: int | None = None) -> str:
    """Which of core.FAILURE_KINDS this is.

    Ordered most specific first, with `browser_error` last and unconditional:
    an unclassified failure must never quietly become one of the named causes,
    because a wrong cause is worse than an unhelpful one once retry policy
    reads it.
    """
    if challenge is not None:
        if challenge.kind in LOGIN_KINDS:
            return "login_required"
        if challenge.kind in NOT_FOUND_KINDS:
            return "not_found"
        return "blocked"
    if http_status == 404:
        return "not_found"
    if error and any(marker in error for marker in TIMEOUT_MARKERS):
        return "timeout"
    return "browser_error"
