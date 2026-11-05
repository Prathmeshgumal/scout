"""Asynchronous public reads for canonical Google search results."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from ..google.contract import normalise_lookup_top_n, normalise_query
from ..google.read import (
    GoogleDocumentCorrupt,
    GoogleDocumentUnavailable,
    GoogleRequestMissing,
    search_result,
)
from ..observe import resolve
from ..store.blobs import open_store
from .v1 import database
from .consumer import detailed_result
from . import consumer


router = APIRouter(prefix="/v1/google-search", responses=consumer.ERROR_RESPONSES)


@router.get("", response_model=consumer.GoogleQueryResponse)
def google_search_by_query(
    query: str,
    top_n: int,
    at: datetime | None = None,
    connection: Connection = Depends(database),
):
    """The newest ranking for this query at or before `at`.

    A SERP has no sections, so there is nothing to walk: one indexed lookup on
    (query_normalised, searched_at DESC) is the whole resolution.

    `top_n` is part of the identity rather than an attribute -- a stored top
    two is not an answer to a top five question, which is the same rule
    uq_scrape_request_outstanding_google already applies to deduplication.
    Read against the stored ceiling rather than the request one, so a search
    run before the request cap came down can still be asked for by name.

    Distinct from the by-request-id route below, which answers "what did that
    one run return". That is not a temporal question and has nothing for `at`
    to attach to.
    """
    moment = resolve.moment(at)
    _, identity = normalise_query(query)
    count = normalise_lookup_top_n(top_n)
    observation = connection.execute(text("""
        SELECT id, searched_at, query_requested, requested_top_n
        FROM google.search_observation
        WHERE query_normalised = :q AND requested_top_n = :n
          AND searched_at <= :at
          AND failure_kind IS NULL
        ORDER BY searched_at DESC, id DESC LIMIT 1
    """), {"q": identity, "n": count, "at": moment}).one_or_none()
    if observation is None:
        when = "" if at is None else f" by {moment.isoformat()}"
        raise HTTPException(
            404,
            f"no search for {query!r} at top {count} had been observed{when}")
    results = connection.execute(text("""
        SELECT rank, url, normalised_url, host, title, snippet, selected,
               selection_reason
        FROM google.search_result WHERE observation_id = :o ORDER BY rank
    """), {"o": observation.id}).all()
    return {
        "query": observation.query_requested,
        "top_n": observation.requested_top_n,
        "as_of": moment.isoformat(),
        "observed_at": observation.searched_at.isoformat(),
        "results": [dict(row._mapping) for row in results],
    }


@router.get("/{request_id}", response_model=consumer.GoogleResultResponse,
            responses={202: {"model": consumer.PendingResultResponse}})
def google_search_result(
    request_id: str,
    include_screenshots: bool = False,
    connection: Connection = Depends(database),
):
    try:
        answer = search_result(
            connection, request_id, open_store=open_store,
            include_screenshots=include_screenshots)
    except GoogleRequestMissing as exc:
        raise HTTPException(404, str(exc)) from exc
    except GoogleDocumentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except GoogleDocumentCorrupt as exc:
        raise HTTPException(
            503, "Google search document failed integrity verification") from exc
    if answer.pending:
        return JSONResponse(
            detailed_result(answer.document), status_code=202,
            headers={"Location": f"/v1/google-search/{request_id}", "Retry-After": "2"})
    return detailed_result(answer.document)
