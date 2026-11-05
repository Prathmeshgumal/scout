"""Canonical JSON contract for one directly requested generic page."""

from __future__ import annotations

from datetime import datetime
from typing import Any, NoReturn
from urllib.parse import urlsplit

from ..capture.writer import Document


SCHEMA_VERSION = "1.0"
EXTRACTOR = "generic_page_json"

#: What a capture's error may be. Frozen here rather than imported from the
#: attempt vocabulary: this is a stored contract, and a document written today
#: must still validate after that vocabulary grows. `capture_error` is the one
#: kind no attempt records -- a page that arrived whole but would not
#: screenshot, which is what `partial` means.
#: What a capture's status may be, and what the projection stores.
CAPTURE_STATUSES = ("succeeded", "partial", "failed")

FAILURE_KINDS = (
    "blocked", "login_required", "not_found", "timeout", "browser_error",
    "extract_failed", "parser_drift", "worker_lost", "account_mismatch",
    "storage_error", "cancelled", "capture_error",
)


class InvalidGenericDocument(ValueError):
    """A generic page document does not satisfy schema 1.0."""


def build(document: Document, *, capture_id: str,
          normalised_url: str) -> dict[str, Any]:
    rendered = document.text or ""
    partial = document.screenshot_error is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "rank": 1,
        "url": document.url_requested,
        "normalised_url": normalised_url,
        "host": document.host,
        "title": document.title,
        "snippet": "",
        "selection": {"selected": True, "reason": "direct"},
        "capture": {
            "capture_id": capture_id,
            "mode": "generic",
            "status": "partial" if partial else "succeeded",
            "url_requested": document.url_requested,
            "url_final": document.url_final or document.url_requested,
            "host": document.host,
            "http_status": document.http_status,
            "title": document.title,
            "captured_at": document.captured_at.isoformat(),
            "text": rendered,
            "text_characters": len(rendered),
            "completeness": {
                "scroll_stopped": document.scroll_stopped or "unknown",
                "scroll_growing": document.scroll_growing,
            },
            "error": ({
                "kind": "capture_error",
                "detail": document.screenshot_error,
            } if partial else None),
        },
    }


