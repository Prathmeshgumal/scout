"""Read one Google search request back as its canonical document.

The branching here is the whole reason this is not inline in the route: a
request can be unfinished, finished with a document, or finished having failed
before any document was written, and each is a different answer rather than an
error. The CLI needs the same three answers as the HTTP API, so the decision
lives here and each caller only maps the outcome onto its own vocabulary --
status codes for the API, exit codes for the command line.
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy import Connection, text

from ..store.config import artifact_url_ttl_seconds
from .contract import SCHEMA_VERSION, cancelled_envelope, failed_envelope
from .store import GoogleDocumentCorrupt, GoogleDocumentMissing, load_extraction

ACTIVE = {"queued", "assigned", "running"}

__all__ = [
    "ACTIVE",
    "GoogleDocumentCorrupt",
    "GoogleDocumentUnavailable",
    "GoogleRequestMissing",
    "SearchResult",
    "search_result",
]


class GoogleRequestMissing(LookupError):
    """No Google search request with that id."""


class GoogleDocumentUnavailable(RuntimeError):
    """The request is finished but its document cannot be read yet."""


class SearchResult:
    """One answer about a request: the document, and whether it is settled."""

    __slots__ = ("document", "pending")

    def __init__(self, document: dict, *, pending: bool):
        self.document = document
        self.pending = pending


def search_result(connection: Connection, request_id: str, *, open_store,
                  include_screenshots: bool = False) -> SearchResult:
    """`open_store` is a factory, not a store: an unfinished request is
    answered without ever reaching object storage, and opening it eagerly
    would turn a missing blob configuration into a failure to report that a
    search is merely still running."""
    row = connection.execute(text("""
        SELECT r.id, r.kind, r.status, r.requested_at, r.satisfied_at,
               r.cancel_requested_at,
               r.query_requested, r.query_normalised, r.google_top_n,
               attempt.started_at, attempt.ended_at,
               attempt.cause, attempt.cause_detail
        FROM scrape_request r
        LEFT JOIN LATERAL (
            SELECT started_at, ended_at, cause, cause_detail
            FROM scrape_attempt WHERE request_id=r.id
            ORDER BY attempt_no DESC LIMIT 1
        ) attempt ON TRUE
        WHERE r.id=:request
    """), {"request": request_id}).one_or_none()
    if row is None or row.kind != "google_search":
        raise GoogleRequestMissing(f"no Google search request {request_id}")

    if row.status in ACTIVE:
        return SearchResult({
            "schema_version": SCHEMA_VERSION,
            "request_id": row.id,
            "status": row.status,
            "search": {
                "query": row.query_requested,
                "normalised_query": row.query_normalised,
                "requested_top_n": row.google_top_n,
            },
            "result": None,
        }, pending=True)

    source_request_id = row.id
    if row.status == "satisfied":
        source_request_id = connection.execute(text("""
            SELECT capture.request_id
            FROM request_satisfaction satisfaction
            JOIN capture ON capture.id=satisfaction.capture_id
            WHERE satisfaction.request_id=:request
              AND satisfaction.section='serp'
        """), {"request": request_id}).scalar_one_or_none()
        if source_request_id is None:
            raise GoogleDocumentUnavailable(
                "Satisfied Google search has no source capture")

    store = open_store()
    try:
        document = load_extraction(connection, source_request_id, store)
    except GoogleDocumentMissing as exc:
        if row.status == "cancelled":
            document = cancelled_envelope({
                "request_id": row.id,
                "requested_at": row.requested_at,
                "started_at": row.started_at,
                "finished_at": row.ended_at,
                "cancel_requested_at": row.cancel_requested_at,
                "query": row.query_requested,
                "normalised_query": row.query_normalised,
                "top_n": row.google_top_n,
            })
        elif row.status != "failed":
            raise GoogleDocumentUnavailable(
                "Google search document is not available") from exc
        else:
            document = failed_envelope({
                "request_id": row.id,
                "requested_at": row.requested_at,
                "started_at": row.started_at,
                "finished_at": row.ended_at,
                "query": row.query_requested,
                "normalised_query": row.query_normalised,
                "top_n": row.google_top_n,
                "failure_kind": row.cause,
                "failure_detail": row.cause_detail or "",
            })

    if source_request_id != row.id:
        # A freshness-satisfied request owns no extraction of its own. Return
        # the verified source document as this request's successful answer,
        # while retaining the original observation and result timestamps.
        document = deepcopy(document)
        document["request"] = {
            **document["request"],
            "request_id": row.id,
            "status": "satisfied",
            "requested_at": row.requested_at.isoformat(),
            "started_at": None,
            "finished_at": row.satisfied_at.isoformat(),
        }

    if include_screenshots:
        document = with_screenshots(connection, store, document)
    return SearchResult(document, pending=False)


def with_screenshots(connection: Connection, store, document: dict) -> dict:
    """Attach expiring artifact URLs to the SERP and each captured result."""
    enriched = deepcopy(document)
    captures: dict[str, dict] = {}
    serp = enriched.get("serp")
    if isinstance(serp, dict) and serp.get("capture_id"):
        captures[serp["capture_id"]] = serp
    for result in enriched.get("results", []):
        capture = result.get("capture")
        if isinstance(capture, dict) and capture.get("capture_id"):
            captures[capture["capture_id"]] = capture
    if not captures:
        return enriched

    rows = connection.execute(text("""
        SELECT capture_id, ordinal, blob_key, content_type, byte_size, sha256
        FROM artifact
        WHERE kind='screenshot' AND capture_id = ANY(:captures)
        ORDER BY capture_id, ordinal
    """), {"captures": list(captures)}).all()
    expires = artifact_url_ttl_seconds()
    for row in rows:
        captures[row.capture_id].setdefault("screenshots", []).append({
            "ordinal": row.ordinal,
            "url": store.url(row.blob_key, expires),
            "content_type": row.content_type,
            "byte_size": row.byte_size,
            "sha256": row.sha256,
        })
    return enriched
