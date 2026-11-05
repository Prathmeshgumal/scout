"""Structured entity data, scrape requests and request status.

Nothing that renders a captured page appears here. Raw captures carry
third-party personal data.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from ..capture.urls import UrlError, canonical, job_search_url
from ..capture.options import InvalidCaptureOption, job_search_terms
from ..google.contract import InvalidGoogleRequest
from ..observe.freshness import InvalidFreshness
from . import requests as submission, serialise
from .db import database, write
from .security import consumer_key
from . import consumer

router = APIRouter(prefix="/v1", responses=consumer.ERROR_RESPONSES)

SCRAPE_PATHS = {
    "linkedin-individual": "linkedin_person",
    "linkedin-company": "linkedin_company",
    "linkedin-job": "linkedin_job",
    "url": "generic",
}

def _submitted(answer: submission.Submission, target: dict) -> JSONResponse:
    """Report the durable state and this caller's freshness assessment."""
    body = consumer.SubmissionResponse(
        request_id=answer.request_id,
        status="satisfied" if answer.satisfied_by else answer.status,
        target=target,
        requested_sections=([consumer.section_path(name, target["kind"]) for name in answer.sections]
                            if answer.sections is not None else None),
        status_url=f"/v1/requests/{answer.request_id}",
        data_url=consumer.data_url(target["kind"], target.get("url"), answer.request_id),
        coverage=answer.coverage,
    ).model_dump()
    return _request_response(body)


def _consumer_request(connection, request_id):
    body = serialise.scrape_request(connection, request_id)
    if body is None:
        raise HTTPException(404, f"no request {request_id}")
    selection = body.get("linkedin_sections")
    sections = (None if body["kind"] == "google_search" else
                [consumer.section_path(name, body["kind"]) for name in
                 submission._freshness_roles(body["kind"], selection)])
    return consumer.request_response(body, sections=sections)


def _request_response(body):
    active = body["status"] in consumer.ACTIVE
    return JSONResponse(body, status_code=202 if active else 200,
                        headers={"Location": body["status_url"], "Retry-After": "2"}
                        if active else None)


@router.post("/scrape/{path}", status_code=202, response_model=consumer.SubmissionResponse,
             responses={200: {"model": consumer.SubmissionResponse}})
def scrape(path: str, body: dict,
           key: str = Depends(consumer_key)) -> dict:
    return _scrape(path, body, api_key_id=key)


def _scrape(path: str, body: dict, *, api_key_id: str | None = None) -> dict:
    supplied = body or {}
    if path == "linkedin-job-search":
        return _submit_job_search(supplied, api_key_id)
    if path not in SCRAPE_PATHS and path != "google-search":
        raise HTTPException(404, f"no scrape endpoint {path!r}")
    allowed = {"url", "freshness", "max_attempts"}
    if path.startswith("linkedin-"):
        allowed.add("linkedin_sections")
    if path == "linkedin-company":
        allowed.add("job_pages")
    elif path == "google-search":
        allowed = {"query", "top_n", "freshness"}
    unknown = sorted(set(supplied) - allowed)
    if unknown:
        if "linkedin_google_search_chance" in unknown:
            raise HTTPException(400, "linkedin_google_search_chance is configured on the worker")
        raise HTTPException(400, f"unsupported submission fields: {', '.join(unknown)}")
    return _submit_scrape(path, supplied, api_key_id)


JOB_SEARCH_FIELDS = {"keywords", "location", "posted_within", "workplace_types",
                     "job_pages", "freshness", "max_attempts"}


def _submit_job_search(body: dict, api_key_id: str | None) -> dict:
    unknown = sorted(set(body) - JOB_SEARCH_FIELDS)
    if unknown:
        raise HTTPException(
            400, f"unsupported submission fields: {', '.join(unknown)}")
    try:
        url = job_search_url(job_search_terms(
            body.get("keywords"), body.get("location"),
            body.get("posted_within"), body.get("workplace_types")))
        answer = submission.submit(
            url, expected_kind="linkedin_job_search",
            job_pages=body.get("job_pages"),
            max_attempts=body.get("max_attempts"),
            freshness_window=body.get("freshness"), api_key_id=api_key_id)
    except submission.RequestOptionsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (UrlError, submission.KindMismatch, InvalidCaptureOption,
            InvalidFreshness) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _submitted(answer, {"kind": "linkedin_job_search", "url": url})


def _submit_scrape(path: str, body: dict, api_key_id: str | None) -> dict:
    if path == "google-search":
        supplied = body or {}
        allowed = {"query", "top_n", "label", "tempo", "freshness"}
        unknown = sorted(set(supplied) - allowed)
        if unknown:
            raise HTTPException(
                400, f"unknown Google search fields: {', '.join(unknown)}")
        try:
            answer, query, top_n = submission.submit_google(
                supplied.get("query"), top_n=supplied.get("top_n"),
                label=supplied.get("label"), tempo=supplied.get("tempo"),
                freshness_window=supplied.get("freshness"),
                api_key_id=api_key_id,
            )
        except (
            InvalidCaptureOption,
            InvalidGoogleRequest,
            InvalidFreshness,
            submission.InvalidLabel,
        ) as exc:
            raise HTTPException(400, str(exc)) from exc
        return _submitted(answer, {
            "kind": "google_search", "query": query, "top_n": top_n,
        })

    kind = SCRAPE_PATHS.get(path)
    if kind is None:
        raise HTTPException(404, f"no scrape endpoint {path!r}")
    supplied = body or {}
    if "linkedin_google_search_chance" in supplied:
        raise HTTPException(
            400,
            "linkedin_google_search_chance is configured on the worker",
        )
    url = supplied.get("url")
    try:
        answer = submission.submit(
            url or "", label=supplied.get("label"), expected_kind=kind,
            tempo=supplied.get("tempo"),
            linkedin_sections=supplied.get("linkedin_sections"),
            job_pages=supplied.get("job_pages"),
            max_attempts=supplied.get("max_attempts"),
            freshness_window=supplied.get("freshness"),
            api_key_id=api_key_id)
        normalised = canonical(url)
    except submission.RequestOptionsConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except (UrlError, submission.KindMismatch, submission.InvalidLabel,
            InvalidCaptureOption, InvalidFreshness) as exc:
        raise HTTPException(400, str(exc)) from exc
    return _submitted(answer, {"kind": kind, "url": normalised})