def validate(document: Any) -> dict[str, Any]:
    document = _mapping(document, "generic document")
    required = {
        "rank", "url", "normalised_url", "host", "title", "snippet",
        "selection", "capture",
    }
    # Optional, and only on the way in. Every document written from here on
    # says which schema it is, the way a Google or LinkedIn document does --
    # but the first version of this extractor wrote none, and a stored document
    # is re-validated on every read. Requiring it would make those unreadable
    # rather than merely old.
    if "schema_version" in document:
        required = required | {"schema_version"}
        if document["schema_version"] != SCHEMA_VERSION:
            _invalid(f"schema_version must be {SCHEMA_VERSION!r}")
    _keys(document, required, "generic document")
    if isinstance(document["rank"], bool) or document["rank"] != 1:
        _invalid("rank must be the integer 1")
    _http_url(document["url"], "url")
    _http_url(document["normalised_url"], "normalised_url")
    _nonempty_text(document["host"], "host")
    _optional_text(document["title"], "title")
    if document["snippet"] != "":
        _invalid("snippet must be empty for a direct URL")

    selection = document["selection"]
    if selection != {"selected": True, "reason": "direct"}:
        _invalid("selection must describe a selected direct URL")

    capture = _mapping(document["capture"], "capture")
    capture_keys = {
        "capture_id", "mode", "status", "url_requested", "url_final",
        "host", "http_status", "title", "captured_at", "text",
        "text_characters", "completeness", "error",
    }
    # `screenshots` is attached by the reader when a caller asks for it, and is
    # never stored: the URLs in it are presigned and expire, so a document
    # carrying them would go stale in the bucket. Allowed here so that what the
    # API serves still satisfies the contract it claims to serve.
    if "screenshots" in capture:
        capture_keys = capture_keys | {"screenshots"}
        _screenshots(capture["screenshots"])
    # `markdown` is attached the same way, and is never stored for the same
    # kind of reason: it is derived from the stored HTML and can be re-derived.
    if "markdown" in capture:
        capture_keys = capture_keys | {"markdown"}
        if capture["markdown"] is not None and not isinstance(
                capture["markdown"], str):
            _invalid("capture.markdown must be a string or null")
    _keys(capture, capture_keys, "capture")
    if capture["mode"] != "generic":
        _invalid("capture.mode must be 'generic'")
    status = capture["status"]
    if status not in CAPTURE_STATUSES:
        _invalid("capture.status must be succeeded, partial or failed")
    # A failed capture is a page that was never read: there is no capture id to
    # point at, no final URL it settled on, and no instant it was read at. It
    # is still the answer to "what happened to this request", which is why the
    # reader returns one and why the contract has to accept it.
    failed = status == "failed"
    _nonempty_text(capture["capture_id"], "capture.capture_id",
                   nullable=failed)
    _http_url(capture["url_requested"], "capture.url_requested")
    _http_url(capture["url_final"], "capture.url_final", nullable=failed)
    _nonempty_text(capture["host"], "capture.host")
    http_status = capture["http_status"]
    if http_status is not None and (
            isinstance(http_status, bool) or not isinstance(http_status, int)
            or not 100 <= http_status <= 599):
        _invalid("capture.http_status must be an HTTP status or null")
    _optional_text(capture["title"], "capture.title")
    _timestamp(capture["captured_at"], "capture.captured_at", nullable=failed)
    text = capture["text"]
    count = capture["text_characters"]
    if failed and text is None:
        if isinstance(count, bool) or count != 0:
            _invalid("capture.text_characters must be zero without text")
    elif (not isinstance(text, str) or isinstance(count, bool)
            or not isinstance(count, int) or count != len(text)):
        _invalid("capture.text_characters must equal len(capture.text)")
    completeness = _mapping(capture["completeness"], "capture.completeness")
    _keys(completeness, {"scroll_stopped", "scroll_growing"},
          "capture.completeness")
    _nonempty_text(
        completeness["scroll_stopped"], "capture.completeness.scroll_stopped")
    if not isinstance(completeness["scroll_growing"], bool):
        _invalid("capture.completeness.scroll_growing must be Boolean")

    error = capture["error"]
    if status == "succeeded" and error is not None:
        _invalid("a succeeded capture cannot have an error")
    if status != "succeeded":
        error = _mapping(error, "capture.error")
        _keys(error, {"kind", "detail"}, "capture.error")
        if error["kind"] not in FAILURE_KINDS:
            _invalid("capture.error.kind is not a known failure")
        if status == "partial" and error["kind"] != "capture_error":
            _invalid("a partial capture must use capture_error")
        _nonempty_text(error["detail"], "capture.error.detail")

    if document["url"] != capture["url_requested"]:
        _invalid("url must match capture.url_requested")
    if document["host"] != capture["host"]:
        _invalid("host must match capture.host")
    if document["title"] != capture["title"]:
        _invalid("title must match capture.title")
    return document


def _screenshots(value: object) -> None:
    if not isinstance(value, list):
        _invalid("capture.screenshots must be an array")
    for index, shot in enumerate(value):
        shot = _mapping(shot, f"capture.screenshots[{index}]")
        _keys(shot, {"ordinal", "url", "content_type", "byte_size", "sha256"},
              f"capture.screenshots[{index}]")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid(f"{label} must be an object")
    return value


def _keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _invalid(f"{label} keys are invalid")


def _nonempty_text(value: object, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str) or not value:
        _invalid(f"{label} must be non-empty text")


def _optional_text(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        _invalid(f"{label} must be text or null")


def _http_url(value: object, label: str, *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, str):
        _invalid(f"{label} must be an HTTP(S) URL")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
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


def _invalid(message: str) -> NoReturn:
    raise InvalidGenericDocument(message)
