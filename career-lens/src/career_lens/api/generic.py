"""Asynchronous public reads for generic URL capture results."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import Connection

from ..capture.urls import UrlError, canonical
from ..generic.read import (
    GenericRequestMissing,
    GenericResultUnavailable,
    GenericTextUnavailable,
    url_at,
    url_result,
)
from ..generic.store import GenericDocumentCorrupt
from ..observe import resolve
from ..store import blobs
from .v1 import database
from .consumer import detailed_result
from . import consumer


router = APIRouter(prefix="/v1/url", responses=consumer.ERROR_RESPONSES)


@router.get("", response_model=consumer.PageQueryResponse)
def generic_url_at(
    url: str,
    at: datetime | None = None,
    include_screenshots: bool = False,
    include_markdown: bool = False,
    connection: Connection = Depends(database),
):
    """The newest reading of this page at or before `at`.

    The question a caller actually has -- "what does this page hold" -- rather
    than "what did that one run return", which is the route below. A page has
    no sections, so there is nothing to walk: one indexed lookup on
    (url_normalised, observed_at DESC) is the whole resolution, the same shape
    the Google route uses for a query.
    """
    try:
        normalised = canonical(url)
    except UrlError as exc:
        raise HTTPException(400, str(exc)) from exc
    moment = resolve.moment(at)
    try:
        answer = url_at(
            connection, normalised, at=moment, open_store=blobs.open_store,
            include_screenshots=include_screenshots,
            include_markdown=include_markdown)
    except GenericRequestMissing as exc:
        raise HTTPException(404, str(exc)) from exc
    except (GenericDocumentCorrupt, GenericResultUnavailable,
            GenericTextUnavailable) as exc:
        raise HTTPException(503, str(exc)) from exc
    return detailed_result(answer)


@router.get("/{request_id}", response_model=consumer.PageResponse,
            responses={202: {"model": consumer.PendingResultResponse}})
def generic_url_result(
    request_id: str,
    include_screenshots: bool = False,
    include_markdown: bool = False,
    connection: Connection = Depends(database),
):
    try:
        answer = url_result(
            connection, request_id, open_store=blobs.open_store,
            include_screenshots=include_screenshots,
            include_markdown=include_markdown)
    except GenericRequestMissing as exc:
        raise HTTPException(404, str(exc)) from exc
    except (
        GenericDocumentCorrupt,
        GenericResultUnavailable,
        GenericTextUnavailable,
    ) as exc:
        raise HTTPException(503, str(exc)) from exc
    if answer.pending:
        return JSONResponse(
            detailed_result(answer.document), status_code=202,
            headers={"Location": f"/v1/url/{request_id}", "Retry-After": "2"})
    return detailed_result(answer.document)
