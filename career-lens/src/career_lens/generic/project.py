"""Deterministically project a canonical generic page document into Postgres.

The document is the evidence; this is the copy a question can be asked of.
Written in the same transaction as the extraction row it belongs to, so the
two cannot disagree about what was read.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .contract import validate


class ProjectionConflict(RuntimeError):
    """An extraction already carries a different projection."""


def project(connection: Connection, *, extraction_id: str, request_id: str,
            document: dict) -> int:
    """Insert and return this document's append-only observation row.

    Repeating the same projection returns the row that already exists.
    Reusing one extraction for a different document is refused rather than
    reconciled: append-only evidence cannot be corrected by rewriting it.
    """
    canonical = validate(document)
    values = _values(extraction_id, request_id, canonical)

    existing = connection.execute(text("""
        SELECT id, capture_id, url_normalised, status, observed_at,
               text_characters
        FROM generic.page_observation WHERE extraction_id=:extraction_id
    """), {"extraction_id": extraction_id}).one_or_none()
    if existing is not None:
        _assert_matches(existing, values)
        return existing.id

    return connection.execute(text("""
        INSERT INTO generic.page_observation
            (extraction_id, request_id, capture_id, url_requested,
             url_normalised, host, title, http_status, status, scroll_stopped,
             scroll_growing, text_characters, observed_at, failure_kind,
             failure_detail)
        VALUES
            (:extraction_id, :request_id, :capture_id, :url_requested,
             :url_normalised, :host, :title, :http_status, :status,
             :scroll_stopped, :scroll_growing, :text_characters, :observed_at,
             :failure_kind, :failure_detail)
        RETURNING id
    """), values).scalar_one()


def _values(extraction_id: str, request_id: str,
            document: dict) -> dict[str, Any]:
    capture = document["capture"]
    error = capture["error"] or {}
    return {
        "extraction_id": extraction_id,
        "request_id": request_id,
        "capture_id": capture["capture_id"],
        "url_requested": document["url"],
        "url_normalised": document["normalised_url"],
        "host": document["host"],
        "title": document["title"],
        "http_status": capture["http_status"],
        "status": capture["status"],
        "scroll_stopped": capture["completeness"]["scroll_stopped"],
        "scroll_growing": capture["completeness"]["scroll_growing"],
        "text_characters": capture["text_characters"],
        "observed_at": _observed_at(capture),
        "failure_kind": error.get("kind"),
        "failure_detail": error.get("detail"),
    }


def _observed_at(capture: dict) -> datetime:
    """When the page was read.

    Not when this row was written: a temporal question asks what the page
    looked like at an instant, and the answer is only as good as the instant
    it is filed under. A capture that never happened has no such instant,
    which is why a failed document is not projected.
    """
    captured_at = capture["captured_at"]
    if captured_at is None:
        raise ProjectionConflict(
            "a document with no capture instant cannot be projected")
    return datetime.fromisoformat(captured_at.replace("Z", "+00:00"))


def _assert_matches(existing, values: dict[str, Any]) -> None:
    for column in ("capture_id", "url_normalised", "status", "text_characters"):
        if getattr(existing, column) != values[column]:
            raise ProjectionConflict(
                f"extraction already projected with a different {column}")
    if existing.observed_at != values["observed_at"]:
        raise ProjectionConflict(
            "extraction already projected with a different instant")
