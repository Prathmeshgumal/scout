"""The stable, storage-safe Google search JSON 1.0 contract.

This module deliberately has no API, database, browser, or Pydantic imports.
Workers validate the exact same document that the public API later serves.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit


SCHEMA_VERSION = "1.0"
DEFAULT_TOP_N = 5
#: The most destinations a caller may ask one search to open.
MAX_TOP_N = 5
#: The most a *stored* document may say was asked for. The request ceiling came
#: down from 20 to 5, and documents are re-validated every time they are read:
#: holding stored documents to today's ceiling would make every search run
#: under the old one unreadable, which is not what lowering a limit means.
MAX_STORED_TOP_N = 20
MAX_RESULTS = 60

OUTCOMES = ("ok", "partial", "failed", "cancelled")
CAPTURE_STATUSES = ("succeeded", "partial", "failed")
CAPTURE_MODES = ("generic", "linkedin_shallow")
SELECTION_REASONS = (
    "top_n", "beyond_top_n", "linkedin_unavailable", "excluded_host")
RESULT_FAILURE_KINDS = (
    "blocked",
    "login_required",
    "not_found",
    "timeout",
    "browser_error",
    "storage_error",
    "capture_error",
    "search_budget",
)
REQUEST_FAILURE_KINDS = (
    "blocked",
    "login_required",
    "not_found",
    "timeout",
    "browser_error",
    "extract_failed",
    "parser_drift",
    "worker_lost",
    "account_mismatch",
    "storage_error",
)

_TOP_LEVEL_KEYS = {
    "schema_version", "request", "search", "outcome", "failure", "serp",
    "results", "summary",
}
_REQUEST_KEYS = {
    "request_id", "status", "requested_at", "started_at", "finished_at",
}
_SEARCH_KEYS = {
    "query", "normalised_query", "requested_top_n", "results_examined",
    "google_host", "serp_url", "browser_locale", "browser_timezone",
    "searched_at",
}
_SERP_KEYS = {
    "capture_id", "status", "url_final", "title", "text",
    "text_characters", "completeness",
}
_RESULT_KEYS = {
    "rank", "url", "normalised_url", "host", "title", "snippet",
    "selection", "capture",
}
_SELECTION_KEYS = {"selected", "reason"}
_CAPTURE_KEYS = {
    "capture_id", "mode", "status", "url_requested", "url_final", "host",
    "http_status", "title", "captured_at", "text", "text_characters",
    "completeness", "error",
}
_COMPLETENESS_KEYS = {"scroll_stopped", "scroll_growing"}
_ERROR_KEYS = {"kind", "detail"}
_SUMMARY_KEYS = {
    "examined", "selected", "captured", "partial", "failed", "skipped",
}


class InvalidGoogleRequest(ValueError):
    """The caller supplied an unusable query or result count."""


class InvalidGoogleDocument(ValueError):
    """A document does not satisfy the canonical JSON 1.0 contract."""


def normalise_query(value: object) -> tuple[str, str]:
    """Return the display query and its stable outstanding-work identity."""
    if not isinstance(value, str):
        raise InvalidGoogleRequest("query must be text")
    display = " ".join(unicodedata.normalize("NFKC", value).split())
    if not 1 <= len(display) <= 500:
        raise InvalidGoogleRequest("query must contain 1 through 500 codepoints")
    return display, display.casefold()


def normalise_requested_query(value: object) -> tuple[str, str]:
    """Normalise a new query, refusing a target meant for Generic URL.

    Stored documents continue to use ``normalise_query`` directly. A URL
    accepted before this request rule existed therefore remains readable.
    """
    display, identity = normalise_query(value)
    try:
        parsed = urlsplit(display)
    except ValueError:
        parsed = None
    if (parsed is not None
            and parsed.scheme.casefold() in ("http", "https")
            and parsed.netloc
            and not any(character.isspace() for character in display)):
        raise InvalidGoogleRequest(
            f"{display} is a URL, but it was posted to the Google search "
            "endpoint; use the Generic URL recipe instead")
    return display, identity


def normalise_top_n(value: object = None) -> int:
    """Validate the requested destination count without coercing callers."""
    if value is None:
        return DEFAULT_TOP_N
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidGoogleRequest("top_n must be an integer")
    if not 0 <= value <= MAX_TOP_N:
        raise InvalidGoogleRequest(f"top_n must be between 0 and {MAX_TOP_N}")
    return value


def normalise_lookup_top_n(value: object) -> int:
    """The same count read as a key rather than as a request.

    A new search may ask for at most `MAX_TOP_N` destinations. A search that
    ran before that ceiling came down is still stored, and reading it back
    means naming the count it ran at -- so a lookup accepts anything a stored
    document may hold. It grants nothing: no work is dispatched by naming a
    number here, and a count with no stored search behind it finds nothing.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidGoogleRequest("top_n must be an integer")
    if not 0 <= value <= MAX_STORED_TOP_N:
        raise InvalidGoogleRequest(
            f"top_n must be between 0 and {MAX_STORED_TOP_N}")
    return value


