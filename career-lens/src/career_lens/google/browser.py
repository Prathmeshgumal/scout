"""Shared natural Google interaction and unscrolled organic-result parsing."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urldefrag, urljoin, urlsplit

from patchright.sync_api import Error as BrowserError

from ..capture import interaction, overlays, trace
from ..capture.failures import raise_if_browser_gone
from ..capture.urls import UrlError, canonical
from .browser_contract import (
    GOOGLE_HOME as GOOGLE_HOME,
    SEARCH_BOX as SEARCH_BOX,
    RESULT_LINKS as RESULT_LINKS,
    RESULT_HEADING,
    NAV_TIMEOUT_MS as NAV_TIMEOUT_MS,
    RESULT_TIMEOUT_MS as RESULT_TIMEOUT_MS,
    REDIRECT_RESOLUTIONS,
)
from .results_contract import (
    RESULT_LIMIT,
    ResultRow,
    SearchResult,
    deduplicate_results,
    google_internal as _google_internal,
    ranked_results,
    refreshed_result,
)


KEY_MEAN = 0.11
KEY_STDDEV = 0.045
KEY_MIN = 0.045
KEY_MAX = 0.30
SPACE_EXTRA = 0.08
THINK_CHANCE = 0.08
THINK_RANGE = (0.35, 0.9)


@dataclass(frozen=True)
class BrowserEnvironment:
    locale: str | None
    timezone: str | None


def _key_delay(char: str, rng: Any) -> tuple[float, bool]:
    delay = min(max(rng.gauss(KEY_MEAN, KEY_STDDEV), KEY_MIN), KEY_MAX)
    if char == " ":
        delay += SPACE_EXTRA
    thought = rng.random() < THINK_CHANCE
    if thought:
        delay += rng.uniform(*THINK_RANGE)
    return delay, thought


def type_text(page, value: str, *, rng: Any = random) -> int:
    """Type paced text without returning or tracing the potentially secret value."""
    thoughts = 0
    for char in value:
        page.keyboard.type(char)
        delay, thought = _key_delay(char, rng)
        thoughts += thought
        time.sleep(delay)
    return thoughts


def type_query(page, query: str, *, rng: Any = random) -> None:
    """Fill Google's search box naturally; submission remains a separate step."""
    with trace.phase(trace.WAIT, "type discovery query") as span:
        box = page.locator(SEARCH_BOX).first
        interaction.click(page, box, rng=rng)
        page.keyboard.press("ControlOrMeta+a")
        time.sleep(rng.uniform(0.08, 0.2))
        thoughts = type_text(page, query, rng=rng)
        time.sleep(rng.uniform(0.2, 0.6))
        span.detail = (
            f"{len(query)} chars @ ~{KEY_MEAN * 1000:.0f}ms "
            f"· {thoughts} think-pause(s)")


def _submit_query(page, host: str, callback):
    callback(host)
    # Typing and budget checks must not consume the page-loading deadline.
    with page.expect_navigation(
            wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS) as navigation:
        page.keyboard.press("Enter")
    return navigation.value


def search(page, query: str, *, before_submit, rng: Any = random):
    """Navigate naturally and charge immediately before the query submission."""
    if "google." not in (urlsplit(page.url).hostname or ""):
        page.goto(GOOGLE_HOME, wait_until="domcontentloaded",
                  timeout=NAV_TIMEOUT_MS)
        time.sleep(rng.uniform(0.6, 1.4))
    interaction.ensure_pointer_in_viewport(page, rng=rng)
    overlays.dismiss(page)
    host = (urlsplit(page.url).hostname or "www.google.com").lower()
    type_query(page, query, rng=rng)
    response = _submit_query(page, host, before_submit)
    try:
        page.wait_for_selector(RESULT_LINKS, timeout=RESULT_TIMEOUT_MS)
    except Exception:
        pass
    return response


def _snippet(anchor: Any) -> str:
    """Read the container text around one result. The contract trims it."""
    try:
        return anchor.locator(
            "xpath=ancestor::div[@data-hveid][1]").first.inner_text()
    except Exception:
        return ""


