"""A worker that died mid-capture, and the request it left behind.

Without this, a stuck `running` request is permanent: submit() reuses an
outstanding request and the partial unique index forbids a second one, so the
URL can never be scraped again.

Procrastinate owns the detection -- it keeps worker heartbeats and answers a
stalled-job query from them, which is part of why it was chosen (v2 D9). What
is tested here is our half: turning a stalled job into a closed attempt and a
failed request.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.journal import Journal, sweep_stalled
from career_lens.store.db import transaction
from career_lens.store.ids import REQUEST, new_id

NOW = datetime.now(UTC)


def _queued_slot_operation(app_url):
    from career_lens.fleet.assignments import assign_account

    with transaction(app_url) as connection:
        connection.execute(text("""
            INSERT INTO supervisor
                (id, label, desired_state, instance_id, hostname,
                 software_version, started_at, last_heartbeat_at)
            VALUES ('worker-a', 'Laptop', 'active', 'boot', 'host', 'test',
                    :now, :now)
        """), {"now": NOW})
        connection.execute(text("""
            INSERT INTO worker
                (id, supervisor_id, local_name, desired_state, health)
            VALUES ('worker-a', 'worker-a', 'research', 'enabled', 'healthy')
        """))
        connection.execute(text("""
            INSERT INTO linkedin_account
                (id, label, username, state, max_attempts_24h,
                 min_interval_seconds, created_at, updated_at)
            VALUES ('la-a', 'Alice', 'alice@example.com', 'enabled',
                    20, 0, :now, :now)
        """), {"now": NOW})
        connection.execute(text("""
            INSERT INTO linkedin_account_credential
                (account_id, ciphertext, nonce, key_version, updated_at)
            VALUES ('la-a', :ciphertext, :nonce, 1, :now)
        """), {"ciphertext": b"opaque", "nonce": b"n" * 12, "now": NOW})
        assignment_id, operation_id = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        queue_job_id = connection.execute(text("""
            SELECT queue_job_id FROM worker_operation WHERE id=:operation
        """), {"operation": operation_id}).scalar_one()
    return assignment_id, operation_id, queue_job_id


def _set_queue_job_status(connection, job_id: int, status: str) -> None:
    """Run Procrastinate's trigger with its own schema on the search path."""
    connection.execute(text("SET search_path TO procrastinate, public"))
    connection.execute(text("""
        UPDATE procrastinate.procrastinate_jobs SET status=:status
        WHERE id=:job
    """), {"status": status, "job": job_id})
    connection.execute(text("SET search_path TO public"))


class _StalledJob:
    """What get_stalled_jobs returns, reduced to what the sweep reads."""

    def __init__(self, request_id: str, job_id: int = 1):
        self.id = job_id
        self.task_kwargs = {"request_id": request_id}


