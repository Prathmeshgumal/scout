"""What the core tables refuse.

Every test here connects as the section that can actually reach the thing being
tested. Postgres checks table privileges BEFORE firing triggers, so a test that
asserts CL001 while connected as career_lens_app can never pass -- the grant
stops it first, with 42501.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from career_lens.store.ids import CAPTURE, REQUEST, new_id

NOW = datetime.now(UTC)


def _request(connection, *, url="https://www.linkedin.com/in/x/",
             kind="linkedin_person", status="queued") -> str:
    rid = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :u, :u, :k, :s, :t)
    """), {"id": rid, "u": url, "k": kind, "s": status, "t": NOW})
    return rid


def _google_request(connection, *, query="AI Research", normalised="ai research",
                    top_n=5, status="queued") -> str:
    rid = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:id, :query, :normalised, :top_n,
                'google_search', :status, :now)
    """), {
        "id": rid, "query": query, "normalised": normalised,
        "top_n": top_n, "status": status, "now": NOW,
    })
    return rid


def test_the_app_role_cannot_update_a_capture(app):
    rid = _request(app)
    cid = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at, browser_profile)
        VALUES (:c, :r, 'profile', 'https://x/', 'x', :t, 'research')
    """), {"c": cid, "r": rid, "t": NOW})

    with pytest.raises(DBAPIError) as raised:
        app.execute(text("UPDATE capture SET title = 'changed'"))
    # The grant, not the trigger: privileges are checked first.
    assert raised.value.orig.sqlstate == "42501"


def test_even_the_owner_cannot_update_a_capture(migrator):
    rid = _request(migrator)
    cid = new_id(CAPTURE)
    migrator.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at, browser_profile)
        VALUES (:c, :r, 'profile', 'https://x/', 'x', :t, 'research')
    """), {"c": cid, "r": rid, "t": NOW})

    with pytest.raises(DBAPIError) as raised:
        migrator.execute(text("UPDATE capture SET title = 'changed'"))
    # Ownership ignores GRANT and REVOKE, so this is the trigger's refusal.
    assert raised.value.orig.sqlstate == "CL001"


def test_an_update_matching_no_rows_is_still_refused(migrator):
    # FOR EACH STATEMENT: what is rejected is the intent. Code that means to
    # update an append-only table is wrong whether or not it finds a row today.
    with pytest.raises(DBAPIError) as raised:
        migrator.execute(text("UPDATE capture SET title = 'x' WHERE false"))
    assert raised.value.orig.sqlstate == "CL001"


def test_two_outstanding_requests_for_one_url_are_refused(app):
    _request(app, status="queued")
    with pytest.raises(IntegrityError):
        _request(app, status="running")


def test_a_url_can_be_requested_again_once_the_first_finished(app):
    # Dedupe, not a prohibition. The whole point of the tool is scraping the
    # same profile repeatedly over time.
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"id": new_id(REQUEST), "u": "https://www.linkedin.com/in/x/", "t": NOW})
    _request(app, status="queued")   # must not raise


def test_google_request_requires_query_and_forbids_url(app):
    request_id = _google_request(app)

    row = app.execute(text("""
        SELECT query_requested, query_normalised, google_top_n,
               url_requested, url_normalised, kind
        FROM scrape_request WHERE id=:id
    """), {"id": request_id}).one()
    assert row._mapping == {
        "query_requested": "AI Research",
        "query_normalised": "ai research",
        "google_top_n": 5,
        "url_requested": None,
        "url_normalised": None,
        "kind": "google_search",
    }


@pytest.mark.parametrize("top_n", [-1, 6, 21])
def test_google_top_n_is_bounded_by_the_database(app, top_n):
    with pytest.raises(IntegrityError):
        _google_request(app, query=f"query {top_n}",
                        normalised=f"query {top_n}", top_n=top_n)


def test_url_request_cannot_carry_google_query_fields(app):
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, query_requested,
                 query_normalised, google_top_n, kind, status, requested_at)
            VALUES (:id, 'https://example.com/a', 'https://example.com/a',
                    'AI', 'ai', 1, 'generic', 'queued', :now)
        """), {"id": new_id(REQUEST), "now": NOW})


def test_google_request_cannot_carry_url_fields(app):
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, query_requested,
                 query_normalised, google_top_n, kind, status, requested_at)
            VALUES (:id, 'https://example.com/a', 'https://example.com/a',
                    'AI', 'ai', 1, 'google_search', 'queued', :now)
        """), {"id": new_id(REQUEST), "now": NOW})


def test_outstanding_google_dedupe_includes_top_n(app):
    _google_request(app, top_n=5)
    _google_request(app, top_n=4)

    with pytest.raises(IntegrityError):
        _google_request(app, top_n=5, status="running")


def test_completed_google_requests_may_be_repeated(app):
    _google_request(app, status="succeeded")
    _google_request(app, status="queued")


def test_storage_error_is_a_valid_attempt_cause(app):
    request_id = _google_request(app)
    cause = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, ended_at, cause)
        VALUES (:request, 1, :now, :now, 'storage_error')
        RETURNING cause
    """), {"request": request_id, "now": NOW}).scalar_one()
    assert cause == "storage_error"


def test_rendered_text_is_a_valid_capture_artifact(app):
    request_id = _request(app)
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture
            (id, request_id, section, url_requested, host, captured_at)
        VALUES (:capture, :request, 'page', 'https://example.com/a',
                'example.com', :now)
    """), {"capture": capture_id, "request": request_id, "now": NOW})

    stored = app.execute(text("""
        INSERT INTO artifact
            (capture_id, kind, ordinal, blob_key, content_type,
             byte_size, sha256)
        VALUES (:capture, 'text', 0, :key, 'text/plain; charset=utf-8',
                4, :sha256)
        RETURNING kind
    """), {
        "capture": capture_id,
        "key": f"captures/{capture_id}/page.txt",
        "sha256": "0" * 64,
    }).scalar_one()
    assert stored == "text"


# The three tests that were here -- a failed request must name a cause, a
# succeeded one may not carry one, and an unknown cause is refused -- moved
# with their columns. The cause lives on scrape_attempt now (0008), so:
#
#   the vocabulary   tests/test_attempt_schema.py::test_an_unknown_cause_is_refused
#   the pairing      tests/test_queue.py::
#                    test_a_failed_request_always_has_an_attempt_carrying_the_cause
#
# The pairing is no longer a CHECK -- it cannot span two tables -- so it is an
# invariant queue/tasks.py::_finish holds and that test asserts.


def test_a_failed_extraction_may_not_name_a_document(app):
    rid = _request(app)
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                    extracted_at, outcome, document_key)
            VALUES ('ex-1', :r, 'x', '1.0', :t, 'failed', 'some/key')
        """), {"r": rid, "t": NOW})


def test_a_successful_extraction_must_name_a_document(app):
    rid = _request(app)
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                    extracted_at, outcome)
            VALUES ('ex-2', :r, 'x', '1.0', :t, 'ok')
        """), {"r": rid, "t": NOW})


def test_a_request_carries_its_own_attempt_ceiling(app):
    """Per-request rather than a global setting: raising the deployment
    default must not retroactively resurrect old failures."""
    request_id = _request(app)

    row = app.execute(text(
        "SELECT max_attempts, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()

    assert (row.max_attempts, row.next_attempt_at) == (3, None)


def test_a_request_may_not_have_an_attempt_ceiling_below_one(app):
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status, requested_at,
                 max_attempts)
            VALUES (:id, :u, :u, 'generic', 'queued', :t, 0)
        """), {"id": new_id(REQUEST), "u": "https://example.com/floor",
               "t": NOW})


