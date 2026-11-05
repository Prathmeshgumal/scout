"""The journal: what reaches the database, and what may never reach the run.

The instrumentation these events come from already exists and is already
tested. What is asserted here is the bridge -- that a traced phase becomes a
row with the right shape, and that a database failure on the way stops at this
module rather than costing a capture.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.capture import trace
from career_lens.journal import Journal, worker_name
from career_lens.store.db import transaction
from career_lens.store.ids import CAPTURE, REQUEST, new_id

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _no_trace_leaks():
    """trace keeps its active run in a module global. A test that activated one
    and did not close it would hand it to the next test."""
    yield
    trace.close()


@pytest.fixture
def request_id(app) -> str:
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'running', :t)
    """), {"r": rid, "u": "https://example.com/a", "t": NOW})
    return rid


def _events(connection, attempt_id: int):
    return connection.execute(text("""
        SELECT kind, level, phase, detail, duration_ms, elapsed_ms, failed,
               capture_id
        FROM attempt_event WHERE attempt_id = :a ORDER BY id
    """), {"a": attempt_id}).all()


def test_opening_an_attempt_numbers_it_from_one_and_names_the_worker(
        app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research")

    row = app.execute(text("""
        SELECT attempt_no, worker, browser_profile, ended_at, cause
        FROM scrape_attempt WHERE id = :a
    """), {"a": journal.attempt_id}).one()
    assert row.attempt_no == 1
    assert row.worker == worker_name()
    assert row.browser_profile == "research"
    assert row.ended_at is None and row.cause is None


def test_a_second_attempt_for_the_same_request_is_numbered_two(
        app, app_url, request_id):
    Journal.open(app_url, request_id=request_id, browser_profile="research")
    second = Journal.open(app_url, request_id=request_id,
                          browser_profile="research")

    assert app.execute(text(
        "SELECT attempt_no FROM scrape_attempt WHERE id = :a"),
        {"a": second.attempt_id}).scalar_one() == 2


def test_a_traced_phase_becomes_an_event_carrying_its_duration(
        app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)
    trace.activate(["test"], sink=journal.sink)

    with trace.phase(trace.NET, "open tab") as span:
        span.detail = "example.com"

    events = _events(app, journal.attempt_id)
    assert len(events) == 1
    assert events[0].kind == "net"
    assert events[0].phase == "open tab"
    assert events[0].detail == "example.com"
    assert events[0].level == "info"
    assert events[0].duration_ms is not None
    assert events[0].failed is False


def test_a_routine_phase_is_recorded_at_debug(app, app_url, request_id):
    """trace.ROUTINE names the sub-second bookkeeping phases. They are
    persisted -- filtering happens on read -- but they carry their level so the
    UI can hide them."""
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)
    trace.activate(["test"], sink=journal.sink)

    with trace.phase(trace.WORK, "extract html"):
        pass

    assert _events(app, journal.attempt_id)[0].level == "debug"


def test_a_failing_phase_is_recorded_as_failed_at_warning(
        app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)
    trace.activate(["test"], sink=journal.sink)

    with pytest.raises(RuntimeError):
        with trace.phase(trace.WORK, "scroll page"):
            raise RuntimeError("no")

    event = _events(app, journal.attempt_id)[0]
    assert event.failed is True
    assert event.level == "warning"


def test_a_note_becomes_an_event_with_no_duration(app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)
    trace.activate(["test"], sink=journal.sink)

    trace.note("challenge cleared")

    event = _events(app, journal.attempt_id)[0]
    assert event.kind == "note"
    assert event.phase == "challenge cleared"
    assert event.duration_ms is None


def test_capture_stored_carries_the_capture_it_names(app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at, browser_profile)
        VALUES (:c, :r, 'profile', 'https://example.com/a', 'example.com',
                :t, 'research')
    """), {"c": capture_id, "r": request_id, "t": NOW})

    journal.capture_stored(capture_id, "profile")

    event = _events(app, journal.attempt_id)[0]
    assert event.phase == "capture stored"
    assert event.capture_id == capture_id
    assert event.detail == "profile"


def test_a_flush_that_raises_never_reaches_the_traced_code(
        app_url, request_id, monkeypatch):
    """The whole point of the module. A progress log that can lose a run is
    worse than no progress log, so a database failure here is swallowed and
    reported, never raised.

    Note trace itself does NOT guard the sink: if it did, this test could not
    tell whether the guard below exists.
    """
    import career_lens.journal as journal_module

    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research", batch=1)

    def explode(*args, **kwargs):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(journal_module, "transaction", explode)
    trace.activate(["test"], sink=journal.sink)

    with trace.phase(trace.WORK, "scroll page"):
        pass  # reached the sink, which flushed inline, and did not raise


def test_close_writes_the_cause_on_the_connection_it_is_given(
        app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research")

    with transaction(app_url) as connection:
        journal.close(connection, cause="timeout", detail="did not settle")

    row = app.execute(text(
        "SELECT cause, cause_detail, ended_at FROM scrape_attempt WHERE id = :a"),
        {"a": journal.attempt_id}).one()
    assert row.cause == "timeout"
    assert row.cause_detail == "did not settle"
    assert row.ended_at is not None


def test_close_flushes_pending_events_on_the_connection_it_is_given(
        app, app_url, request_id, monkeypatch):
    import career_lens.journal as journal_module

    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research")
    journal.sink(
        kind="work", phase="finalize request", detail="succeeded",
        seconds=0.01, failed=False, level="info", elapsed=0.01)

    def unexpected_private_transaction(*args, **kwargs):
        raise RuntimeError("close must use its caller's transaction")

    monkeypatch.setattr(
        journal_module, "transaction", unexpected_private_transaction)
    with transaction(app_url) as connection:
        journal.close(connection)

    assert _events(app, journal.attempt_id)[0].phase == "finalize request"


def test_a_close_inside_a_transaction_that_rolls_back_did_not_happen(
        app, app_url, request_id):
    """close() is synchronous and joins the caller's transaction rather than
    opening its own. That is what lets the attempt's cause and the request's
    status commit together -- and it means a caller that fails takes the close
    down with it. A close on a private connection would survive this."""
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research")

    with pytest.raises(RuntimeError):
        with transaction(app_url) as connection:
            journal.close(connection, cause="timeout", detail="did not settle")
            raise RuntimeError("the caller failed after closing")

    row = app.execute(text(
        "SELECT cause, ended_at FROM scrape_attempt WHERE id = :a"),
        {"a": journal.attempt_id}).one()
    assert row.cause is None
    assert row.ended_at is None


def test_a_successful_close_leaves_no_cause(app, app_url, request_id):
    journal = Journal.open(app_url, request_id=request_id,
                           browser_profile="research")
    with transaction(app_url) as connection:
        journal.close(connection)

    row = app.execute(text(
        "SELECT cause, ended_at FROM scrape_attempt WHERE id = :a"),
        {"a": journal.attempt_id}).one()
    assert row.cause is None
    assert row.ended_at is not None
