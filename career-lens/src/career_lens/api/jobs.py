"""Reads of LinkedIn job postings and keyword job searches."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from ..capture.urls import UrlError, canonical, classify
from ..linkedin.jobs import (
    SCHEMA_VERSION, JobDocumentCorrupt, JobDocumentMissing, load_document,
)
from ..store import blobs
from . import consumer
from .v1 import database

router = APIRouter(prefix="/v1", responses=consumer.ERROR_RESPONSES)

ACTIVE = ("queued", "assigned", "running")


@router.get("/linkedin-job")
def linkedin_job(url: str, connection: Connection = Depends(database)):
    """The newest reading of one job posting, with its full description."""
    try:
        normalised = canonical(url)
    except UrlError as exc:
        raise HTTPException(400, str(exc)) from exc
    if classify(normalised) != "linkedin_job":
        raise HTTPException(400, f"{normalised} is not a LinkedIn job posting")
    row = connection.execute(text("""
        SELECT request_id, extraction_id, observed_at
        FROM linkedin.job_observation WHERE job_url=:url
        ORDER BY observed_at DESC, id DESC LIMIT 1
    """), {"url": normalised}).one_or_none()
    if row is None:
        raise HTTPException(404, f"no reading of {normalised}")
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": row.request_id,
        "observed_at": row.observed_at.isoformat(),
        "job": _document(connection, row.extraction_id),
    }


@router.get("/linkedin-job-search/{request_id}")
def linkedin_job_search(request_id: str,
                        connection: Connection = Depends(database)):
    """One job search request: still running, failed with its cause, or its jobs.

    A failed search has no result, so it is never mistaken for a search that
    found no jobs.
    """
    row = connection.execute(text("""
        SELECT request.id, request.kind, request.status, request.url_normalised,
               request.satisfied_at, attempt.cause, attempt.cause_detail
        FROM scrape_request request
        LEFT JOIN LATERAL (
            SELECT cause, cause_detail FROM scrape_attempt
            WHERE request_id=request.id ORDER BY attempt_no DESC LIMIT 1
        ) attempt ON TRUE
        WHERE request.id=:request
    """), {"request": request_id}).one_or_none()
    if row is None or row.kind != "linkedin_job_search":
        raise HTTPException(404, f"no job search request {request_id}")
    body = {
        "schema_version": SCHEMA_VERSION,
        "request_id": row.id,
        "status": row.status,
        "search_url": row.url_normalised,
        "failure": ({"cause": row.cause, "detail": row.cause_detail}
                    if row.cause else None),
        "result": None,
    }
    if row.status in ACTIVE:
        return JSONResponse(body, status_code=202, headers={
            "Location": f"/v1/linkedin-job-search/{request_id}",
            "Retry-After": "2"})
    if row.status == "succeeded":
        extraction = connection.execute(text("""
            SELECT extraction_id FROM linkedin.job_search_observation
            WHERE request_id=:request ORDER BY id DESC LIMIT 1
        """), {"request": request_id}).scalar_one_or_none()
    elif row.status == "satisfied":
        extraction = connection.execute(text("""
            SELECT extraction_id FROM linkedin.job_search_observation
            WHERE search_url=:url AND observed_at <= :at
            ORDER BY observed_at DESC, id DESC LIMIT 1
        """), {"url": row.url_normalised, "at": row.satisfied_at}
        ).scalar_one_or_none()
    else:
        return body
    if extraction is None:
        raise HTTPException(503, f"job search {request_id} has no stored result")
    body["result"] = _document(connection, extraction)
    return body


def _document(connection: Connection, extraction_id: str) -> dict:
    try:
        return load_document(connection, extraction_id=extraction_id,
                             store=blobs.open_store())
    except (JobDocumentMissing, JobDocumentCorrupt) as exc:
        raise HTTPException(503, str(exc)) from exc
