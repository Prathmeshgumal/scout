"""Normalized Google observations preserve the canonical JSON invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id


NOW = datetime.now(UTC)


def _facts(connection):
    request_id = new_id(REQUEST)
    extraction_id = new_id(EXTRACTION)
    serp_id = new_id(CAPTURE)
    result_id = new_id(CAPTURE)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:request, 'Enterprise AI', 'enterprise ai', 1,
                'google_search', 'succeeded', :now)
    """), {"request": request_id, "now": NOW})
    connection.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, url_final, host, captured_at)
        VALUES
            (:serp, :request, 'serp', 'https://www.google.com/search?q=AI',
             'https://www.google.com/search?q=AI', 'www.google.com', :now),
            (:result, :request, 'result-001', 'https://example.com/article',
             'https://example.com/article', 'example.com', :now)
    """), {
        "serp": serp_id, "result": result_id,
        "request": request_id, "now": NOW,
    })
    connection.execute(text("""
        INSERT INTO extraction
            (id, request_id, extractor, extractor_version, extracted_at,
             outcome, document_key, document_sha256, document_bytes)
        VALUES (:extraction, :request, 'google_search_json', '1.0', :now,
                'ok', 'document.json', 'abc', 1)
    """), {
        "extraction": extraction_id, "request": request_id, "now": NOW,
    })
    return request_id, extraction_id, serp_id, result_id


def _observation(connection, **overrides) -> tuple[int, str]:
    request_id, extraction_id, serp_id, result_id = _facts(connection)
    values = {
        "extraction": extraction_id,
        "request": request_id,
        "serp": serp_id,
        "query": "Enterprise AI",
        "normalised": "enterprise ai",
        "top_n": 1,
        "results_examined": 1,
        "google_host": "www.google.com",
        "serp_url": "https://www.google.com/search?q=AI",
        "locale": "en-IN",
        "timezone": "Asia/Kolkata",
        "searched_at": NOW,
        "outcome": "ok",
        "failure_kind": None,
        "failure_detail": None,
        "examined": 1,
        "selected": 1,
        "captured": 1,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
    }
    values.update(overrides)
    observation_id = connection.execute(text("""
        INSERT INTO google.search_observation
            (extraction_id, request_id, serp_capture_id, query_requested,
             query_normalised, requested_top_n, results_examined, google_host,
             serp_url, browser_locale, browser_timezone, searched_at, outcome,
             failure_kind, failure_detail, examined, selected, captured,
             partial, failed, skipped)
        VALUES
            (:extraction, :request, :serp, :query, :normalised, :top_n,
             :results_examined, :google_host, :serp_url, :locale, :timezone,
             :searched_at, :outcome, :failure_kind, :failure_detail, :examined,
             :selected, :captured, :partial, :failed, :skipped)
        RETURNING id
    """), values).scalar_one()
    return observation_id, result_id


def _result(connection, observation_id: int, capture_id: str | None,
            **overrides) -> int:
    values = {
        "observation": observation_id,
        "rank": 1,
        "url": "https://example.com/article",
        "normalised_url": "https://example.com/article",
        "host": "example.com",
        "title": "Enterprise AI article",
        "snippet": "A complete organic result snippet.",
        "selected": True,
        "selection_reason": "top_n",
        "capture": capture_id,
        "capture_mode": "generic",
        "capture_status": "succeeded",
        "failure_kind": None,
        "failure_detail": None,
    }
    values.update(overrides)
    return connection.execute(text("""
        INSERT INTO google.search_result
            (observation_id, rank, url, normalised_url, host, title, snippet,
             selected, selection_reason, capture_id, capture_mode,
             capture_status, failure_kind, failure_detail)
        VALUES
            (:observation, :rank, :url, :normalised_url, :host, :title,
             :snippet, :selected, :selection_reason, :capture, :capture_mode,
             :capture_status, :failure_kind, :failure_detail)
        RETURNING id
    """), values).scalar_one()


def test_one_observation_per_extraction(app):
    observation, _ = _observation(app)
    extraction_id = app.execute(text("""
        SELECT extraction_id FROM google.search_observation WHERE id=:id
    """), {"id": observation}).scalar_one()
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO google.search_observation
                (extraction_id, request_id, query_requested, query_normalised,
                 requested_top_n, results_examined, outcome, examined,
                 selected, captured, partial, failed, skipped)
            SELECT extraction_id, request_id, query_requested, query_normalised,
                   requested_top_n, results_examined, outcome, examined,
                   selected, captured, partial, failed, skipped
            FROM google.search_observation WHERE id=:id
        """), {"id": observation})
    assert extraction_id


def test_result_rank_is_unique_inside_observation(app):
    observation, capture_id = _observation(app)
    _result(app, observation, capture_id)
    with pytest.raises(IntegrityError):
        _result(app, observation, capture_id)


def test_selected_success_requires_a_capture(app):
    observation, _ = _observation(app)
    with pytest.raises(IntegrityError):
        _result(app, observation, None)


