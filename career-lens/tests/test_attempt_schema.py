"""What an attempt may change, and what its journal may not.

Two refusals with two different mechanisms, so each is asserted as the
principal it actually stops: the grant refuses the application (42501), the
trigger refuses the owner (CL001). A test that connects as the wrong section
proves the wrong thing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from career_lens.store.ids import REQUEST, new_id

NOW = datetime.now(UTC)


@pytest.fixture
def request_id(app) -> str:
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'queued', :t)
    """), {"r": rid, "u": "https://example.com/a", "t": NOW})
    return rid


def _attempt(connection, request_id: str, *, attempt_no: int = 1) -> int:
    return connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker, browser_profile, started_at)
        VALUES (:r, :n, 'host:1', 'research', :t)
        RETURNING id
    """), {"r": request_id, "n": attempt_no, "t": NOW}).scalar_one()


def _event(connection, attempt_id: int, **overrides) -> int:
    values = {"a": attempt_id, "at": NOW, "elapsed": 10, "duration": 5,
              "kind": "work", "level": "info", "phase": "extract html",
              "detail": "", "failed": False}
    values.update(overrides)
    return connection.execute(text("""
        INSERT INTO attempt_event
            (attempt_id, at, elapsed_ms, duration_ms, kind, level, phase,
             detail, failed)
        VALUES (:a, :at, :elapsed, :duration, :kind, :level, :phase,
                :detail, :failed)
        RETURNING id
    """), values).scalar_one()


def test_an_attempt_can_be_closed(app, request_id):
    """The attempt is the one new mutable table: ended_at and cause are
    written after the row is created."""
    attempt = _attempt(app, request_id)
    app.execute(text("""
        UPDATE scrape_attempt SET ended_at = :t, cause = 'timeout',
               cause_detail = 'the page did not settle'
        WHERE id = :a
    """), {"a": attempt, "t": NOW})

    row = app.execute(text(
        "SELECT cause, ended_at FROM scrape_attempt WHERE id = :a"),
        {"a": attempt}).one()
    assert row.cause == "timeout"
    assert row.ended_at is not None


def test_a_cause_without_an_end_is_refused(app, request_id):
    """An attempt that has a reason it stopped has stopped."""
    attempt = _attempt(app, request_id)
    with pytest.raises(DBAPIError) as caught:
        app.execute(text(
            "UPDATE scrape_attempt SET cause = 'timeout' WHERE id = :a"),
            {"a": attempt})
    assert caught.value.orig.sqlstate == "23514"


def test_two_attempts_for_one_request_need_different_numbers(app, request_id):
    _attempt(app, request_id, attempt_no=1)
    with pytest.raises(DBAPIError) as caught:
        _attempt(app, request_id, attempt_no=1)
    assert caught.value.orig.sqlstate == "23505"

    assert _attempt(app, request_id, attempt_no=2) is not None


def test_worker_lost_is_an_accepted_cause(app, request_id):
    """The cause a killed worker gets. Added to the vocabulary here; nothing
    writes it until the recovery sweep exists."""
    attempt = _attempt(app, request_id)
    app.execute(text("""
        UPDATE scrape_attempt SET ended_at = :t, cause = 'worker_lost'
        WHERE id = :a
    """), {"a": attempt, "t": NOW})

    assert app.execute(text(
        "SELECT cause FROM scrape_attempt WHERE id = :a"),
        {"a": attempt}).scalar_one() == "worker_lost"


def test_an_unknown_cause_is_refused(app, request_id):
    """The vocabulary is closed. Free text cannot be aggregated, alerted on, or
    used to decide whether a retry is even sensible."""
    attempt = _attempt(app, request_id)
    with pytest.raises(DBAPIError) as caught:
        app.execute(text("""
            UPDATE scrape_attempt SET ended_at = :t, cause = 'gave_up'
            WHERE id = :a
        """), {"a": attempt, "t": NOW})
    assert caught.value.orig.sqlstate == "23514"


def test_an_event_level_outside_the_vocabulary_is_refused(app, request_id):
    attempt = _attempt(app, request_id)
    with pytest.raises(DBAPIError) as caught:
        _event(app, attempt, level="trace")
    assert caught.value.orig.sqlstate == "23514"


def test_the_application_may_not_change_an_event(app, request_id):
    """The grant refuses it: career_lens_app holds SELECT and INSERT only."""
    attempt = _attempt(app, request_id)
    _event(app, attempt)
    with pytest.raises(DBAPIError) as caught:
        app.execute(text("UPDATE attempt_event SET detail = 'edited'"))
    assert caught.value.orig.sqlstate == "42501"


def test_the_owner_may_not_change_an_event_either(app, migrator, request_id):
    """The trigger refuses it. The owner is not stopped by the grant, so this
    is the only assertion that proves the trigger exists."""
    attempt = _attempt(app, request_id)
    _event(app, attempt)
    with pytest.raises(DBAPIError) as caught:
        migrator.execute(text("UPDATE attempt_event SET detail = 'edited'"))
    assert caught.value.orig.sqlstate == "CL001"