@router.get("/requests/{request_id}", response_model=consumer.RequestResponse,
            responses={202: {"model": consumer.RequestResponse}})
def one_request(request_id: str,
                connection: Connection = Depends(database)) -> dict:
    return _request_response(_consumer_request(connection, request_id))


#: The statuses a cancel can act on. Everything else is already an ending.
CANCELLABLE = ("queued", "assigned", "running")


@router.post("/requests/{request_id}/cancel", response_model=consumer.CancelResponse,
             responses={202: {"model": consumer.CancelResponse}})
def withdraw_request(request_id: str,
                     connection: Connection = Depends(database),
                     key: str = Depends(consumer_key)) -> JSONResponse:
    """Withdraw this key's interest in a request, and stop the work when no
    caller wants it any more.

    One request serves every caller that asked for the same target, so a
    consumer may not stop work that another caller still waits for. A request
    that this key did not ask for is a 404, as if it did not exist: a key may
    not learn about other callers' work. submit() reads the row FOR KEY SHARE,
    so no new interest arrives between the count and the stop.
    """
    with write(connection) as writable:
        row = writable.execute(text("""
            SELECT request.status, request.cancel_requested_at
            FROM scrape_request request
            JOIN request_interest interest
              ON interest.request_id = request.id AND interest.api_key_id = :key
            WHERE request.id = :r
            FOR UPDATE OF request
        """), {"r": request_id, "key": key}).one_or_none()
        if row is None:
            raise HTTPException(404, f"no request {request_id}")
        if row.status not in CANCELLABLE:
            raise HTTPException(
                409, f"request {request_id} is {row.status}, not cancellable")
        writable.execute(text("""
            UPDATE request_interest SET withdrawn_at = :now
            WHERE request_id = :r AND api_key_id = :key AND withdrawn_at IS NULL
        """), {"r": request_id, "key": key, "now": datetime.now(UTC)})
        wanted = writable.execute(text("""
            SELECT EXISTS (SELECT 1 FROM request_interest
                           WHERE request_id = :r AND withdrawn_at IS NULL)
        """), {"r": request_id}).scalar_one()
        if wanted:
            code, outcome = 200, "withdrawn"
        else:
            code = _stop(writable, request_id, row,
                         detail="every caller withdrew this request")
            outcome = "cancelled" if code == 200 else "stopping"
        body = {**_consumer_request(writable, request_id), "cancel": outcome}
    return JSONResponse(body, status_code=code)


def _stop(writable, request_id: str, row, *, detail: str) -> int:
    """Stop an outstanding request that the caller has locked FOR UPDATE.
    Returns 200 when the request is closed here, 202 when its worker closes it.

    Whoever holds the work closes it. Nothing holds a `queued` request, so this
    closes it here. `assigned` and `running` work records the intent and returns
    202; its worker writes the ending, keeping the attempt cause and request
    status together.
    """
    if row.cancel_requested_at is None:
        writable.execute(text("""
            UPDATE scrape_request SET cancel_requested_at=:now WHERE id=:r
        """), {"r": request_id, "now": datetime.now(UTC)})
    if row.status == "queued":
        writable.execute(text("""
            UPDATE scrape_request
            SET status='cancelled', next_attempt_at=NULL WHERE id=:r
        """), {"r": request_id})
        return 200
    return 202


def _nothing_observed(target: str, at: datetime | None) -> str:
    """A 404 that says which question came back empty.

    The instant matters: "nothing for this profile" and "nothing for this
    profile by March" are different answers, and a caller walking backwards
    through time needs to tell them apart.
    """
    if at is None:
        return f"nothing has been observed for {target}"
    return f"nothing had been observed for {target} by {at.isoformat()}"


def linkedin_individual(url: str,
                        at: datetime | None = None,
                        allow_stale: bool = True,
                        capture: str | None = None,
                        connection: Connection = Depends(database)) -> dict:
    try:
        normalised = canonical(url)
    except UrlError as exc:
        raise HTTPException(400, str(exc)) from exc
    body = serialise.person(connection, normalised, at=at,
                            allow_stale=allow_stale, capture_id=capture)
    if body is None:
        raise HTTPException(404, _nothing_observed(normalised, at))
    return body


def linkedin_company(url: str,
                     at: datetime | None = None,
                     allow_stale: bool = True,
                     capture: str | None = None,
                     connection: Connection = Depends(database)) -> dict:
    try:
        normalised = canonical(url)
    except UrlError as exc:
        raise HTTPException(400, str(exc)) from exc
    body = serialise.company(connection, normalised, at=at,
                             allow_stale=allow_stale, capture_id=capture)
    if body is None:
        raise HTTPException(404, _nothing_observed(normalised, at))
    return body