def summary(results: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Derive the public aggregate counts from result entries."""
    captures = [result.get("capture") for result in results]
    selected = sum(bool(result["selection"]["selected"]) for result in results)
    return {
        "examined": len(results),
        "selected": selected,
        "captured": sum(
            capture is not None and capture.get("status") == "succeeded"
            for capture in captures
        ),
        "partial": sum(
            capture is not None and capture.get("status") == "partial"
            for capture in captures
        ),
        "failed": sum(
            capture is not None and capture.get("status") == "failed"
            for capture in captures
        ),
        "skipped": sum(capture is None for capture in captures),
    }


def validate_document(value: object) -> dict[str, Any]:
    """Validate and return the original canonical document unchanged."""
    document = _mapping(value, "document")
    _reject_embedded_artifacts(document)
    _keys(document, _TOP_LEVEL_KEYS, "top-level document")
    if document["schema_version"] != SCHEMA_VERSION:
        _invalid(f"schema_version must be {SCHEMA_VERSION!r}")

    request = _mapping(document["request"], "request")
    _validate_request(request)
    search = _mapping(document["search"], "search")
    _validate_search(search)

    outcome = document["outcome"]
    if outcome not in OUTCOMES:
        _invalid(f"outcome must be one of {OUTCOMES}")
    failure = _validate_error(
        document["failure"], "failure", REQUEST_FAILURE_KINDS, nullable=True)

    results_value = document["results"]
    if not isinstance(results_value, list):
        _invalid("results must be an array")
    if len(results_value) > MAX_RESULTS:
        _invalid(f"results must contain no more than {MAX_RESULTS} entries")
    results: list[Mapping[str, Any]] = []
    normalised_urls: set[str] = set()
    for expected_rank, raw_result in enumerate(results_value, 1):
        result = _mapping(raw_result, f"result {expected_rank}")
        _validate_result(result, expected_rank)
        normalised_url = result["normalised_url"]
        if normalised_url in normalised_urls:
            _invalid("result normalised_url values must be unique")
        normalised_urls.add(normalised_url)
        results.append(result)

    if search["results_examined"] != len(results):
        _invalid("search.results_examined must equal the number of results")
    if sum(result["selection"]["selected"] for result in results) > search[
            "requested_top_n"]:
        _invalid("result selection exceeds search.requested_top_n")

    serp_value = document["serp"]
    serp = None if serp_value is None else _mapping(serp_value, "serp")
    if serp is not None:
        _validate_serp(serp)
        if not search["google_host"] or not search["serp_url"]:
            _invalid("a captured serp requires its Google host and URL")
        if search["searched_at"] is None:
            _invalid("a captured serp requires search.searched_at")

    stored_summary = _mapping(document["summary"], "summary")
    _keys(stored_summary, _SUMMARY_KEYS, "summary")
    for key, count in stored_summary.items():
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _invalid(f"summary.{key} must be a non-negative integer")
    if stored_summary != summary(results):
        _invalid("summary does not match result entries")

    _validate_terminal_semantics(
        request=request,
        outcome=outcome,
        failure=failure,
        serp=serp,
        stored_summary=stored_summary,
    )
    return document


def failed_envelope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the canonical response for a terminal failure without evidence."""
    query = _first(request, "query", "query_requested")
    normalised = _first(request, "normalised_query", "query_normalised")
    top_n = _first(request, "top_n", "google_top_n")
    failure_kind = _first(request, "failure_kind", "cause")
    failure_detail = _first(request, "failure_detail", "cause_detail")
    document = {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "request_id": _first(request, "request_id", "id"),
            "status": "failed",
            "requested_at": _timestamp_value(request.get("requested_at")),
            "started_at": _timestamp_value(request.get("started_at")),
            "finished_at": _timestamp_value(request.get("finished_at")),
        },
        "search": {
            "query": query,
            "normalised_query": normalised,
            "requested_top_n": top_n,
            "results_examined": 0,
            "google_host": None,
            "serp_url": None,
            "browser_locale": None,
            "browser_timezone": None,
            "searched_at": None,
        },
        "outcome": "failed",
        "failure": {"kind": failure_kind, "detail": failure_detail},
        "serp": None,
        "results": [],
        "summary": summary([]),
    }
    return validate_document(document)


