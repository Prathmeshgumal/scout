"""What a URL is, and what we act on.

Normalisation decides when two requests are the same request, so it is what the
outstanding-work unique index keys on. It is deliberately conservative: it
lowercases the host, drops the fragment, strips tracking parameters and
normalises the trailing slash, and does nothing else. Anything cleverer risks
two genuinely different pages colliding, and a collision here means one of them
is never scraped.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit


PROFILE = re.compile(r"^/in/([^/]+)/?$")
COMPANY = re.compile(r"^/(company|school|showcase)/([^/]+)/?$")

LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com")

# Query parameters that identify where a link was clicked rather than what it
# points at. Two URLs differing only in these are the same page.
TRACKING = ("trk", "trackingId", "originalSubdomain", "lipi", "licu",
            "midToken", "midSig", "eBP", "refId", "original_referer")


class UrlError(ValueError):
    """The URL cannot be acted on, and the message says why."""


def canonical(url: str) -> str:
    """The canonical form of `url`, or UrlError.

    Named `canonical` rather than `normalise` because `capture/naming.py`
    already has a `normalise` that does something else entirely -- it adds a
    missing scheme so a pasted `example.com/x` becomes a URL. Two functions
    called normalise in one package, doing different jobs, is a bug waiting for
    whoever imports the wrong one.
    """
    raw = (url or "").strip()
    if not raw:
        raise UrlError("no URL was given")
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise UrlError(f"not a URL: {exc}") from exc
    if parts.scheme not in ("http", "https"):
        raise UrlError(
            f"only http and https can be captured, not {parts.scheme or 'a bare path'}")
    if not parts.netloc:
        raise UrlError("the URL names no host")

    host = parts.netloc.lower()
    # The port is kept when it is not the default: a different port is a
    # different service, whatever the host says.
    if host.endswith(":443") and parts.scheme == "https":
        host = host[:-4]
    if host.endswith(":80") and parts.scheme == "http":
        host = host[:-3]

    keep = [pair for pair in parts.query.split("&")
            if pair and pair.split("=", 1)[0] not in TRACKING]
    path = parts.path or "/"
    if _is_linkedin(host):
        # LinkedIn is inconsistent about the trailing slash and serves the same
        # page either way, so one spelling is chosen. Only for LinkedIn: on an
        # arbitrary site /a and /a/ can be different documents.
        path = path if path.endswith("/") else path + "/"
        # Country-host roots identify the same LinkedIn entity for request
        # deduplication. This changes only the stored request identity: Google
        # discovery deliberately keeps the browser on the country host it
        # clicked, and the capture records that actual landed host.
        if PROFILE.match(path) or COMPANY.match(path):
            _, separator, port = host.rpartition(":")
            host = "www.linkedin.com" + (f":{port}" if separator else "")
    return urlunsplit((parts.scheme, host, path, "&".join(keep), ""))


def _is_linkedin(host: str) -> bool:
    bare = host.split(":", 1)[0]
    return bare in LINKEDIN_HOSTS or bare.endswith(".linkedin.com")


def classify(url: str) -> str:
    """Which of `core.KINDS` this URL is. Takes a normalised URL."""
    parts = urlsplit(url)
    if not _is_linkedin(parts.netloc):
        # The rest of the web: one page, no recipe.
        return "generic"
    if PROFILE.match(parts.path):
        return "linkedin_person"
    if COMPANY.match(parts.path):
        return "linkedin_company"
    # A LinkedIn URL that is neither a profile nor an organisation -- a feed, a
    # job posting, a search. Captured as a page, with no recipe.
    return "generic"
