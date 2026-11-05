"""Transport-neutral Google search extraction from captured evidence.

The envelope lives apart from the worker task, so it can be built and tested
without a browser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


from ..capture.detect import detect_signs, is_login_dialog_text
from .capture_contract import destination_failure
from . import contract


def _outcome(
    failure: Mapping[str, str] | None,
    serp: Mapping[str, object],
    results: Sequence[Mapping[str, object]],
) -> str:
    """Derive the extraction outcome from what was actually stored.

    A failed request still yields `partial` rather than `failed`: a durable
    readable SERP is useful evidence, and a failed extraction must keep meaning
    "no document was produced" everywhere in the schema.
    """
    if failure is not None:
        return "partial"
    statuses = [
        result["capture"]["status"]
        for result in results
        if result.get("capture") is not None
    ]
    if serp["status"] != "succeeded" or any(
            status != "succeeded" for status in statuses):
        return "partial"
    return "ok"


def build_search_document(
    *,
    request_id: str,
    requested_at: str,
    started_at: str,
    finished_at: str,
    query: str,
    normalised_query: str,
    requested_top_n: int,
    google_host: str | None,
    serp_url: str | None,
    searched_at: str | None,
    browser_locale: str | None,
    browser_timezone: str | None,
    serp: Mapping[str, object],
    results: Sequence[Mapping[str, object]],
    failure: Mapping[str, str] | None,
) -> dict:
    """Assemble one schema 1.0 search document. Callers validate it."""
    entries = list(results)
    return {
        "schema_version": contract.SCHEMA_VERSION,
        "request": {
            "request_id": request_id,
            "status": "failed" if failure is not None else "succeeded",
            "requested_at": requested_at,
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "search": {
            "query": query,
            "normalised_query": normalised_query,
            "requested_top_n": requested_top_n,
            "results_examined": len(entries),
            "google_host": google_host,
            "serp_url": serp_url,
            # Unknown values are null, never omitted from the contract.
            "browser_locale": browser_locale,
            "browser_timezone": browser_timezone,
            "searched_at": searched_at,
        },
        "outcome": _outcome(failure, serp, entries),
        "failure": dict(failure) if failure is not None else None,
        "serp": dict(serp),
        "results": [dict(entry) for entry in entries],
        "summary": contract.summary(entries),
    }


def _capture_status(document) -> tuple[str, dict[str, str] | None]:
    error = getattr(document, "screenshot_error", None)
    if error:
        return "partial", {"kind": "capture_error", "detail": error}
    return "succeeded", None


def serp_part(document, *, capture_id: str) -> dict:
    """Project one stored results page onto the contract's serp block."""
    status, _error = _capture_status(document)
    text_value = document.text or ""
    return {
        "capture_id": capture_id,
        "status": status,
        "url_final": document.url_final or document.url_requested,
        "title": document.title,
        "text": text_value,
        "text_characters": len(text_value),
        # A SERP is captured unscrolled on purpose: Google's ranking is what
        # the first screen shows.
        "completeness": {
            "scroll_stopped": "not_scrolled",
            "scroll_growing": False,
        },
    }


def result_part(
    observation: Mapping[str, object],
    *,
    document=None,
    capture_id: str | None = None,
    mode: str = "generic",
    failure: Mapping[str, str] | None = None,
) -> dict:
    """Project one examined result, with its destination capture if opened."""
    capture = None
    if document is not None and capture_id is not None:
        status, error = _capture_status(document)
        signs = observation.get("destination_signs")
        challenge = None
        if isinstance(signs, Mapping):
            dialogs = observation.get("destination_dialogs")
            dialogs = dialogs if isinstance(dialogs, list) else []
            challenge = detect_signs(
                url=str(signs.get("url") or document.url_final or document.url_requested),
                status=document.http_status, head=str(signs.get("head") or ""),
                text_length=int(signs.get("textLength", signs.get("text_length", 0))),
                login_dialog=any(isinstance(dialog, Mapping) and is_login_dialog_text(str(dialog.get("text") or "")) for dialog in dialogs),
            )
        destination_error = destination_failure(document.http_status, challenge)
        if destination_error is not None:
            status, error = "failed", destination_error
        text_value = document.text or ""
        capture = {
            "capture_id": capture_id,
            "mode": mode,
            "status": status,
            "url_requested": document.url_requested,
            "url_final": document.url_final or document.url_requested,
            "host": document.host,
            "http_status": document.http_status,
            "title": document.title,
            "captured_at": document.captured_at.isoformat(),
            "text": text_value,
            "text_characters": len(text_value),
            "completeness": {
                "scroll_stopped": document.scroll_stopped or "unknown",
                "scroll_growing": bool(document.scroll_growing),
            },
            "error": error,
        }
    elif failure is not None:
        failure_kind = str(failure.get("cause") or "browser_error")
        if failure_kind not in contract.RESULT_FAILURE_KINDS:
            failure_kind = "browser_error"
        capture = {
            "capture_id": None,
            "mode": mode,
            "status": "failed",
            "url_requested": str(
                failure.get("requested_url") or observation["url"]),
            "url_final": None,
            "host": observation["host"],
            "http_status": None,
            "title": None,
            "captured_at": None,
            "text": None,
            "text_characters": 0,
            "completeness": {
                "scroll_stopped": "not_started",
                "scroll_growing": False,
            },
            "error": {
                "kind": failure_kind,
                "detail": str(
                    failure.get("error")
                    or failure.get("operator_detail")
                    or "destination capture failed"
                ),
            },
        }
    selection = observation.get("selection")
    if not isinstance(selection, Mapping):
        selection = {"selected": False, "reason": "beyond_top_n"}
    return {
        "rank": observation["rank"],
        "url": observation["url"],
        "normalised_url": observation["normalised_url"],
        "host": observation["host"],
        "title": observation.get("title") or "",
        "snippet": observation.get("snippet") or "",
        "selection": {
            "selected": bool(selection["selected"]),
            "reason": str(selection["reason"]),
        },
        "capture": capture,
    }


__all__ = [
    "build_search_document",
    "result_part",
    "serp_part",
]