def cancelled_envelope(request: Mapping[str, Any]) -> dict[str, Any]:
    """Build the canonical response for a cancellation without evidence."""
    query = _first(request, "query", "query_requested")
    normalised = _first(request, "normalised_query", "query_normalised")
    top_n = _first(request, "top_n", "google_top_n")
    document = {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "request_id": _first(request, "request_id", "id"),
            "status": "cancelled",
            "requested_at": _timestamp_value(request.get("requested_at")),
            "started_at": _timestamp_value(request.get("started_at")),
            "finished_at": _timestamp_value(_first(
                request, "finished_at", "cancel_requested_at", "requested_at")),
        },
        "search": {
            "query": query,
            "normalised_query": normalised,
            "requested_top_n": top_n,
            "results_examined": 0,
            "google_host": None,
            "serp_url": None,
            "browser_locale": None,
            "browser_timezone": None,
            "searched_at": None,
        },
        "outcome": "cancelled",
        "failure": None,
        "serp": None,
        "results": [],
        "summary": summary([]),
    }
    return validate_document(document)


def _validate_request(request: Mapping[str, Any]) -> None:
    _keys(request, _REQUEST_KEYS, "request")
    _nonempty_text(request["request_id"], "request.request_id")
    if request["status"] not in ("succeeded", "failed", "cancelled"):
        _invalid("request.status must be terminal")
    _timestamp(request["requested_at"], "request.requested_at")
    _timestamp(request["started_at"], "request.started_at", nullable=True)
    _timestamp(request["finished_at"], "request.finished_at")


def _validate_search(search: Mapping[str, Any]) -> None:
    _keys(search, _SEARCH_KEYS, "search")
    try:
        display, identity = normalise_query(search["query"])
    except InvalidGoogleRequest as error:
        _invalid(f"invalid search: {error}")
    if display != search["query"] or identity != search["normalised_query"]:
        _invalid("search query and normalised_query do not agree")
    # Against the stored ceiling rather than the request one: what a caller may
    # ask for today does not decide whether yesterday's document is readable.
    top_n = search["requested_top_n"]
    if (isinstance(top_n, bool) or not isinstance(top_n, int)
            or not 0 <= top_n <= MAX_STORED_TOP_N):
        _invalid("search.requested_top_n is invalid")
    examined = search["results_examined"]
    if isinstance(examined, bool) or not isinstance(examined, int):
        _invalid("search.results_examined must be an integer")
    if not 0 <= examined <= MAX_RESULTS:
        _invalid(f"search.results_examined must be between 0 and {MAX_RESULTS}")
    _optional_text(search["google_host"], "search.google_host")
    serp_url = search["serp_url"]
    if serp_url is not None:
        _http_url(serp_url, "search.serp_url")
    _optional_text(search["browser_locale"], "search.browser_locale")
    _optional_text(search["browser_timezone"], "search.browser_timezone")
    _timestamp(search["searched_at"], "search.searched_at", nullable=True)


