"""Shared entity reads, without exposing capture pinning to consumers."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import Connection

from ..observe import resolve
from . import consumer, v1

router = APIRouter(prefix="/v1", responses=consumer.ERROR_RESPONSES)
def linkedin_response(body, connection, kind, cutoff):
    latest = resolve.walk(
        connection, kind=kind, key=body["entity"]["url"], at=cutoff, allow_stale=False
    )
    for name, item in body.get("meta", {}).items():
        reading = latest.get(name)
        if (
            reading
            and reading.grade == resolve.FAILED
            and reading.observed_at
            and item.get("observed_at")
            and reading.observed_at > datetime.fromisoformat(item["observed_at"])
        ):
            item["fallback"] = {
                "status": reading.status,
                "observed_at": reading.observed_at.isoformat(),
            }
    return consumer.entity_response(body, cutoff=cutoff)


def no_capture(request: Request):
    if "capture" in request.query_params:
        raise HTTPException(400, "capture pinning is not available")


@router.get(
    "/linkedin-individual",
    response_model=consumer.EntityResponse,
    dependencies=[Depends(no_capture)],
)
def person(
    url: str,
    at: datetime | None = None,
    allow_stale: bool = True,
    connection: Connection = Depends(v1.database),
):
    cutoff = resolve.moment(at)
    return linkedin_response(
        v1.linkedin_individual(url, cutoff, allow_stale, None, connection),
        connection,
        resolve.PERSON,
        cutoff,
    )


@router.get(
    "/linkedin-company",
    response_model=consumer.EntityResponse,
    dependencies=[Depends(no_capture)],
)
def company(
    url: str,
    at: datetime | None = None,
    allow_stale: bool = True,
    connection: Connection = Depends(v1.database),
):
    cutoff = resolve.moment(at)
    return linkedin_response(
        v1.linkedin_company(url, cutoff, allow_stale, None, connection),
        connection,
        resolve.COMPANY,
        cutoff,
    )


