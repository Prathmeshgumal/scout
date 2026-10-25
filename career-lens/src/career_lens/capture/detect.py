"""Recognise the ways a target refuses to show us the page.

A refusal is a result, not an error. Knowing *which* wall a target puts up is
what tells us whether it needs a login, a slower rate, or a different approach --
so each detection records its evidence and the page gets screenshotted anyway.

Body matching is limited to the opening of the document: a news article about
CAPTCHAs is not a CAPTCHA, and the real interstitials all announce themselves
immediately.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from . import pressure

if TYPE_CHECKING:      # annotations only
    from patchright.sync_api import Page, Response

BODY_WINDOW = 2000

# Above this much rendered text, a login prompt is an overlay on readable
# content, not a gate. Measured: a logged-out LinkedIn company page renders
# ~11k characters -- name, size, founding year, specialities and employees --
# behind a dismissible "Sign in to see who you already know" modal. Calling
# that an authwall would discard a page we successfully read, and would send
# the operator to solve a challenge that is not blocking anything.
CONTENT_FLOOR = 3000
LINKEDIN_NOT_FOUND_TEXT = (
    "this page doesn't exist",
    "this page doesn’t exist",
)


@dataclass
class Challenge:
    kind: str
    evidence: str
    url: str
    status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "evidence": self.evidence,
            "url": self.url,
            "status": self.status,
        }


# (kind, url substrings, body/title phrases). URL matches are stronger evidence
# than text, so they are checked first.
URL_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google_sorry", ("/sorry/index", "/sorry/image")),
    ("google_consent", ("consent.google.com", "consent.youtube.com")),
    ("linkedin_authwall", ("/authwall", "/uas/login", "linkedin.com/login")),
    ("captcha", ("/recaptcha/", "hcaptcha.com")),
    # Bot-management vendors, by the paths and resources each redirects to.
    # Unlike the site-specific rules above these apply to any destination,
    # which matters once the input is a list of companies rather than one.
    # geo.captcha-delivery.com belongs here rather than under "captcha": naming
    # the vendor says which wall you are up against, which the generic kind
    # does not.
    ("datadome", ("geo.captcha-delivery.com", "/.well-known/captcha/")),
    ("perimeterx", ("/_px/", "px-captcha", "captcha.px-cdn.net")),
    ("imperva", ("_incapsula_resource", "/_imperva_")),
    ("akamai", ("/akam/", "ak-challenge")),
    ("kasada", ("/149e9513-01fa-4fb0-aad4-566afd725d1b/2d206a39",)),
    ("generic_login", ("/login?", "/signin?", "/sign-in?", "/account/login")),
)

TEXT_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("cloudflare", ("just a moment", "checking your browser", "verify you are human",
                    "enable javascript and cookies to continue")),
    ("google_sorry", ("unusual traffic", "our systems have detected")),
    ("captcha", ("i'm not a robot", "complete the security check", "press and hold")),
    ("linkedin_authwall", ("sign in to see", "join linkedin", "sign in to view")),
    ("consent", ("before you continue", "accept all cookies", "we use cookies")),
    ("blocked", ("access denied", "you have been blocked", "request blocked",
                 "unusual activity from your", "automated access")),
    # Content gates rather than bot gates. At company-research scale these are
    # far more common than any anti-bot wall: trade press, analyst notes and
    # professional directories all use them.
    ("paywall", ("subscribe to continue", "subscribers only", "this article is for",
                 "become a member to read", "create a free account to continue",
                 "register to continue reading", "you've reached your article limit",
                 "continue reading with a subscription")),
    ("login_gate", ("log in to continue", "sign up to continue",
                    "create an account to view", "sign in to view this")),
)

# Kinds whose textual signs can appear over content that is nonetheless
# readable. Cloudflare and captcha are excluded: those interstitials replace the
# page, so their text appearing alongside a full page body means the phrase was
# in the content, not that we were challenged.
SOFT_SIGNS = frozenset({"linkedin_authwall", "consent", "blocked", "paywall",
                        "login_gate", "generic_login"})

STATUS_SIGNS: dict[int, str] = {
    401: "unauthorised",
    403: "forbidden",
    429: "rate_limited",
    451: "unavailable_legal",
    503: "unavailable",
}

# A visible sign-in dialog owns the next interaction even when the document
# underneath it is substantial.  This is intentionally site-neutral: providers
# change markup often, while the actions offered by an authentication gate are
# comparatively stable.
LOGIN_DIALOG_ACTIONS = (
    "sign in with email", "log in with email",
    "continue with google", "continue with apple",
)
LOGIN_DIALOG_ENTRY = ("sign in", "log in")
LOGIN_DIALOG_REGISTRATION = (
    "join now", "sign up", "create an account", "new to ",
)


def detect(page: Page, response: Response | None = None) -> Challenge | None:
    """Return the wall this page is showing, or None if it looks like content."""
    return detect_signs(
        url=page.url, status=response.status if response is not None else None,
        head=_head_text(page), text_length=_text_length(page),
        login_dialog=_visible_login_dialog(page) is not None,
    )


def detect_signs(*, url: str, status: int | None, head: str,
                 text_length: int, login_dialog: bool = False) -> Challenge | None:
    """Apply the native classification to browser-independent observations."""

    if _is_linkedin_host(url) and urlsplit(url).path.rstrip("/") == "/404":
        return Challenge(
            "linkedin_not_found", "url path is '/404/'", url, status)

    # Vendors are inconsistent about case (`_Incapsula_Resource`), so match
    # against a lowered copy while reporting the URL as served.
    lowered_url = url.lower()
    for kind, needles in URL_SIGNS:
        for needle in needles:
            if needle in lowered_url:
                return Challenge(kind, f"url contains {needle!r}", url, status)

    if status is not None and status in STATUS_SIGNS:
        # Cloudflare serves its interstitial as 403; prefer the specific name.
        for kind, needles in TEXT_SIGNS:
            if kind != "cloudflare":
                continue
            for needle in needles:
                if needle in head:
                    return Challenge("cloudflare", f"status {status}, {needle!r}", url, status)
        return Challenge(STATUS_SIGNS[status], f"http {status}", url, status)

    if login_dialog:
        return Challenge(
            "generic_login", "visible dialog offers sign-in", url, status)

    substantial = text_length >= CONTENT_FLOOR
    if _is_linkedin_host(url) and not substantial:
        for needle in LINKEDIN_NOT_FOUND_TEXT:
            if needle in head:
                return Challenge(
                    "linkedin_not_found",
                    f"page text contains {needle!r}", url, status)
    for kind, needles in TEXT_SIGNS:
        for needle in needles:
            if needle not in head:
                continue
            # A prompt sitting on top of a page we can read is not a refusal.
            # URL and status signs above are exempt: those identify a page that
            # replaced the content rather than covering it.
            if substantial and kind in SOFT_SIGNS:
                continue
            return Challenge(kind, f"page text contains {needle!r}", url, status)

    return None


def _visible_login_dialog(page: Page) -> str | None:
    """Return a visible authentication dialog's text, if one is present."""
    try:
        texts = page.locator(
            '[role="dialog"]:visible, dialog:visible').all_inner_texts()
    except Exception:
        return None
    for text in texts:
        if is_login_dialog_text(text):
            return text
    return None