def _validate_serp(serp: Mapping[str, Any]) -> None:
    _keys(serp, _SERP_KEYS, "serp")
    _nonempty_text(serp["capture_id"], "serp.capture_id")
    if serp["status"] not in ("succeeded", "partial"):
        _invalid("serp.status must be succeeded or partial")
    _http_url(serp["url_final"], "serp.url_final")
    _optional_text(serp["title"], "serp.title")
    _text_and_count(serp["text"], serp["text_characters"], "serp")
    completeness = _mapping(serp["completeness"], "serp.completeness")
    _validate_completeness(completeness, "serp.completeness")
    if completeness["scroll_stopped"] != "not_scrolled":
        _invalid("serp completeness must report scroll_stopped='not_scrolled'")
    if completeness["scroll_growing"] is not False:
        _invalid("serp completeness must report scroll_growing=false")


def _validate_result(result: Mapping[str, Any], expected_rank: int) -> None:
    _keys(result, _RESULT_KEYS, f"result {expected_rank}")
    rank = result["rank"]
    if isinstance(rank, bool) or not isinstance(rank, int) or rank != expected_rank:
        _invalid("result ranks must be dense and one-based")
    _http_url(result["url"], f"result {rank}.url")
    _http_url(result["normalised_url"], f"result {rank}.normalised_url")
    _nonempty_text(result["host"], f"result {rank}.host")
    _text(result["title"], f"result {rank}.title")
    _text(result["snippet"], f"result {rank}.snippet")

    selection = _mapping(result["selection"], f"result {rank}.selection")
    _keys(selection, _SELECTION_KEYS, f"result {rank}.selection")
    if not isinstance(selection["selected"], bool):
        _invalid(f"result {rank} selection.selected must be Boolean")
    if selection["reason"] not in SELECTION_REASONS:
        _invalid(f"result {rank} selection reason is invalid")
    should_select = selection["reason"] == "top_n"
    if selection["selected"] is not should_select:
        _invalid(f"result {rank} selection reason and selected flag disagree")

    capture_value = result["capture"]
    if not selection["selected"]:
        if capture_value is not None:
            _invalid(f"unselected result {rank} cannot have a capture")
        return
    if capture_value is None:
        _invalid(f"selected result {rank} requires a capture envelope")
    _validate_capture(_mapping(capture_value, f"result {rank}.capture"), rank)


def _validate_capture(capture: Mapping[str, Any], rank: int) -> None:
    label = f"result {rank}.capture"
    _keys(capture, _CAPTURE_KEYS, label)
    if capture["mode"] not in CAPTURE_MODES:
        _invalid(f"{label}.mode is invalid")
    status = capture["status"]
    if status not in CAPTURE_STATUSES:
        _invalid(f"{label}.status is invalid")
    capture_id = capture["capture_id"]
    if capture_id is not None:
        _nonempty_text(capture_id, f"{label}.capture_id")
    _http_url(capture["url_requested"], f"{label}.url_requested")
    if capture["url_final"] is not None:
        _http_url(capture["url_final"], f"{label}.url_final")
    _nonempty_text(capture["host"], f"{label}.host")
    # LinkedIn uses non-standard 999 responses. Keep the observed evidence;
    # a blocked destination must not invalidate other captured results.
    http_status = capture["http_status"]
    if http_status is not None and (
            isinstance(http_status, bool) or not isinstance(http_status, int)
            or not (100 <= http_status <= 599 or http_status == 999)):
        _invalid(f"{label}.http_status must be an HTTP status or null")
    _optional_text(capture["title"], f"{label}.title")
    _timestamp(capture["captured_at"], f"{label}.captured_at", nullable=True)
    completeness = _mapping(capture["completeness"], f"{label}.completeness")
    _validate_completeness(completeness, f"{label}.completeness")
    error = _validate_error(
        capture["error"], f"{label}.error", RESULT_FAILURE_KINDS,
        nullable=True,
    )

    text = capture["text"]
    text_characters = capture["text_characters"]
    if status in ("succeeded", "partial"):
        if capture_id is None:
            _invalid(f"{label}.capture_id is required for status {status}")
        if capture["url_final"] is None or capture["captured_at"] is None:
            _invalid(f"{label} durable capture facts are incomplete")
        _text_and_count(text, text_characters, label)
        if status == "succeeded" and error is not None:
            _invalid(f"{label}.error must be null when status is succeeded")
        if status == "partial" and error is None:
            _invalid(f"{label}.error is required when status is partial")
    else:
        if error is None:
            _invalid(f"{label}.error is required when status is failed")
        if text is None:
            if text_characters != 0:
                _invalid(f"{label}.text_characters must be zero without text")
        else:
            _text_and_count(text, text_characters, label)