@pytest.fixture
def running(app) -> str:
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'running', :t)
    """), {"r": rid, "u": "https://example.com/stalled", "t": NOW})
    return rid


def test_a_stalled_job_closes_its_attempt_and_retries_its_request(
        app, app_url, running):
    journal = Journal.open(app_url, request_id=running,
                           browser_profile="research")

    closed = sweep_stalled(app_url, stalled=lambda: [_StalledJob(running)],
                           finish=lambda job: None)

    assert closed == [running]
    row = app.execute(text("""
        SELECT r.status, a.cause, a.ended_at
        FROM scrape_request r JOIN scrape_attempt a ON a.request_id = r.id
        WHERE a.id = :a
    """), {"a": journal.attempt_id}).one()
    assert row.status == "queued"
    assert row.cause == "worker_lost"
    assert row.ended_at is not None


def test_the_outstanding_url_reuses_the_recovering_request(
        app_url, running, monkeypatch):
    """The user-visible bug. The partial unique index on url_normalised covers
    queued and running rows, so a stuck request blocks its URL forever."""
    from career_lens.api import requests as submission

    Journal.open(app_url, request_id=running, browser_profile="research")
    sweep_stalled(app_url, stalled=lambda: [_StalledJob(running)],
                  finish=lambda job: None)

    # submit() imports enqueue inside the function, so the name to replace is
    # the one in its defining module rather than a binding on the caller.
    request_id, created, *_ = submission.submit(
        "https://example.com/stalled", database=app_url)

    assert created is False
    assert request_id == running


def test_a_job_whose_attempt_already_ended_is_left_alone(app, app_url, running):
    """A job can stall after its attempt closed -- the worker died between
    finishing the work and telling the queue. Rewriting the cause would replace
    a real one with `worker_lost`."""
    journal = Journal.open(app_url, request_id=running,
                           browser_profile="research")
    with transaction(app_url) as connection:
        journal.close(connection, cause="not_found", detail="gone")
        connection.execute(text(
            "UPDATE scrape_request SET status = 'failed' WHERE id = :r"),
            {"r": running})

    closed = sweep_stalled(app_url, stalled=lambda: [_StalledJob(running)],
                           finish=lambda job: None)

    assert closed == []
    assert app.execute(text(
        "SELECT cause FROM scrape_attempt WHERE id = :a"),
        {"a": journal.attempt_id}).scalar_one() == "not_found"


def test_the_queue_is_told_even_when_there_was_nothing_to_close(app_url, running):
    """A stalled job must be finished either way, or it is claimed again on the
    next startup and stalls again."""
    finished: list[int] = []

    sweep_stalled(app_url, stalled=lambda: [_StalledJob(running, job_id=42)],
                  finish=lambda job: finished.append(job.id))

    assert finished == [42]


def test_terminal_capture_job_requeues_the_open_attempt(
        app, app_url, running):
    from career_lens.fleet.recovery import sweep_orphaned_scrape_attempts

    _, _, job_id = _queued_slot_operation(app_url)
    attempt_id = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, started_at, queue_job_id)
        VALUES (:request, 1, :now, :job)
        RETURNING id
    """), {"request": running, "now": NOW, "job": job_id}).scalar_one()
    _set_queue_job_status(app, job_id, "failed")

    assert sweep_orphaned_scrape_attempts(
        database=app_url, now=NOW) == [running]

    row = app.execute(text("""
        SELECT request.status, request.next_attempt_at,
               attempt.ended_at, attempt.cause, attempt.cause_detail
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE attempt.id=:attempt
    """), {"attempt": attempt_id}).one()
    assert row.status == "queued"
    assert row.next_attempt_at == NOW + timedelta(minutes=2)
    assert row.ended_at == NOW
    assert row.cause == "browser_error"
    assert "queue job became terminal" in row.cause_detail


def test_a_job_naming_no_request_is_skipped(app_url):
    """Procrastinate's queue is ours alone today, but a job whose kwargs do not
    name a request must not raise the whole sweep."""
    class _Nameless:
        id = 7
        task_kwargs: dict = {}

    assert sweep_stalled(app_url, stalled=lambda: [_Nameless()],
                         finish=lambda job: None) == []


