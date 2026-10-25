"""Verified persistence for canonical generic page JSON."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import text

from ..capture.writer import store_verified
from ..store.blobs import BlobStore, sha256
from ..store.db import transaction
from ..store.ids import EXTRACTION, new_id
from .contract import EXTRACTOR, SCHEMA_VERSION, validate
from .project import project


class GenericDocumentMissing(LookupError):
    """No canonical generic extraction exists for the request."""


class GenericDocumentCorrupt(RuntimeError):
    """The stored generic object disagrees with its durable metadata."""


def store_extraction(database_url: str | None, *, request_id: str,
                     attempt_id: int | None, capture_id: str,
                     document: dict, store: BlobStore) -> str:
    validate(document)
    extraction_id = new_id(EXTRACTION)
    payload = json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    key = f"extractions/{extraction_id}/generic.json"
    outcome = "partial" if document["capture"]["status"] == "partial" else "ok"
    # Remote I/O stays outside the database transaction. In the exceptional
    # concurrent-writer race the loser may leave one unreferenced object, but
    # it cannot hold a database connection through S3 retries or register a
    # competing canonical extraction row.
    store_verified(store, key, payload, "application/json")

    with transaction(database_url) as connection:
        # Both workers and operator backfills use this writer. Serialize on the
        # request so two processes cannot upload and register competing
        # canonical documents for the same extractor version.
        connection.execute(text("""
            SELECT pg_advisory_xact_lock(hashtextextended(:request, 0))
        """), {"request": request_id})
        existing = connection.execute(text("""
            SELECT id FROM extraction
            WHERE request_id=:request AND extractor=:extractor
              AND extractor_version=:version AND document_key IS NOT NULL
            ORDER BY extracted_at DESC, id DESC LIMIT 1
        """), {
            "request": request_id, "extractor": EXTRACTOR,
            "version": SCHEMA_VERSION,
        }).scalar_one_or_none()
        if existing is not None:
            return existing
        connection.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version,
                 extracted_at, outcome, document_key, document_sha256,
                 document_bytes)
            VALUES (:id, :request, :attempt, :extractor, :version, :now,
                    :outcome, :key, :digest, :size)
        """), {
            "id": extraction_id, "request": request_id,
            "attempt": attempt_id, "extractor": EXTRACTOR,
            "version": SCHEMA_VERSION, "now": datetime.now(UTC),
            "outcome": outcome, "key": key, "digest": sha256(payload),
            "size": len(payload),
        })
        connection.execute(text("""
            INSERT INTO extraction_input (extraction_id, capture_id, section)
            VALUES (:extraction, :capture, 'page')
        """), {"extraction": extraction_id, "capture": capture_id})
        # In this transaction, not a later one: the evidence and the row that
        # makes it findable are one write, so no reader can see a document
        # that no question can reach.
        project(connection, extraction_id=extraction_id,
                request_id=request_id, document=document)
    return extraction_id


def load_extraction(connection, request_id: str, store: BlobStore) -> dict:
    row = connection.execute(text("""
        SELECT extractor, extractor_version, document_key,
               document_sha256, document_bytes
        FROM extraction
        WHERE request_id=:request AND extractor=:extractor
          AND document_key IS NOT NULL
        ORDER BY extracted_at DESC, id DESC LIMIT 1
    """), {"request": request_id, "extractor": EXTRACTOR}).one_or_none()
    if row is None:
        raise GenericDocumentMissing(
            f"no generic page document for request {request_id}")
    if row.extractor_version != SCHEMA_VERSION:
        raise GenericDocumentCorrupt("generic extraction identity is invalid")
    try:
        payload = store.get(row.document_key)
    except Exception as exc:
        raise GenericDocumentCorrupt(
            "generic extraction object is unavailable") from exc
    if not isinstance(payload, bytes):
        raise GenericDocumentCorrupt("generic extraction object is not bytes")
    if len(payload) != row.document_bytes or sha256(payload) != row.document_sha256:
        raise GenericDocumentCorrupt(
            "generic extraction failed integrity verification")
    try:
        return validate(json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise GenericDocumentCorrupt(
            "generic extraction does not satisfy JSON schema 1.0") from exc