@pytest.mark.parametrize("overrides", [
    {"top_n": -1},
    {"top_n": 21},
    {"results_examined": -1, "examined": -1},
    {"results_examined": 61, "examined": 61, "selected": 0,
     "captured": 0, "skipped": 61},
    {"selected": -1},
    {"captured": -1},
    {"partial": -1},
    {"failed": -1},
    {"skipped": -1},
    {"results_examined": 2},
    {"selected": 2, "captured": 2},
    {"captured": 0},
    {"skipped": 1},
])
def test_observation_counts_and_bounds_are_closed(app, overrides):
    with pytest.raises(IntegrityError):
        _observation(app, **overrides)


@pytest.mark.parametrize("overrides", [
    {"outcome": "suspect"},
    {"failure_kind": "invented", "failure_detail": "x",
     "outcome": "partial"},
    {"failure_kind": "blocked"},
    {"failure_detail": "challenge"},
    {"outcome": "ok", "failure_kind": "blocked",
     "failure_detail": "challenge"},
    {"outcome": "failed"},
])
def test_observation_outcome_and_failure_vocabulary_is_closed(app, overrides):
    with pytest.raises(IntegrityError):
        _observation(app, **overrides)


def test_partial_observation_may_preserve_request_failure_evidence(app):
    observation, _ = _observation(
        app, outcome="partial", failure_kind="blocked",
        failure_detail="unusual traffic")
    assert app.execute(text("""
        SELECT failure_kind FROM google.search_observation WHERE id=:id
    """), {"id": observation}).scalar_one() == "blocked"


@pytest.mark.parametrize("overrides", [
    {"rank": 0},
    {"rank": 61},
    {"selection_reason": "invented"},
    {"capture_mode": "deep_linkedin"},
    {"capture_status": "suspect"},
    {"failure_kind": "invented", "failure_detail": "x",
     "capture_status": "failed", "capture": None},
])
def test_result_bounds_and_vocabularies_are_closed(app, overrides):
    observation, capture_id = _observation(app)
    with pytest.raises(IntegrityError):
        _result(app, observation, capture_id, **overrides)


def test_unselected_result_cannot_name_capture_facts(app):
    observation, capture_id = _observation(app)
    with pytest.raises(IntegrityError):
        _result(
            app, observation, capture_id, selected=False,
            selection_reason="beyond_top_n")


def test_a_skipped_social_destination_is_stored_as_an_unselected_result(app):
    """The rank, URL, title and snippet are kept; the capture columns are not
    filled in, because nothing was opened."""
    observation, _ = _observation(app)

    result_id = _result(
        app, observation, None, selected=False,
        selection_reason="excluded_host", capture_mode=None,
        capture_status=None)

    assert result_id > 0


def test_a_skipped_social_destination_cannot_name_a_capture(app):
    """`excluded_host` means the page was never opened, and the database is
    where that stops being a promise and starts being enforced."""
    observation, capture_id = _observation(app)

    with pytest.raises(IntegrityError):
        _result(app, observation, capture_id, selected=False,
                selection_reason="excluded_host")


def test_selected_failed_result_may_have_no_capture(app):
    observation, _ = _observation(app)
    result_id = _result(
        app, observation, None, capture_status="failed",
        failure_kind="timeout", failure_detail="navigation timed out")
    assert result_id > 0


@pytest.mark.parametrize("overrides", [
    {"capture_status": "succeeded", "failure_kind": "timeout",
     "failure_detail": "x"},
    {"capture_status": "partial"},
    {"capture_status": "failed"},
    {"capture_status": "partial", "failure_kind": "capture_error"},
    {"capture_status": "failed", "failure_detail": "x"},
])
def test_result_failure_shape_matches_capture_status(app, overrides):
    observation, capture_id = _observation(app)
    with pytest.raises(IntegrityError):
        _result(app, observation, capture_id, **overrides)


@pytest.mark.parametrize("table", [
    "google.search_observation", "google.search_result",
])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "TRUNCATE"])
def test_google_projection_tables_are_append_only_for_app_and_owner(
        app, migrator, table, verb):
    observation, capture_id = _observation(app)
    if table.endswith("search_result"):
        _result(app, observation, capture_id)
    update_column = (
        "query_requested" if table.endswith("search_observation") else "url")
    truncate_target = table
    if table.endswith("search_observation"):
        # PostgreSQL checks the child FK before firing a parent-only TRUNCATE
        # trigger, so name the closed projection set to exercise protection.
        truncate_target += ", google.search_result"
    statement = {
        "UPDATE": f"UPDATE {table} SET {update_column}={update_column}",
        "DELETE": f"DELETE FROM {table}",
        "TRUNCATE": f"TRUNCATE {truncate_target}",
    }[verb]
    with pytest.raises(DBAPIError) as app_error:
        app.execute(text(statement))
    assert app_error.value.orig.sqlstate == "42501"
    with pytest.raises(DBAPIError) as owner_error:
        migrator.execute(text(statement))
    assert owner_error.value.orig.sqlstate == "CL001"