def test_a_stalled_slot_operation_quarantines_attached_resources(
        app, app_url, monkeypatch):
    from career_lens.fleet.assignments import assign_account

    monkeypatch.setattr(
        "career_lens.queue.enqueue.enqueue_worker_operation", lambda *a, **k: 1)
    with transaction(app_url) as connection:
        connection.execute(text("""
            INSERT INTO supervisor
                (id, label, desired_state, instance_id, hostname,
                 software_version, started_at, last_heartbeat_at)
            VALUES ('worker-a', 'Laptop', 'active', 'boot', 'host', 'test',
                    :now, :now)
        """), {"now": NOW})
        connection.execute(text("""
            INSERT INTO worker
                (id, supervisor_id, local_name, desired_state, health)
            VALUES ('worker-a', 'worker-a', 'research', 'enabled', 'healthy')
        """))
        connection.execute(text("""
            INSERT INTO linkedin_account
                (id, label, username, state, max_attempts_24h,
                 min_interval_seconds, created_at, updated_at)
            VALUES ('la-a', 'Alice', 'alice@example.com', 'enabled',
                    20, 0, :now, :now)
        """), {"now": NOW})
        connection.execute(text("""
            INSERT INTO linkedin_account_credential
                (account_id, ciphertext, nonce, key_version, updated_at)
            VALUES ('la-a', :ciphertext, :nonce, 1, :now)
        """), {"ciphertext": b"opaque", "nonce": b"n" * 12, "now": NOW})
        assignment_id, operation_id = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)

    class OperationJob:
        id = 9
        task_kwargs = {"operation_id": operation_id}

    finished = []
    sweep_stalled(
        app_url, stalled=lambda: [OperationJob()],
        finish=lambda job: finished.append(job.id))

    row = app.execute(text("""
        SELECT operation.status, ass.state, ass.ended_at,
               ass.operator_attention_code AS assignment_code,
               account.state AS account_state,
               account.operator_attention_code AS account_code,
               worker.health, worker.operator_attention_code AS worker_code
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
    """), {"operation": operation_id}).one()
    assert row._mapping == {
        "status": "failed", "state": "attention_required",
        "ended_at": None,
        "assignment_code": "worker_operation_abandoned",
        "account_state": "attention_required",
        "account_code": "worker_operation_abandoned",
        "health": "attention_required",
        "worker_code": "worker_operation_abandoned",
    }
    assert finished == [9]
    assert assignment_id


@pytest.mark.parametrize("status", ["todo", "doing", "aborting"])
def test_live_slot_operation_jobs_are_never_reaped_for_age(
        app, app_url, status):
    from career_lens.fleet.recovery import sweep_orphaned_worker_operations

    _, operation_id, job_id = _queued_slot_operation(app_url)
    _set_queue_job_status(app, job_id, status)
    app.execute(text("""
        UPDATE worker_operation SET requested_at=:old WHERE id=:operation
    """), {"old": NOW - timedelta(days=1), "operation": operation_id})

    assert sweep_orphaned_worker_operations(
        database=app_url, now=NOW) == []
    assert app.execute(text("""
        SELECT status FROM worker_operation WHERE id=:operation
    """), {"operation": operation_id}).scalar_one() == "queued"


@pytest.mark.parametrize(
    "status", ["failed", "cancelled", "aborted", "succeeded"])
def test_terminal_job_fails_an_open_slot_operation_immediately(
        app, app_url, status):
    from career_lens.fleet.recovery import sweep_orphaned_worker_operations

    assignment_id, operation_id, job_id = _queued_slot_operation(app_url)
    _set_queue_job_status(app, job_id, status)

    assert sweep_orphaned_worker_operations(
        database=app_url, now=NOW) == [operation_id]
    row = app.execute(text("""
        SELECT operation.status, ass.state, ass.ended_at,
               ass.operator_attention_code AS assignment_code,
               account.state AS account_state,
               account.operator_attention_code AS account_code,
               worker.health, worker.operator_attention_code AS worker_code
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
    """), {"operation": operation_id}).one()
    assert row._mapping == {
        "status": "failed", "state": "attention_required", "ended_at": None,
        "assignment_code": "worker_operation_abandoned",
        "account_state": "attention_required",
        "account_code": "worker_operation_abandoned",
        "health": "attention_required",
        "worker_code": "worker_operation_abandoned",
    }
    assert assignment_id


@pytest.mark.parametrize("attribution", ["missing", "legacy_null"])
def test_unattributed_operation_gets_a_fifteen_minute_grace(
        app, app_url, attribution):
    from career_lens.fleet.recovery import sweep_orphaned_worker_operations

    _, operation_id, _ = _queued_slot_operation(app_url)
    queue_job_id = None if attribution == "legacy_null" else 9_999_999
    app.execute(text("""
        UPDATE worker_operation SET queue_job_id=:queue_job
        WHERE id=:operation
    """), {"queue_job": queue_job_id, "operation": operation_id})

    assert sweep_orphaned_worker_operations(
        database=app_url, now=NOW + timedelta(minutes=14)) == []
    assert sweep_orphaned_worker_operations(
        database=app_url, now=NOW + timedelta(minutes=16)) == [operation_id]


