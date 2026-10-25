"""Deterministically project canonical Google search JSON into PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from career_lens.google.contract import SCHEMA_VERSION, validate_document


EXTRACTOR = "google_search_json"


class ProjectionConflict(RuntimeError):
    """Core identity or an existing projection disagrees with the document."""


def project(connection: Connection, *, extraction_id: str, request_id: str,
            document: dict) -> int:
    """Insert and return the exact append-only projection of ``document``.

    Repeating the same projection is idempotent. Reusing an extraction for a
    different document is refused because append-only evidence cannot be
    reconciled by mutation.
    """
    canonical = validate_document(document)
    _verify_core_identity(
        connection, extraction_id=extraction_id, request_id=request_id,
        document=canonical)
    observation = _observation_values(
        extraction_id=extraction_id, request_id=request_id,
        document=canonical)
    results = [_result_values(result) for result in canonical["results"]]

    existing_id = connection.execute(text("""
        SELECT id FROM google.search_observation
        WHERE extraction_id=:extraction_id
    """), {"extraction_id": extraction_id}).scalar_one_or_none()
    if existing_id is not None:
        _assert_existing_matches(connection, existing_id, observation, results)
        return existing_id

    observation_id = connection.execute(text("""
        INSERT INTO google.search_observation
            (extraction_id, request_id, serp_capture_id, query_requested,
             query_normalised, requested_top_n, results_examined, google_host,
             serp_url, browser_locale, browser_timezone, searched_at, outcome,
             failure_kind, failure_detail, examined, selected, captured,
             partial, failed, skipped)
        VALUES
            (:extraction_id, :request_id, :serp_capture_id, :query_requested,
             :query_normalised, :requested_top_n, :results_examined,
             :google_host, :serp_url, :browser_locale, :browser_timezone,
             :searched_at, :outcome, :failure_kind, :failure_detail, :examined,
             :selected, :captured, :partial, :failed, :skipped)
        ON CONFLICT (extraction_id) DO NOTHING
        RETURNING id
    """), observation).scalar_one_or_none()
    if observation_id is None:
        observation_id = connection.execute(text("""
            SELECT id FROM google.search_observation
            WHERE extraction_id=:extraction_id
        """), {"extraction_id": extraction_id}).scalar_one()
        _assert_existing_matches(connection, observation_id, observation,
                                 results)
        return observation_id

    for result in results:
        connection.execute(text("""
            INSERT INTO google.search_result
                (observation_id, rank, url, normalised_url, host, title,
                 snippet, selected, selection_reason, capture_id, capture_mode,
                 capture_status, failure_kind, failure_detail)
            VALUES
                (:observation_id, :rank, :url, :normalised_url, :host, :title,
                 :snippet, :selected, :selection_reason, :capture_id,
                 :capture_mode, :capture_status, :failure_kind,
                 :failure_detail)
        """), {"observation_id": observation_id, **result})
    return observation_id


def _verify_core_identity(connection: Connection, *, extraction_id: str,
                          request_id: str, document: dict) -> None:
    if document["request"]["request_id"] != request_id:
        raise ProjectionConflict("document request id does not match request")
    row = connection.execute(text("""
        SELECT request_id, extractor, extractor_version
        FROM extraction WHERE id=:id
    """), {"id": extraction_id}).mappings().one_or_none()
    if row is None:
        raise ProjectionConflict("extraction does not exist")
    if row["request_id"] != request_id:
        raise ProjectionConflict("extraction belongs to a different request")
    if row["extractor"] != EXTRACTOR or row["extractor_version"] != (
            SCHEMA_VERSION):
        raise ProjectionConflict(
            f"extraction must use {EXTRACTOR} version {SCHEMA_VERSION}")


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _observation_values(*, extraction_id: str, request_id: str,
                        document: dict) -> dict[str, Any]:
    search = document["search"]
    failure = document["failure"]
    serp = document["serp"]
    return {
        "extraction_id": extraction_id,
        "request_id": request_id,
        "serp_capture_id": serp["capture_id"] if serp else None,
        "query_requested": search["query"],
        "query_normalised": search["normalised_query"],
        "requested_top_n": search["requested_top_n"],
        "results_examined": search["results_examined"],
        "google_host": search["google_host"],
        "serp_url": search["serp_url"],
        "browser_locale": search["browser_locale"],
        "browser_timezone": search["browser_timezone"],
        "searched_at": _timestamp(search["searched_at"]),
        "outcome": document["outcome"],
        "failure_kind": failure["kind"] if failure else None,
        "failure_detail": failure["detail"] if failure else None,
        **document["summary"],
    }


def _result_values(result: dict) -> dict[str, Any]:
    capture = result["capture"]
    error = capture["error"] if capture else None
    return {
        "rank": result["rank"],
        "url": result["url"],
        "normalised_url": result["normalised_url"],
        "host": result["host"],
        "title": result["title"],
        "snippet": result["snippet"],
        "selected": result["selection"]["selected"],
        "selection_reason": result["selection"]["reason"],
        "capture_id": capture["capture_id"] if capture else None,
        "capture_mode": capture["mode"] if capture else None,
        "capture_status": capture["status"] if capture else None,
        "failure_kind": error["kind"] if error else None,
        "failure_detail": error["detail"] if error else None,
    }


def _assert_existing_matches(connection: Connection, observation_id: int,
                             expected_observation: dict[str, Any],
                             expected_results: list[dict[str, Any]]) -> None:
    columns = tuple(expected_observation)
    actual_observation = connection.execute(text(f"""
        SELECT {', '.join(columns)} FROM google.search_observation WHERE id=:id
    """), {"id": observation_id}).mappings().one()
    if dict(actual_observation) != expected_observation:
        raise ProjectionConflict("existing observation differs from document")

    result_columns = tuple(_result_values({
        "rank": 1, "url": "x", "normalised_url": "x", "host": "x",
        "title": "", "snippet": "", "selection": {
            "selected": False, "reason": "beyond_top_n"}, "capture": None,
    }))
    actual_results = connection.execute(text(f"""
        SELECT {', '.join(result_columns)} FROM google.search_result
        WHERE observation_id=:id ORDER BY rank
    """), {"id": observation_id}).mappings().all()
    if [dict(row) for row in actual_results] != expected_results:
        raise ProjectionConflict("existing results differ from document")