def literal_host(result: SearchResult) -> str:
    """The host Google actually offered, not the one we normalised it to.

    `SearchResult.host` is read off `normalised_url`, and `canonical()` folds
    LinkedIn country hosts to `www` so that two spellings of one profile
    deduplicate to one request. That is right for identity and wrong for the
    journal: it reported `www.linkedin.com` for an `in.linkedin.com` href,
    which hides exactly what the country-host handling exists to measure.
    """
    try:
        return (urlsplit(result.url).hostname or "").lower() or result.host
    except ValueError:
        return result.host


def results_detail(results: list[SearchResult]) -> str:
    """What the SERP offered, for the journal.

    Hosts and ranks only. The query is a person's name and the titles and
    snippets repeat it, so they stay out of a record that is kept far longer
    than the run -- the same rule `type_query`'s own detail follows.
    """
    if not results:
        return "no results were readable"
    shown = ", ".join(
        f"{result.rank}:{literal_host(result)}" for result in results[:5])
    more = "" if len(results) <= 5 else f", +{len(results) - 5} more"
    return f"{len(results)} results · {shown}{more}"


def read_results(page, limit: int = RESULT_LIMIT) -> tuple[SearchResult, ...]:
    """Read currently rendered organic results without scrolling or pagination."""
    with trace.phase(trace.WORK, "read results") as span:
        results = _read_results(page, limit)
        span.detail = results_detail(results)
    return results


def _read_results(page, limit: int) -> tuple[SearchResult, ...]:
    """Gather rendered anchors; `results_contract` decides what they mean."""
    anchors = page.locator(RESULT_LINKS)
    try:
        content = page.content()
    except Exception:
        content = ""
    rows: list[ResultRow] = []
    for index in range(anchors.count()):
        anchor = anchors.nth(index)
        click_href = anchor.get_attribute("href") or ""
        try:
            title = " ".join(anchor.locator(RESULT_HEADING).first.inner_text().split())
        except Exception:
            title = ""
        rows.append(ResultRow(
            href=click_href,
            click_href=click_href,
            title=title,
            snippet=_snippet(anchor),
        ))
    return ranked_results(
        rows, page_url=page.url, content=content, limit=limit)


def resolve_search_redirects(page, results: tuple[SearchResult, ...]) -> tuple[SearchResult, ...]:
    """Resolve the leading ranks without navigating to a result's destination.

    The context request client shares Google cookies, but never follows redirects.
    At most `REDIRECT_RESOLUTIONS` single attempts of at most three seconds bound
    extra search latency. Original click links remain in the results and in the
    captured SERP evidence.
    """
    updated = list(results)
    origin = urlsplit(page.url)
    for index, item in enumerate(results[:REDIRECT_RESOLUTIONS]):
        source = urlsplit(item.url)
        if (not item.opaque_redirect or source.path != "/goto"
                or source.scheme != "https" or source.netloc != origin.netloc
                or source.username or source.password
                or not _google_internal(source.hostname or "")):
            continue
        with trace.phase(trace.NET, "google redirect resolution", f"rank {item.rank}") as span:
            response = None
            try:
                response = page.context.request.get(
                    item.url, max_redirects=0, max_retries=0, timeout=3000)
                span.detail = f"rank {item.rank} · unresolved · HTTP {response.status}"
                if response.status in {403, 429}:
                    break
                if response.status not in {301, 302, 303, 307, 308}:
                    continue
                location = response.headers.get("location", "")
                target = urlsplit(urljoin(item.url, location))
                if _google_internal(target.hostname or ""):
                    break  # A Google interstitial is not a provider destination.
                if (not location or target.scheme not in {"http", "https"}
                        or not target.hostname or target.username or target.password):
                    continue
                updated[index] = resolved_result(item, target.geturl())
                span.detail = f"rank {item.rank} · resolved · HTTP {response.status}"
            except BrowserError as error:
                raise_if_browser_gone(page, error, during="Google redirect resolution")
                span.detail = f"rank {item.rank} · unresolved · {type(error).__name__}"
            except ValueError:
                span.detail = f"rank {item.rank} · unresolved · invalid destination"
            finally:
                if response is not None:
                    response.dispose()
    # Different opaque tokens can lead to the same URL, including one already
    # visible elsewhere in the SERP. Keep first occurrence and the contract's
    # dense ranks, without spending additional redirect requests to fill gaps.
    return deduplicate_results(updated)