def _validate_completeness(value: Mapping[str, Any], label: str) -> None:
    _keys(value, _COMPLETENESS_KEYS, label)
    _nonempty_text(value["scroll_stopped"], f"{label}.scroll_stopped")
    if not isinstance(value["scroll_growing"], bool):
        _invalid(f"{label}.scroll_growing must be Boolean")


def _validate_error(
    value: object,
    label: str,
    kinds: Sequence[str],
    *,
    nullable: bool,
) -> Mapping[str, Any] | None:
    if value is None:
        if nullable:
            return None
        _invalid(f"{label} is required")
    error = _mapping(value, label)
    _keys(error, _ERROR_KEYS, label)
    if error["kind"] not in kinds:
        _invalid(f"{label} uses an invalid failure kind")
    _text(error["detail"], f"{label}.detail")
    return error


def _validate_terminal_semantics(
    *,
    request: Mapping[str, Any],
    outcome: str,
    failure: Mapping[str, Any] | None,
    serp: Mapping[str, Any] | None,
    stored_summary: Mapping[str, Any],
) -> None:
    failed_request = request["status"] == "failed"
    cancelled_request = request["status"] == "cancelled"
    if failed_request != (failure is not None):
        _invalid("request terminal status and failure must agree")
    if cancelled_request:
        if outcome != "cancelled" or failure is not None or serp is not None:
            _invalid("cancelled request requires an empty cancelled outcome")
        return
    if outcome == "cancelled":
        _invalid("outcome cancelled requires a cancelled request")
    if outcome == "ok":
        if failed_request or serp is None or (
                stored_summary["partial"] or stored_summary["failed"]):
            _invalid("outcome ok requires successful captured work")
    elif outcome == "partial":
        incomplete = (
            failed_request or stored_summary["partial"] > 0
            or stored_summary["failed"] > 0
        )
        if serp is None or not incomplete:
            _invalid("outcome partial requires useful but incomplete evidence")
    elif not failed_request:
        _invalid("outcome failed requires a failed request and failure")


def _reject_embedded_artifacts(value: object, path: str = "document") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if isinstance(raw_key, str):
                key = raw_key.casefold().replace("-", "_")
                forbidden = (
                    "markdown" in key or key in {"html", "raw_html", "page_html"}
                    or "screenshot" in key or "base64" in key
                    or key in {"data_url", "image_data"}
                )
                if forbidden:
                    _invalid(f"forbidden field {raw_key!r} at {path}")
            _reject_embedded_artifacts(child, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_embedded_artifacts(child, f"{path}[{index}]")


def _keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        _invalid(f"{label} keys are invalid; missing={missing}, extra={extra}")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> None:
    if not isinstance(value, str):
        _invalid(f"{label} must be text")


def _nonempty_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value:
        _invalid(f"{label} must be non-empty text")


def _optional_text(value: object, label: str) -> None:
    if value is not None:
        _nonempty_text(value, label)


def _text_and_count(text: object, count: object, label: str) -> None:
    if not isinstance(text, str):
        _invalid(f"{label}.text must be complete text")
    if isinstance(count, bool) or not isinstance(count, int) or count != len(text):
        _invalid(f"{label}.text_characters must equal len(text)")


def _http_url(value: object, label: str) -> None:
    if not isinstance(value, str):
        _invalid(f"{label} must be an HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in ("http", "https") or not parsed.netloc:
        _invalid(f"{label} must be an HTTP(S) URL")


def _timestamp(value: object, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        _invalid(f"{label} must be an ISO-8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _invalid(f"{label} must be an ISO-8601 timestamp")


def _timestamp_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _first(value: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        candidate = value.get(key)
        if candidate is not None:
            return candidate
    return None


def _invalid(message: str) -> NoReturn:
    raise InvalidGoogleDocument(message)
