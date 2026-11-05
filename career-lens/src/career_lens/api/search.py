"""Compact sync-first Google SERP search for API consumers."""

from __future__ import annotations

import time
from contextlib import closing
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from ..google.contract import InvalidGoogleRequest
from ..google.read import (
    GoogleDocumentCorrupt,
    GoogleDocumentUnavailable,
    GoogleRequestMissing,
    SearchResult,
    search_result,
)
from ..observe.freshness import InvalidFreshness
from ..store.blobs import open_store
from ..store.db import connect
from . import requests as submission
from .consumer import safe_failure
from . import consumer
from .security import consumer_key


router = APIRouter(prefix="/v1/search", responses=consumer.ERROR_RESPONSES)
RETRY_AFTER_SECONDS = 2
POLL_INTERVAL_SECONDS = 0.1


class SearchRequest(BaseModel):
    """The intentionally small consumer search contract."""

    model_config = ConfigDict(extra="forbid")

    query: StrictStr
    wait_seconds: Annotated[StrictInt, Field(ge=0, le=30)] = 20
    priority: Literal["normal", "high"] = "normal"
    freshness: StrictStr | None = None


def _read(request_id: str) -> SearchResult:
    """Read durable state without retaining a connection between polls."""
    with closing(connect()) as connection:
        return search_result(
            connection,
            request_id,
            open_store=open_store,
            include_screenshots=False,
        )


def _read_or_http_error(request_id: str) -> SearchResult:
    try:
        return _read(request_id)
    except GoogleRequestMissing as exc:
        raise HTTPException(404, str(exc)) from exc
    except GoogleDocumentUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except GoogleDocumentCorrupt as exc:
        raise HTTPException(
            503,
            "Google search document failed integrity verification",
        ) from exc


def _pending(document: dict) -> dict:
    request_id = document["request_id"]
    return {
        "request_id": request_id,
        "status": document["status"],
        "query": document["search"]["query"],
        "result_url": f"/v1/search/{request_id}",
        "retry_after": RETRY_AFTER_SECONDS,
        "results": None,
    }


def _terminal(document: dict) -> dict:
    return {
        "request_id": document["request"]["request_id"],
        "status": document["request"]["status"],
        "query": document["search"]["query"],
        "observed_at": document["search"]["searched_at"],
        "outcome": document["outcome"],
        "failure": safe_failure(document["failure"]) if document.get("failure") else None,
        "results": [
            {
                "rank": result["rank"],
                "title": result["title"],
                "url": result["url"],
                "snippet": result["snippet"],
            }
            for result in document["results"]
        ],
    }


def _response(answer: SearchResult):
    if not answer.pending:
        return _terminal(answer.document)
    body = _pending(answer.document)
    return JSONResponse(
        body,
        status_code=202,
        headers={
            "Location": body["result_url"],
            "Retry-After": str(RETRY_AFTER_SECONDS),
        },
    )


def read_compact_search(request_id: str, *, include_captures: bool = False) -> dict:
    """Shared read projection for search-based consumer features."""
    answer = _read_or_http_error(request_id)
    result = _pending(answer.document) if answer.pending else _terminal(answer.document)
    if include_captures:
        from ..google.progress import read_progress
        from ..store.config import artifact_url_ttl_seconds
        from sqlalchemy import text

        with closing(connect()) as connection:
            document = answer.document
            if answer.pending or not document.get("results"):
                snapshot = read_progress(connection, request_id)
                if snapshot:
                    document = snapshot
            result["requested_top_n"] = answer.document["search"]["requested_top_n"]
            if document.get("results") is not None:
                result["observed_at"] = document["search"]["searched_at"]
                result["results"] = []
                for item in document["results"]:
                    capture = ({key: item["capture"].get(key)
                                for key in ("capture_id", "status", "error")}
                               if item.get("capture") else None)
                    entry = {key: item[key] for key in ("rank", "title", "url", "snippet")}
                    entry.update(selected=item["selection"]["selected"],
                                 selection_reason=item["selection"]["reason"], capture=capture)
                    result["results"].append(entry)
                captures = {item["capture"]["capture_id"]: item["capture"]
                            for item in result["results"] if item.get("capture")
                            and item["capture"].get("capture_id")}
                if captures:
                    content = connection.execute(text("""
                        SELECT id, request_id, markdown FROM capture WHERE id=ANY(:ids)
                    """), {"ids": list(captures)}).mappings()
                    for row in content:
                        captures[row["id"]].update(
                            request_id=row["request_id"], markdown=row["markdown"],
                            has_markdown=row["markdown"] is not None)
                    store = open_store()
                    rows = connection.execute(text("""
                        SELECT a.capture_id, a.kind, a.ordinal, a.blob_key,
                               c.request_id, c.markdown
                        FROM artifact a JOIN capture c ON c.id=a.capture_id
                        WHERE a.capture_id=ANY(:ids) AND a.kind IN ('html', 'screenshot')
                        ORDER BY a.capture_id, a.kind, a.ordinal
                    """), {"ids": list(captures)}).mappings()
                    for row in rows:
                        target = captures[row["capture_id"]]
                        target["request_id"] = row["request_id"]
                        target["has_markdown"] = row["markdown"] is not None
                        target["markdown"] = row["markdown"]
                        target.setdefault("artifacts", []).append({
                            "kind": row["kind"], "ordinal": row["ordinal"],
                            "url": store.url(row["blob_key"], artifact_url_ttl_seconds()),
                        })
    return result


@router.post("", response_model=consumer.SearchResponse,
             responses={202: {"model": consumer.SearchResponse}})
def submit_search(body: SearchRequest, key: str | None = Depends(consumer_key)):
    """Submit a first-page SERP search and wait briefly for its result."""
    try:
        answer, _, _ = submission.submit_google(
            body.query,
            top_n=0,
            priority=body.priority,
            freshness_window=body.freshness,
            api_key_id=key,
        )
    except (InvalidGoogleRequest, InvalidFreshness) as exc:
        raise HTTPException(400, str(exc)) from exc

    deadline = time.monotonic() + body.wait_seconds
    while True:
        result = _read_or_http_error(answer.request_id)
        if not result.pending:
            return _terminal(result.document)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _response(result)
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


@router.get("/{request_id}", response_model=consumer.SearchResponse,
            responses={202: {"model": consumer.SearchResponse}})
def get_search(request_id: str):
    """Return a compact result, or stable polling instructions if active."""
    return _response(_read_or_http_error(request_id))
