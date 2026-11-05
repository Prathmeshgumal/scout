"""Redaction of credential-bearing values.

A HAR exported "with sensitive data" carries live session cookies and OAuth bearer
tokens. Rather than dropping those values (which would make sessions
indistinguishable) we replace them with a stable fingerprint, so you can still tell
whether a value *changed* between captures without holding the secret itself.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

# Hosts whose URL *path* embeds an account identifier rather than a resource name.
# A profile avatar URL is publicly fetchable and identifies the account, so the
# opaque segment has to go even though nothing about it looks like a credential.
IDENTITY_URL_HOSTS = re.compile(r"(googleusercontent\.com|lh\d+\.google\.com)$")

# Below this length a path segment is a route ("a", "u", "rd-ogw"), not an ID.
MIN_OPAQUE_SEGMENT = 16

# Header names whose values are credentials or stable device identifiers.
SECRET_HEADERS: frozenset[str] = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-client-data",
        "x-browser-validation",
        "x-goog-api-key",
        "x-goog-visitor-id",
        "x-youtube-identity-token",
        "x-sg-e",
        # LinkedIn's CSRF token is not a separate secret: its value *is* the
        # JSESSIONID cookie, so a HAR that fingerprints `cookie` and leaves
        # this alone still carries the live session credential in plaintext.
        # Observed on every authenticated Voyager and rsc-action request.
        "csrf-token",
        # Identifiers that tie a capture to this signed-in account or browser
        # instance and persist across requests. Fingerprinted rather than
        # dropped, so "did it rotate between captures?" stays answerable.
        "x-li-application-instance",
        "x-li-pageforestid",
        "x-li-page-instance",
        "x-li-page-instance-tracking-id",
        "x-li-realtime-session",
        "x-li-traceparent",
        "x-li-tracestate",
    }
)

# Deliberately NOT secret: `x-li-track` carries clientVersion, osName,
# deviceFormFactor and display metrics. That is the fingerprint surface this
# log exists to record, and it names no account.

# Every cookie value is redacted, not just session credentials. An earlier
# prefix-allowlist leaked persistent tracking identifiers (Google Analytics client
# IDs, visitor IDs) that are just as identifying as a session token. No cookie
# value's plaintext is needed for analysis: the name carries the meaning, and the
# fingerprint still reveals whether the value rotated.


def fingerprint(value: str) -> str:
    """Stable, non-reversible stand-in for a secret.

    Same input always yields the same output, so a value that is constant across
    captures stays visibly constant, and a rotated one visibly changes.
    """
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:12]
    return f"sha256:{digest}:len={len(value)}"


# Query parameters that carry credentials. Google's /log endpoints put a
# SAPISIDHASH auth token straight into the query string, so a leak can bypass
# header-based redaction entirely -- it lands in request.url, in the query list,
# and in the HTTP/2 :path pseudo-header.
SECRET_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "auth",
        "access_token",
        "id_token",
        "refresh_token",
        "oauth_token",
        "token",
        "apikey",
        "api_key",
        "key",
        "sig",
        "signature",
        "secret",
        "password",
        "passwd",
        "pwd",
        "session",
        "sessionid",
        "credential",
    }
)

# Substrings that mark a value as a credential regardless of the parameter name.
SECRET_VALUE_MARKERS: tuple[str, ...] = (
    "sapisidhash",
    "sapisid1phash",
    "sapisid3phash",
    "ya29.",
    "bearer ",
)


def is_secret_query(name: str, value: str) -> bool:
    if name.lower() in SECRET_QUERY_PARAMS:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in SECRET_VALUE_MARKERS)


def redact_query_string(query: str, *, enabled: bool = True) -> str:
    """Fingerprint credential-bearing query parameters, preserving order and names."""
    if not enabled or not query:
        return query
    out = []
    for pair in query.split("&"):
        if not pair:
            continue
        name, sep, value = pair.partition("=")
        if sep and is_secret_query(unquote_plus(name), unquote_plus(value)):
            out.append(f"{name}={fingerprint(unquote_plus(value))}")
        else:
            out.append(pair)
    return "&".join(out)


# Headers whose value is itself a URL, and so can carry the identifiers that
# redact_url() strips from request URLs.
URL_VALUED_HEADERS: frozenset[str] = frozenset(
    {"location", "content-location", "referer", "refresh"}
)


def is_secret_header(name: str) -> bool:
    return name.lower() in SECRET_HEADERS


def redact_header(name: str, value: str, *, enabled: bool = True) -> str:
    if not enabled:
        return value
    if is_secret_header(name):
        return fingerprint(value)
    if name.lower() in URL_VALUED_HEADERS:
        return redact_url(value)
    return value


def redact_url(url: str, *, enabled: bool = True) -> str:
    """Fingerprint opaque path segments on identity-bearing hosts.

    Host, route structure and query string are preserved, so the request stays
    recognisable; only the segment that names the account is replaced.
    """
    if not enabled:
        return url
    parts = urlsplit(url)
    query = redact_query_string(parts.query)
    path = parts.path
    if IDENTITY_URL_HOSTS.search(parts.netloc):
        path = "/".join(
            fingerprint(segment) if len(segment) >= MIN_OPAQUE_SEGMENT else segment
            for segment in parts.path.split("/")
        )
    if path == parts.path and query == parts.query:
        return url
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))
