"""Project a generic URL request into the Google destination result shape."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import Connection, text

from ..store.blobs import sha256
from ..store.config import artifact_url_ttl_seconds
from .contract import SCHEMA_VERSION
from .store import GenericDocumentMissing, load_extraction


ACTIVE = {"queued", "assigned", "running"}


class GenericRequestMissing(LookupError):
    """No generic URL request exists with the supplied id."""


class GenericTextUnavailable(RuntimeError):
    """A stored text artifact cannot be read or verified."""


class GenericResultUnavailable(RuntimeError):
    """A terminal request is inconsistent with its durable captures."""


@dataclass(frozen=True)
class GenericResult:
    document: dict
    pending: bool


def _text(store, artifact) -> str:
    if artifact is None:
        return ""
    try:
        payload = store.get(artifact.blob_key)
    except Exception as exc:
        raise GenericTextUnavailable(
            "generic capture text is not available") from exc
    if len(payload) != artifact.byte_size or sha256(payload) != artifact.sha256:
        raise GenericTextUnavailable(
            "generic capture text failed integrity verification")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GenericTextUnavailable(
            "generic capture text is not valid UTF-8") from exc


def _capture(row, capture, rendered_text: str) -> dict:
    if capture is None:
        if row.cause:
            error = {
                "kind": row.cause,
                "detail": row.cause_detail or "the generic capture failed",
            }
        elif row.status == "cancelled":
            error = {
                "kind": "cancelled",
                "detail": "the request was cancelled before capture",
            }
        else:
            error = {
                "kind": "browser_error",
                "detail": "the request produced no capture",
            }
        return {
            "capture_id": None,
            "mode": "generic",
            "status": "failed",
            "url_requested": row.url_requested,
            "url_final": None,
            "host": urlsplit(row.url_normalised).hostname or "",
            "http_status": None,
            "title": None,
            "captured_at": None,
            "text": None,
            "text_characters": 0,
            "completeness": {
                "scroll_stopped": "not_started",
                "scroll_growing": False,
            },
            "error": error,
        }

    failed = row.status in {"failed", "cancelled"}
    partial = not failed and capture.screenshot_error is not None
    error = (
        {"kind": row.cause or "browser_error",
         "detail": row.cause_detail or "the generic capture failed"}
        if failed else
        {"kind": "capture_error", "detail": capture.screenshot_error}
        if partial else None
    )
    return {
        "capture_id": capture.id,
        "mode": "generic",
        "status": "failed" if failed else "partial" if partial else "succeeded",
        "url_requested": capture.url_requested,
        "url_final": capture.url_final,
        "host": capture.host,
        "http_status": capture.http_status,
        "title": capture.title,
        "captured_at": capture.captured_at.isoformat(),
        "text": rendered_text,
        "text_characters": len(rendered_text),
        "completeness": {
            "scroll_stopped": capture.scroll_stopped or "unknown",
            "scroll_growing": capture.scroll_growing,
        },
        "error": error,
    }


def with_screenshots(connection: Connection, store, document: dict) -> dict:
    """Attach expiring artifact URLs to the page this document describes.

    Not stored with it, and for the reason Google does the same: the URLs are
    presigned and short-lived, so a document holding them would be stale in the
    bucket long before it stopped being evidence.
    """
    enriched = deepcopy(document)
    capture = enriched.get("capture")
    if not isinstance(capture, dict) or not capture.get("capture_id"):
        return enriched
    rows = connection.execute(text("""
        SELECT ordinal, blob_key, content_type, byte_size, sha256
        FROM artifact
        WHERE kind='screenshot' AND capture_id=:capture
        ORDER BY ordinal
    """), {"capture": capture["capture_id"]}).all()
    if not rows:
        return enriched
    expires = artifact_url_ttl_seconds()
    capture["screenshots"] = [{
        "ordinal": row.ordinal,
        "url": store.url(row.blob_key, expires),
        "content_type": row.content_type,
        "byte_size": row.byte_size,
        "sha256": row.sha256,
    } for row in rows]
    return enriched


def with_markdown(connection: Connection, document: dict) -> dict:
    """Attach the Markdown of the page this document describes.

    Not stored with it: Markdown is derived from the stored HTML and can be
    re-derived when the converter improves, so the document only names the
    capture. Unlike `text`, it keeps the page's links. The current converter
    resolves root-relative links against the URL the page landed on; captures
    from an older converter keep them relative until re-derived. Null when the
    capture has no Markdown, or there is no capture.
    """
    enriched = deepcopy(document)
    capture = enriched.get("capture")
    if not isinstance(capture, dict):
        return enriched
    markdown = None
    if capture.get("capture_id"):
        markdown = connection.execute(text("""
            SELECT markdown FROM capture WHERE id=:capture
        """), {"capture": capture["capture_id"]}).scalar_one_or_none()
    capture["markdown"] = markdown
    return enriched


def _enrich(connection: Connection, store, document: dict, *,
            include_screenshots: bool, include_markdown: bool) -> dict:
    if include_screenshots:
        document = with_screenshots(connection, store, document)
    if include_markdown:
        document = with_markdown(connection, document)
    return document


def url_at(connection: Connection, url_normalised: str, *,
           at: datetime, open_store,
           include_screenshots: bool = False,
           include_markdown: bool = False) -> dict:
    """The newest stored reading of one URL at or before `at`.

    Answered from the projection, which is the only reason it exists: without
    it this question needs the id of the request that happened to make the
    reading, which a caller asking about a page does not have.
    """
    observation = connection.execute(text("""
        SELECT request_id, url_requested, observed_at
        FROM generic.page_observation
        WHERE url_normalised = :url AND observed_at <= :at
        ORDER BY observed_at DESC, id DESC LIMIT 1
    """), {"url": url_normalised, "at": at}).one_or_none()
    if observation is None:
        raise GenericRequestMissing(
            f"no reading of {url_normalised!r} had been observed"
            f" by {at.isoformat()}")
    answer = url_result(
        connection, observation.request_id, open_store=open_store,
        include_screenshots=include_screenshots,
        include_markdown=include_markdown)
    return {
        "url": observation.url_requested,
        "normalised_url": url_normalised,
        "request_id": observation.request_id,
        "as_of": at.isoformat(),
        "observed_at": observation.observed_at.isoformat(),
        "result": answer.document,
    }


def url_result(connection: Connection, request_id: str, *, open_store,
               include_screenshots: bool = False,
               include_markdown: bool = False) -> GenericResult:
    row = connection.execute(text("""
        SELECT request.id, request.kind, request.status,
               request.url_requested, request.url_normalised,
               attempt.id AS attempt_id, attempt.cause, attempt.cause_detail
        FROM scrape_request request
        LEFT JOIN LATERAL (
            SELECT id, cause, cause_detail
            FROM scrape_attempt WHERE request_id=request.id
            ORDER BY attempt_no DESC LIMIT 1
        ) attempt ON TRUE
        WHERE request.id=:request
    """), {"request": request_id}).one_or_none()
    if row is None or row.kind != "generic":
        raise GenericRequestMissing(f"no generic URL request {request_id}")

    if row.status in ACTIVE:
        return GenericResult({
            "schema_version": SCHEMA_VERSION,
            "request_id": row.id,
            "status": row.status,
            "url": row.url_normalised,
            "result": None,
        }, pending=True)

    if row.status == "satisfied":
        # A freshness-satisfied request owns no capture of its own: it rests on
        # a page an earlier request read, recorded as its satisfaction. Answer
        # with that reading, as Google search does. Without this the read below
        # finds no capture and reports a page we hold as a failed capture.
        source = connection.execute(text("""
            SELECT capture.request_id
            FROM request_satisfaction satisfaction
            JOIN capture ON capture.id=satisfaction.capture_id
            WHERE satisfaction.request_id=:request
              AND satisfaction.section='page'
        """), {"request": request_id}).scalar_one_or_none()
        if source is None or source == request_id:
            raise GenericResultUnavailable(
                "satisfied generic request has no source capture")
        return url_result(
            connection, source, open_store=open_store,
            include_screenshots=include_screenshots,
            include_markdown=include_markdown)

    store = open_store()
    try:
        document = load_extraction(connection, request_id, store)
    except GenericDocumentMissing:
        document = None
    if document is not None:
        document = _enrich(connection, store, document,
                           include_screenshots=include_screenshots,
                           include_markdown=include_markdown)
        return GenericResult(document, pending=False)
    # Captures made before generic extraction documents existed remain
    # readable and are eligible for the explicit backfill command.

    attempt_clause = (
        "attempt_id=:attempt" if row.attempt_id is not None
        else "attempt_id IS NULL")
    capture = connection.execute(text(f"""
        SELECT id, url_requested, url_final, host, http_status, title,
               captured_at, scroll_stopped, scroll_growing, screenshot_error
        FROM capture
        WHERE request_id=:request AND section='page' AND {attempt_clause}
        ORDER BY captured_at DESC, id DESC LIMIT 1
    """), {
        "request": request_id, "attempt": row.attempt_id,
    }).one_or_none()
    if capture is None and row.status == "succeeded":
        raise GenericResultUnavailable("generic URL result has no page capture")
    artifact = None
    rendered_text = ""
    if capture is not None:
        artifact = connection.execute(text("""
            SELECT blob_key, byte_size, sha256
            FROM artifact
            WHERE capture_id=:capture AND kind='text' AND ordinal=0
        """), {"capture": capture.id}).one_or_none()
        rendered_text = _text(store, artifact) if artifact else ""

    capture_document = _capture(row, capture, rendered_text)
    host = capture.host if capture is not None else (
        urlsplit(row.url_normalised).hostname or "")
    title = capture.title if capture is not None else None
    # The URL the capture was made against, not the one the caller typed. The
    # contract requires the document to agree with its own capture, and the two
    # differ whenever normalisation did anything -- lowercasing the host,
    # dropping a fragment. Reporting the typed one made every such request
    # unbackfillable, and the marker that recorded the failure kept it that way.
    requested = (
        capture.url_requested if capture is not None else row.url_requested)
    synthesised = {
        "schema_version": SCHEMA_VERSION,
        "rank": 1,
        "url": requested,
        "normalised_url": row.url_normalised,
        "host": host,
        "title": title,
        "snippet": "",
        "selection": {"selected": True, "reason": "direct"},
        "capture": capture_document,
    }
    synthesised = _enrich(connection, store, synthesised,
                          include_screenshots=include_screenshots,
                          include_markdown=include_markdown)
    return GenericResult(synthesised, pending=False)
