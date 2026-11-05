"""Verified persistence and loading for canonical Google JSON documents."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from ..capture.writer import store_verified
from ..store.blobs import BlobStore, sha256
from ..store.db import transaction
from ..store.ids import EXTRACTION, new_id
from .contract import SCHEMA_VERSION, validate_document
from .project import project


EXTRACTOR = "google_search_json"


class GoogleDocumentMissing(LookupError):
    """No canonical Google extraction exists for the request."""


class GoogleDocumentCorrupt(RuntimeError):
    """The stored object disagrees with its durable metadata or contract."""


def store_extraction(
    database_url: str | None,
    *,
    request_id: str,
    attempt_id: int,
    capture_ids: Sequence[tuple[str, str]],
    document: dict,
    store: BlobStore,
) -> str:
    """Validate and verify one document before inserting any referencing row."""
    validate_document(document)
    extraction_id = new_id(EXTRACTION)
    payload = json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    key = f"extractions/{extraction_id}/document.json"
    store_verified(store, key, payload, "application/json")

    with transaction(database_url) as connection:
        connection.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version,
                 extracted_at, outcome, document_key, document_sha256,
                 document_bytes)
            VALUES (:id, :request, :attempt, :extractor, :version, :now,
                    :outcome, :key, :digest, :size)
        """), {
            "id": extraction_id,
            "request": request_id,
            "attempt": attempt_id,
            "extractor": EXTRACTOR,
            "version": SCHEMA_VERSION,
            "now": datetime.now(UTC),
            "outcome": document["outcome"],
            "key": key,
            "digest": sha256(payload),
            "size": len(payload),
        })
        for section, capture_id in capture_ids:
            connection.execute(text("""
                INSERT INTO extraction_input (extraction_id, capture_id, section)
                VALUES (:extraction, :capture, :section)
            """), {
                "extraction": extraction_id,
                "capture": capture_id,
                "section": section,
            })
        project(
            connection, extraction_id=extraction_id,
            request_id=request_id, document=document)
    return extraction_id


def load_extraction(
    connection: Connection,
    request_id: str,
    store: BlobStore,
) -> dict:
    """Load the newest canonical document and verify bytes and JSON contract."""
    row = connection.execute(text("""
        SELECT extractor, extractor_version, document_key,
               document_sha256, document_bytes
        FROM extraction
        WHERE request_id=:request AND extractor=:extractor
          AND document_key IS NOT NULL
        ORDER BY extracted_at DESC, id DESC LIMIT 1
    """), {"request": request_id, "extractor": EXTRACTOR}).one_or_none()
    if row is None:
        raise GoogleDocumentMissing(
            f"no Google search document for request {request_id}")
    if row.extractor != EXTRACTOR or row.extractor_version != SCHEMA_VERSION:
        raise GoogleDocumentCorrupt("Google extraction identity is invalid")
    try:
        payload = store.get(row.document_key)
    except Exception as exc:  # storage clients use backend-specific exceptions
        raise GoogleDocumentCorrupt("Google extraction object is unavailable") from exc
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    if not isinstance(payload, bytes):
        raise GoogleDocumentCorrupt("Google extraction object is not bytes")
    if len(payload) != row.document_bytes:
        raise GoogleDocumentCorrupt("Google extraction byte count does not match")
    if sha256(payload) != row.document_sha256:
        raise GoogleDocumentCorrupt("Google extraction digest does not match")
    try:
        document = json.loads(payload)
        return validate_document(document)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GoogleDocumentCorrupt(
            "Google extraction does not satisfy JSON schema 1.0") from exc