def _click_target(anchor):
    try:
        title = anchor.locator(RESULT_HEADING).first
        if title.is_visible():
            return title
    except Exception:
        pass
    return anchor


def _result_click_target(page, result: SearchResult):
    anchors = page.locator(RESULT_LINKS)
    click_href = result.click_href
    if result.opaque_redirect:
        current_results = read_results(page)
        current = refreshed_result(result, current_results)
        if current is not None:
            click_href = current.click_href
    target = None
    for index in range(anchors.count()):
        anchor = anchors.nth(index)
        raw_href = anchor.get_attribute("href") or ""
        if click_href is not None:
            if raw_href != click_href:
                continue
            candidate = result.normalised_url
        else:
            try:
                candidate = canonical(raw_href)
            except UrlError:
                continue
        if candidate != result.normalised_url:
            continue
        try:
            if not anchor.is_visible():
                continue
        except Exception:
            continue
        target = _click_target(anchor)
        break
    if target is None:
        raise LookupError(
            f"result no longer on the SERP: {result.normalised_url}")
    return target


def _is_google_serp(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return _google_internal(parts.hostname or "") and parts.path == "/search"


def open_result(page, result: SearchResult, *, rng: Any = random):
    """Dismiss overlays and click one visible result in the current tab."""
    offered = literal_host(result)
    with trace.phase(trace.NET, "open result") as span:
        span.detail = f"rank {result.rank} · {offered}"
        for attempt in range(2):
            overlays.dismiss(page)
            target = _result_click_target(page, result)
            time.sleep(rng.uniform(0.3, 0.9))
            try:
                with page.expect_navigation(
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS) as navigation:
                    interaction.click(page, target, rng=rng)
                landed = _landed_host(page)
                span.detail = (
                    f"rank {result.rank} · {offered}"
                    + (f" -> {landed}" if landed and landed != offered else "")
                    + (" · after a retry" if attempt else ""))
                return navigation.value
            except Exception:
                if attempt or not _is_google_serp(page.url):
                    raise
                # Journalled here rather than after the fact: the retry is a
                # second click on a page Google re-rendered under us, and a
                # run that took two clicks looks identical to one that took
                # one unless this line exists.
                span.detail = (f"rank {result.rank} · {offered} · "
                               "click intercepted, retrying")
        raise RuntimeError("Google result click retry was exhausted")


def _landed_host(page) -> str:
    try:
        return (urlsplit(page.url).hostname or "").lower()
    except Exception:
        return ""


def resolved_result(result: SearchResult, final_url: str) -> SearchResult:
    """Replace an opaque Google click URL with its observed destination."""
    normalised = canonical(final_url)
    return SearchResult(
        rank=result.rank,
        url=final_url,
        normalised_url=normalised,
        host=(urlsplit(normalised).hostname or "").lower(),
        title=result.title,
        snippet=result.snippet,
        click_href=result.click_href,
        opaque_redirect=result.opaque_redirect,
    )


def _same_serp(first: str, second: str) -> bool:
    try:
        left = urlsplit(urldefrag(first).url)
        right = urlsplit(urldefrag(second).url)
    except ValueError:
        return False
    if not (_is_google_serp(first) and _is_google_serp(second)):
        return False
    left_query = parse_qs(left.query).get("q")
    right_query = parse_qs(right.query).get("q")
    return bool(left_query and left_query == right_query)


def return_to_serp(page, serp_url: str, *, max_hops: int = 2) -> bool:
    if _same_serp(page.url, serp_url):
        return True
    for _ in range(max_hops):
        try:
            page.go_back(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        except TypeError:  # narrow fake/adapter compatibility
            try:
                page.go_back()
            except Exception:
                break
        except Exception:
            if _same_serp(page.url, serp_url):
                return True
            break
        if _same_serp(page.url, serp_url):
            return True
    try:
        page.goto(
            serp_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception:
        return False
    return _same_serp(page.url, serp_url)


def environment(page) -> BrowserEnvironment:
    try:
        value = page.evaluate("""
            () => ({
              locale: navigator.language || null,
              timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || null
            })
        """)
    except Exception:
        value = {}
    return BrowserEnvironment(
        locale=value.get("locale") if isinstance(value, dict) else None,
        timezone=value.get("timezone") if isinstance(value, dict) else None,
    )
