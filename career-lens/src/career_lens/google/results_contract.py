"""Browser-independent contract for reading Google organic results.

This module deliberately has no browser, database, API, or Pydantic imports.
Both runtimes gather the same neutral rows -- a native worker from Playwright
locators, a caller from a typed `read` collection -- and resolve them
here, so one definition of rank, dedupe, and redirect handling serves both.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from urllib.parse import parse_qs, unquote, urldefrag, urljoin, urlsplit

from ..capture.urls import UrlError, canonical

#: The deepest Google ranking either runtime reads from one unscrolled page.
RESULT_LIMIT = 60

#: Google hides most destinations behind this prefix. The id after `url=` is
#: usually opaque, so the destination is recovered from the document rather
#: than from the href itself.
GOTO_PREFIX = "/goto?url="

#: How much document text around an href can hold its destination. The
#: extension's `document_context` read field slices exactly this much, so the
#: server must not assume it can scan further.
CONTEXT_WINDOW = 4_096

#: Text Google renders when an empty result set is deliberate rather than a
#: selector/parser failure. Both browser runtimes use this exact vocabulary.
NO_RESULTS_TEXT = (
    "did not match any documents",
    "no results found for",
    "try different keywords",
)


def reports_no_results(text: str) -> bool:
    return any(marker in text.casefold() for marker in NO_RESULTS_TEXT)


@dataclass(frozen=True)
class SearchResult:
    """One organic result, ranked and resolved."""

    rank: int
    url: str
    normalised_url: str
    host: str
    title: str
    snippet: str = ""
    click_href: str | None = None
    opaque_redirect: bool = False


@dataclass(frozen=True)
class ResultRow:
    """One rendered organic anchor, gathered by either runtime.

    `document_context` is the slice of document HTML that begins at this row's
    escaped href. It is the only place an opaque destination can be read from,
    so a row without it can only be resolved when its href is already a URL.
    """

    href: str
    click_href: str
    title: str
    snippet: str
    document_context: str = ""


def deduplicate_results(results: Sequence[SearchResult]) -> tuple[SearchResult, ...]:
    """Keep first destination occurrence and dense ranks after resolution."""
    unique: list[SearchResult] = []
    seen: set[str] = set()
    for item in results:
        if item.normalised_url not in seen:
            seen.add(item.normalised_url)
            unique.append(replace(item, rank=len(unique) + 1))
    return tuple(unique)


def refreshed_result(
    original: SearchResult, current: Sequence[SearchResult],
) -> SearchResult | None:
    """Identify a re-rendered result without trusting a rotated redirect token."""
    exact = next((item for item in current if (
        item.click_href == original.click_href if original.opaque_redirect
        else item.normalised_url == original.normalised_url
    )), None)
    if exact is not None or not original.opaque_redirect:
        return exact
    matches = [item for item in current
               if item.title == original.title and item.snippet == original.snippet]
    if len(matches) == 1:
        return matches[0]
    return next((item for item in current
                 if item.rank == original.rank and item.title == original.title), None)


def google_internal(host: str) -> bool:
    bare = host.split(":", 1)[0].casefold().removeprefix("www.")
    return bare.startswith("google.") or ".google." in bare


def opaque_destination(content: str, click_href: str) -> str | None:
    """Recover the destination Google encoded beside an opaque click href."""
    escaped_href = click_href.replace("=", r"\u003d")
    start = content.find(escaped_href)
    match = re.search(
        re.escape(f'"{escaped_href}"')
        + r'\s*\],\s*\[\s*"(https?://(?:\\.|[^"\\])*)"',
        content,
    )
    candidates = ([match.group(1)] if match is not None else [])
    if not candidates and start >= 0:
        candidates = re.findall(
            r'"(https?://(?:\\.|[^"\\])*)"',
            content[start:start + CONTEXT_WINDOW],
        )
    for candidate in candidates:
        try:
            value = json.loads(f'"{candidate}"')
            normalised = canonical(value)
        except (json.JSONDecodeError, UrlError):
            continue
        if not google_internal((urlsplit(normalised).hostname or "").lower()):
            return value
    return None


def resolved_destination(
    click_href: str,
    *,
    content: str = "",
    page_url: str,
) -> str | None:
    """Return the address behind one click href, or None when unreadable."""
    try:
        parsed = urlsplit(urljoin(page_url, click_href))
    except ValueError:
        return None
    if google_internal(parsed.hostname or "") and parsed.path == "/goto":
        values = parse_qs(parsed.query).get("url", [])
        destination = unquote(values[0]) if values else ""
        href = (
            destination
            if destination.casefold().startswith(("http://", "https://"))
            else opaque_destination(content, click_href) or ""
        )
        try:
            parsed = urlsplit(href)
        except ValueError:
            return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urldefrag(parsed.geturl()).url


def _snippet(value: str, title: str) -> str:
    if title:
        value = value.replace(title, "", 1)
    return " ".join(part.strip() for part in value.splitlines() if part.strip())


def ranked_results(
    rows: Sequence[ResultRow],
    *,
    page_url: str,
    content: str = "",
    limit: int = RESULT_LIMIT,
) -> tuple[SearchResult, ...]:
    """Rank, deduplicate, and resolve rendered organic rows.

    `rank` counts surviving results, not examined rows: a duplicate or an
    unreadable destination is skipped without spending a rank, because rank is
    what a caller means by "the third result".
    """
    maximum = min(max(limit, 0), RESULT_LIMIT)
    results: list[SearchResult] = []
    seen: set[str] = set()
    for row in rows:
        if len(results) >= maximum:
            break
        click_href = row.click_href or row.href
        opaque_redirect = click_href.startswith(GOTO_PREFIX)
        original = resolved_destination(
            click_href,
            content=row.document_context or content,
            page_url=page_url,
        )
        if original is None:
            # An opaque token whose destination cannot be read is still a real
            # result: Google rotates these between reads, and the recovery path
            # re-reads the page and matches rows by identity. Dropping it here
            # would make a rotated token look like a result that vanished.
            if not opaque_redirect:
                continue
            original = urljoin(page_url, click_href)
        try:
            normalised = canonical(original)
        except UrlError:
            continue
        host = (urlsplit(normalised).hostname or "").lower()
        if (not opaque_redirect and google_internal(host)) or normalised in seen:
            continue
        seen.add(normalised)
        results.append(SearchResult(
            rank=len(results) + 1,
            url=original,
            normalised_url=normalised,
            host=host,
            title=row.title,
            snippet=_snippet(row.snippet, row.title),
            click_href=click_href if opaque_redirect else None,
            opaque_redirect=opaque_redirect,
        ))
    return tuple(results)


__all__ = [
    "CONTEXT_WINDOW",
    "GOTO_PREFIX",
    "NO_RESULTS_TEXT",
    "RESULT_LIMIT",
    "ResultRow",
    "SearchResult",
    "google_internal",
    "opaque_destination",
    "ranked_results",
    "reports_no_results",
    "resolved_destination",
]
