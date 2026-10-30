"""What the operator console reads about requests. Only a signed-in operator reaches these routes."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Connection, text

from ..store import blobs
from ..store.schema.core import KINDS, STATUSES
from . import serialise
from .db import database
from .operator_auth import require_operator

MAX_LIMIT = 100

Db = Annotated[Connection, Depends(database)]
router = APIRouter(prefix="/operator/api", dependencies=[Depends(require_operator)], include_in_schema=False)


def _cursor(requested_at: datetime, request_id: str) -> str:
    return base64.urlsafe_b64encode(f"{requested_at.isoformat()}|{request_id}".encode()).decode()


def _read_cursor(value: str) -> tuple[datetime, str]:
    try:
        moment, _, request_id = base64.urlsafe_b64decode(value.encode()).decode().partition("|")
        return datetime.fromisoformat(moment), request_id
    except ValueError as exc:
        raise HTTPException(422, {"code": "bad_cursor"}) from exc


@router.get("/requests")
def list_requests(
    connection: Db,
    status: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    before: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = 50,
) -> dict:
    if status is not None and status not in STATUSES:
        raise HTTPException(422, {"code": "bad_status"})
    if kind is not None and kind not in KINDS:
        raise HTTPException(422, {"code": "bad_kind"})
    moment, request_id = _read_cursor(before) if before else (None, None)
    pattern = None
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
    filters = """
        (CAST(:kind AS text) IS NULL OR kind = :kind)
        AND (CAST(:pattern AS text) IS NULL OR url_requested ILIKE :pattern OR query_requested ILIKE :pattern)
    """
    params = {"status": status, "kind": kind, "pattern": pattern}
    rows = connection.execute(text(f"""
        SELECT id, kind, status, label, url_requested, query_requested, requested_at, dispatch_wait_reason,
               (SELECT count(*) FROM scrape_attempt a WHERE a.request_id = r.id) AS attempts
        FROM scrape_request r
        WHERE {filters} AND (CAST(:status AS text) IS NULL OR status = :status)
          AND (CAST(:moment AS timestamptz) IS NULL OR (requested_at, id) < (:moment, :request_id))
        ORDER BY requested_at DESC, id DESC LIMIT :limit
    """), {**params, "moment": moment, "request_id": request_id, "limit": limit + 1}).all()
    counts = connection.execute(text(f"""
        SELECT status, count(*) AS n FROM scrape_request WHERE {filters} GROUP BY status
    """), params).all()
    shown = rows[:limit]
    return {
        "items": [{
            "id": row.id, "kind": row.kind, "status": row.status, "label": row.label,
            "target": row.url_requested or row.query_requested,
            "requested_at": row.requested_at.isoformat(),
            "wait_reason": row.dispatch_wait_reason, "attempts": row.attempts,
        } for row in shown],
        "counts": {row.status: row.n for row in counts},
        "next": _cursor(shown[-1].requested_at, shown[-1].id) if len(rows) > limit else None,
    }


@router.get("/requests/{request_id}")
def request_detail(request_id: str, connection: Db) -> dict:
    body = serialise.scrape_request(connection, request_id)
    if body is None:
        raise HTTPException(404, {"code": "not_found"})
    events = connection.execute(text("""
        SELECT attempt.attempt_no, event.id, event.at, event.elapsed_ms, event.duration_ms, event.kind, event.level,
               event.phase, event.detail, event.failed
        FROM attempt_event event JOIN scrape_attempt attempt ON attempt.id = event.attempt_id
        WHERE attempt.request_id = :r ORDER BY attempt.attempt_no, event.id
    """), {"r": request_id}).all()
    screenshots = connection.execute(text("""
        SELECT artifact.id, artifact.capture_id, artifact.ordinal
        FROM artifact JOIN capture ON capture.id = artifact.capture_id
        WHERE capture.request_id = :r AND artifact.kind = 'screenshot'
        ORDER BY capture.captured_at, artifact.ordinal
    """), {"r": request_id}).all()
    return {
        **body,
        "events": [{
            "id": row.id, "attempt": row.attempt_no, "at": row.at.isoformat(), "elapsed_ms": row.elapsed_ms,
            "duration_ms": row.duration_ms, "kind": row.kind, "level": row.level, "phase": row.phase,
            "detail": row.detail, "failed": row.failed,
        } for row in events],
        "screenshots": [{"id": row.id, "capture_id": row.capture_id, "ordinal": row.ordinal} for row in screenshots],
    }


@router.get("/requests/{request_id}/result")
def request_result(request_id: str, connection: Db) -> dict:
    row = connection.execute(text("""
        SELECT document_key FROM extraction WHERE request_id = :r AND document_key IS NOT NULL
        ORDER BY extracted_at DESC LIMIT 1
    """), {"r": request_id}).one_or_none()
    if row is None:
        raise HTTPException(404, {"code": "no_result"})
    return json.loads(blobs.open_store().get(row.document_key))


@router.get("/artifacts/{artifact_id}")
def screenshot(artifact_id: str, connection: Db) -> Response:
    row = connection.execute(text("""
        SELECT blob_key, content_type FROM artifact WHERE CAST(id AS text) = :a AND kind = 'screenshot'
    """), {"a": artifact_id}).one_or_none()
    if row is None:
        raise HTTPException(404, {"code": "not_found"})
    return Response(blobs.open_store().get(row.blob_key), media_type=row.content_type,
                    headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
