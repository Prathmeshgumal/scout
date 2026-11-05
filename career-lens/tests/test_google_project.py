"""Canonical Google JSON projects exactly once into queryable rows."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from career_lens.google.contract import InvalidGoogleDocument
from career_lens.google.project import ProjectionConflict, project
from career_lens.store.ids import EXTRACTION, new_id


GOLDEN = Path(__file__).parent / "fixtures" / "google"
NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)


@pytest.fixture(params=["success", "partial", "failed"])
def document(request) -> dict:
    return json.loads((GOLDEN / f"{request.param}.json").read_text())


def _insert_facts(connection, document: dict, *, extractor="google_search_json",
                  version="1.0", request_id: str | None = None,
                  extraction_request_id: str | None = None):
    request_id = request_id or document["request"]["request_id"]
    extraction_request_id = extraction_request_id or request_id
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:id, :query, :normalised, :top_n,
                'google_search', :status, :now)
    """), {
        "id": request_id,
        "query": document["search"]["query"],
        "normalised": document["search"]["normalised_query"],
        "top_n": document["search"]["requested_top_n"],
        "status": document["request"]["status"],
        "now": NOW,
    })
    capture_ids = []
    if document["serp"] is not None:
        capture_ids.append(("serp", document["serp"]["capture_id"],
                            document["serp"]["url_final"]))
    for result in document["results"]:
        capture = result["capture"]
        if capture is not None and capture["capture_id"] is not None:
            capture_ids.append((f"result-{result['rank']:03d}",
                                capture["capture_id"], capture["url_final"]))
    for section, capture_id, url in capture_ids:
        connection.execute(text("""
            INSERT INTO capture
                (id, request_id, section, url_requested, url_final, host,
                 captured_at)
            VALUES (:id, :request, :section, :url, :url, :host, :now)
        """), {
            "id": capture_id, "request": request_id, "section": section,
            "url": url, "host": document["search"]["google_host"],
            "now": NOW,
        })
    extraction_id = new_id(EXTRACTION)
    connection.execute(text("""
        INSERT INTO extraction
            (id, request_id, extractor, extractor_version, extracted_at,
             outcome, document_key, document_sha256, document_bytes)
        VALUES (:id, :request, :extractor, :version, :now,
                :outcome, :key, 'abc', 1)
    """), {
        "id": extraction_id, "request": extraction_request_id,
        "extractor": extractor, "version": version, "now": NOW,
        # A failed JSON envelope is normally synthesized by the API: core's
        # failed-extraction invariant forbids naming a document. Use partial
        # here only to prove the projector's complete mechanical mapping.
        "outcome": (
            "partial" if document["outcome"] == "failed"
            else document["outcome"]),
        "key": f"{extraction_id}.json",
    })
    return request_id, extraction_id


def _expected_result(result: dict) -> dict:
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


def test_all_golden_documents_project_exactly(app, document):
    request_id, extraction_id = _insert_facts(app, document)

    observation_id = project(
        app, extraction_id=extraction_id, request_id=request_id,
        document=document)

    row = app.execute(text("""
        SELECT * FROM google.search_observation WHERE id=:id
    """), {"id": observation_id}).mappings().one()
    search = document["search"]
    failure = document["failure"]
    assert {key: row[key] for key in (
        "extraction_id", "request_id", "query_requested",
        "query_normalised", "requested_top_n", "results_examined",
        "google_host", "serp_url", "browser_locale", "browser_timezone",
        "outcome", "failure_kind", "failure_detail", "examined", "selected",
        "captured", "partial", "failed", "skipped",
    )} == {
        "extraction_id": extraction_id,
        "request_id": request_id,
        "query_requested": search["query"],
        "query_normalised": search["normalised_query"],
        "requested_top_n": search["requested_top_n"],
        "results_examined": search["results_examined"],
        "google_host": search["google_host"],
        "serp_url": search["serp_url"],
        "browser_locale": search["browser_locale"],
        "browser_timezone": search["browser_timezone"],
        "outcome": document["outcome"],
        "failure_kind": failure["kind"] if failure else None,
        "failure_detail": failure["detail"] if failure else None,
        **document["summary"],
    }
    assert row["serp_capture_id"] == (
        document["serp"]["capture_id"] if document["serp"] else None)
    assert row["searched_at"].isoformat().replace("+00:00", "Z") == (
        search["searched_at"])
    results = app.execute(text("""
        SELECT rank, url, normalised_url, host, title, snippet, selected,
               selection_reason, capture_id, capture_mode, capture_status,
               failure_kind, failure_detail
        FROM google.search_result WHERE observation_id=:id ORDER BY rank
    """), {"id": observation_id}).mappings().all()
    assert [dict(result) for result in results] == [
        _expected_result(result) for result in document["results"]]


def test_invalid_document_inserts_nothing(app, document):
    request_id, extraction_id = _insert_facts(app, document)
    document["summary"]["examined"] += 1
    with pytest.raises(InvalidGoogleDocument):
        project(app, extraction_id=extraction_id, request_id=request_id,
                document=document)
    assert app.execute(text(
        "SELECT count(*) FROM google.search_observation")).scalar_one() == 0


@pytest.mark.parametrize("mismatch", ["document", "extraction", "extractor",
                                      "version"])
def test_core_identity_and_extractor_must_match(app, document, mismatch):
    actual = copy.deepcopy(document)
    request_id = actual["request"]["request_id"]
    if mismatch == "document":
        actual["request"]["request_id"] = "rq-different-document"
    other_request = None
    if mismatch == "extraction":
        other_request = "rq-different-extraction"
        app.execute(text("""
            INSERT INTO scrape_request
                (id, query_requested, query_normalised, google_top_n,
                 kind, status, requested_at)
            VALUES (:id, 'other', 'other', 0, 'google_search', 'failed', :now)
        """), {"id": other_request, "now": NOW})
    extractor = "other" if mismatch == "extractor" else "google_search_json"
    version = "2.0" if mismatch == "version" else "1.0"
    request_id, extraction_id = _insert_facts(
        app, document, extractor=extractor, version=version,
        extraction_request_id=other_request)
    with pytest.raises(ProjectionConflict):
        project(app, extraction_id=extraction_id, request_id=request_id,
                document=actual)
    assert app.execute(text(
        "SELECT count(*) FROM google.search_observation")).scalar_one() == 0


def test_exact_existing_projection_is_idempotent(app, document):
    request_id, extraction_id = _insert_facts(app, document)
    first = project(app, extraction_id=extraction_id, request_id=request_id,
                    document=document)
    second = project(app, extraction_id=extraction_id, request_id=request_id,
                     document=copy.deepcopy(document))
    assert second == first
    assert app.execute(text(
        "SELECT count(*) FROM google.search_observation")).scalar_one() == 1
    assert app.execute(text(
        "SELECT count(*) FROM google.search_result")).scalar_one() == len(
            document["results"])


def test_mismatched_existing_projection_is_refused_without_mutation(
        app, document):
    request_id, extraction_id = _insert_facts(app, document)
    observation_id = project(
        app, extraction_id=extraction_id, request_id=request_id,
        document=document)
    changed = copy.deepcopy(document)
    if changed["results"]:
        changed["results"][0]["snippet"] += " changed"
    else:
        changed["failure"]["detail"] += " changed"
    with pytest.raises(ProjectionConflict):
        project(app, extraction_id=extraction_id, request_id=request_id,
                document=changed)
    assert app.execute(text("""
        SELECT query_requested FROM google.search_observation WHERE id=:id
    """), {"id": observation_id}).scalar_one() == document["search"]["query"]