def is_login_dialog_text(text: str) -> bool:
    lowered = text.casefold()
    return any(action in lowered for action in LOGIN_DIALOG_ACTIONS) or (
        any(action in lowered for action in LOGIN_DIALOG_ENTRY)
        and any(action in lowered for action in LOGIN_DIALOG_REGISTRATION)
    )


def _text_length(page: Page) -> int:
    try:
        return len(page.inner_text("body"))
    except Exception:
        return 0


def _head_text(page: Page) -> str:
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    try:
        body = page.inner_text("body")[:BODY_WINDOW]
    except Exception:
        body = ""
    return f"{title}\n{body}".lower()


def is_blocking(challenge: Challenge | None) -> bool:
    """True when the operator must act before the flow can continue.

    Consent banners are noted but not blocking: they overlay content rather than
    replacing it, and dismissing them is part of the flow, not an intervention.

    Also where a challenge is recorded, which makes a predicate write. That is
    a smell, and it is still the right place: `detect` returns a Challenge one
    line before any caller decides what it means, so a record written there
    could not say whether we got past the thing -- it would claim every CAPTCHA
    was handled, including the ones that stopped the run. This function is the
    one that decides, and all fourteen call sites come through it, so the
    outcome is right here and nowhere else has to remember.

    `pressure.record` swallows its own failures, so this stays a predicate
    whose answer cannot change because recording went wrong.

    Only `kind` is required of the argument. The annotation says `Challenge`,
    but callers pass anything that names a kind -- recipes have their own wall
    objects -- and this has always been a predicate over that one attribute.
    Recording must not quietly widen what it demands, so the rest is read
    defensively: a caller that cannot say where it was still gets an answer.
    """
    if challenge is None:
        return False
    blocking = challenge.kind not in {"consent", "google_consent"}
    url = getattr(challenge, "url", "") or ""
    pressure.record(
        kind=challenge.kind,
        outcome="blocked" if blocking else "passed",
        host=host_of(url) if url else "",
        evidence=getattr(challenge, "evidence", "") or "",
    )
    return blocking


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _is_linkedin_host(url: str) -> bool:
    host = host_of(url).split(":", 1)[0]
    return host == "linkedin.com" or host.endswith(".linkedin.com")
