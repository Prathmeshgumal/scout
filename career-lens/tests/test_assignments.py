"""Operator-controlled account-to-browser assignment lifecycle."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier, Lock

import pytest
from sqlalchemy import text

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=UTC)


def _fleet(connection, *, account_id="la-a", worker_id="worker-a"):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-a', 'Laptop', 'active', 'instance-a', 'host-a',
                'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health)
        VALUES (:worker, 'worker-a', 'research', 'enabled', 'healthy')
    """), {"worker": worker_id})
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES (:account, 'Research', 'research@example.com', 'enabled',
                20, 60, :now, :now)
    """), {"account": account_id, "now": NOW})
    connection.execute(text("""
        INSERT INTO linkedin_account_credential
            (account_id, ciphertext, nonce, key_version, updated_at)
        VALUES (:account, :ciphertext, :nonce, 1, :now)
    """), {
        "account": account_id, "ciphertext": b"opaque",
        "nonce": b"n" * 12, "now": NOW,
    })


def _transaction(app_url):
    from career_lens.store.db import transaction

    return transaction(app_url)


def test_assigning_creates_login_pending_assignment_and_operation(app_url):
    from career_lens.fleet.assignments import assign_account

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, operation_id = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        assignment = connection.execute(text("""
            SELECT state, assigned_at, ended_at
            FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()
        operation = connection.execute(text("""
            SELECT kind, status, worker_id, assignment_id, queue_job_id
            FROM worker_operation WHERE id=:id
        """), {"id": operation_id}).one()

    assert assignment._mapping == {
        "state": "login_pending", "assigned_at": NOW, "ended_at": None,
    }
    assert operation._mapping == {
        "kind": "login", "status": "queued", "worker_id": "worker-a",
        "assignment_id": assignment_id,
        "queue_job_id": operation.queue_job_id,
    }
    assert operation.queue_job_id is not None


def test_account_and_slot_assignments_are_exclusive(app_url):
    from career_lens.fleet.assignments import AssignmentConflict, assign_account

    with _transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text("""
            INSERT INTO worker
                (id, supervisor_id, local_name, desired_state, health)
            VALUES ('worker-b', 'worker-a', 'research-b', 'enabled', 'healthy')
        """))
        assign_account(connection, account_id="la-a", worker_id="worker-a",
                       now=NOW)
        with pytest.raises(AssignmentConflict, match="account.*assigned"):
            assign_account(connection, account_id="la-a", worker_id="worker-b",
                           now=NOW)


def test_repeated_assignment_command_returns_the_open_login(app_url):
    from career_lens.fleet.assignments import assign_account

    with _transaction(app_url) as connection:
        _fleet(connection)
        first = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        second = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)

    assert second == first


@pytest.mark.parametrize("account_state", ["disabled", "retired"])
def test_disabled_or_retired_account_cannot_be_assigned(
        app_url, account_state):
    from career_lens.fleet.assignments import AssignmentConflict, assign_account

    with _transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text(
            "UPDATE linkedin_account SET state=:state WHERE id='la-a'"),
            {"state": account_state})
        with pytest.raises(AssignmentConflict, match="not enabled"):
            assign_account(connection, account_id="la-a", worker_id="worker-a",
                           now=NOW)


def test_compare_and_set_rejects_stale_or_invalid_transitions(app_url):
    from career_lens.fleet.assignments import (
        InvalidAssignmentTransition, assign_account, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        with pytest.raises(InvalidAssignmentTransition, match="expected"):
            transition_assignment(
                connection, assignment_id, expected="login_pending",
                target="logging_in", now=NOW)
        with pytest.raises(InvalidAssignmentTransition, match="not allowed"):
            transition_assignment(
                connection, assignment_id, expected="logging_in",
                target="clearing", now=NOW)


def test_ready_transition_records_verification_time(app_url):
    from career_lens.fleet.assignments import (
        assign_account, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in", target="ready",
            now=NOW)
        row = connection.execute(text("""
            SELECT state, verified_at FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()

    assert row.state == "ready"
    assert row.verified_at == NOW


def test_clear_success_detaches_only_after_browser_confirmation(app_url):
    from career_lens.fleet.assignments import (
        assign_account, finish_clear, request_clear, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in", target="ready",
            now=NOW)
        operation_id = request_clear(connection, assignment_id, now=NOW)
        before = connection.execute(text("""
            SELECT state, ended_at FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()
        assert before._mapping == {"state": "clearing", "ended_at": None}
        finish_clear(
            connection, assignment_id, operation_id, success=True,
            detail=None, now=NOW)
        after = connection.execute(text("""
            SELECT state, ended_at FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()

    assert after._mapping == {"state": "ended", "ended_at": NOW}


def test_repeated_clear_command_returns_the_open_operation(app_url):
    from career_lens.fleet.assignments import (
        assign_account, request_clear, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in", target="ready",
            now=NOW)
        first = request_clear(connection, assignment_id, now=NOW)
        second = request_clear(connection, assignment_id, now=NOW)

    assert second == first


def test_clear_failure_does_not_detach_the_account(app_url):
    from career_lens.fleet.assignments import (
        assign_account, finish_clear, request_clear, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in", target="ready",
            now=NOW)
        operation_id = request_clear(connection, assignment_id, now=NOW)
        finish_clear(
            connection, assignment_id, operation_id, success=False,
            detail="still signed in", now=NOW)
        assignment = connection.execute(text("""
            SELECT state, state_detail, ended_at, operator_attention_code
            FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()

    assert assignment._mapping == {
        "state": "attention_required", "state_detail": "still signed in",
        "ended_at": None, "operator_attention_code": "linkedin_clear_failed",
    }


def test_replace_requires_mismatch_and_queues_explicit_operation(app_url):
    from career_lens.fleet.assignments import (
        InvalidAssignmentTransition, assign_account, confirm_replace,
        transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        with pytest.raises(InvalidAssignmentTransition):
            confirm_replace(connection, assignment_id, now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in",
            target="account_mismatch", detail="other account",
            operator_attention_code="linkedin_account_mismatch", now=NOW)
        operation_id = confirm_replace(connection, assignment_id, now=NOW)
        operation = connection.execute(text("""
            SELECT kind, status FROM worker_operation WHERE id=:id
        """), {"id": operation_id}).one()

    assert operation._mapping == {"kind": "replace", "status": "queued"}


def test_retry_login_requeues_an_attention_required_assignment(app_url):
    from career_lens.fleet.assignments import (
        assign_account, request_login_retry, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in",
            target="attention_required", detail="username control unavailable",
            operator_attention_code="linkedin_signed_out",
            now=NOW)

        operation_id = request_login_retry(connection, assignment_id, now=NOW)
        repeated = request_login_retry(connection, assignment_id, now=NOW)
        assignment = connection.execute(text("""
            SELECT state, state_detail FROM account_worker_assignment WHERE id=:id
        """), {"id": assignment_id}).one()
        operation = connection.execute(text("""
            SELECT kind, status FROM worker_operation WHERE id=:id
        """), {"id": operation_id}).one()

    assert repeated == operation_id
    assert assignment._mapping == {"state": "login_pending", "state_detail": None}
    assert operation._mapping == {"kind": "login", "status": "queued"}


def test_concurrent_login_retries_return_the_same_operation(
        app_url, monkeypatch):
    from career_lens.fleet import assignments
    from career_lens.fleet.assignments import (
        assign_account, request_login_retry, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in",
            target="attention_required", detail="login timed out",
            operator_attention_code="linkedin_identity_unobservable", now=NOW)

    original = assignments._open_operation
    gate = Barrier(2)
    counter_lock = Lock()
    calls = 0

    def synchronized_open_operation(connection, requested_assignment, kind):
        nonlocal calls
        result = original(connection, requested_assignment, kind)
        with counter_lock:
            calls += 1
            synchronize = calls <= 2
        if synchronize:
            gate.wait(timeout=5)
        return result

    monkeypatch.setattr(
        assignments, "_open_operation", synchronized_open_operation)

    def retry():
        with _transaction(app_url) as connection:
            return request_login_retry(connection, assignment_id, now=NOW)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry(), range(2)))

    assert results[0] == results[1]


def test_retry_login_rejects_an_open_verify_operation(app_url):
    from career_lens.fleet.assignments import (
        InvalidAssignmentTransition, assign_account, request_login_retry,
        request_verify, transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in",
            target="attention_required", detail="login timed out",
            operator_attention_code="linkedin_identity_unobservable", now=NOW)
        request_verify(connection, assignment_id, now=NOW)

        with pytest.raises(
                InvalidAssignmentTransition, match="open verify operation"):
            request_login_retry(connection, assignment_id, now=NOW)


def test_retry_login_rejects_an_assignment_that_does_not_need_attention(app_url):
    from career_lens.fleet.assignments import (
        InvalidAssignmentTransition, assign_account, request_login_retry,
        transition_assignment,
    )

    with _transaction(app_url) as connection:
        _fleet(connection)
        assignment_id, _ = assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="login_pending",
            target="logging_in", now=NOW)
        transition_assignment(
            connection, assignment_id, expected="logging_in", target="ready",
            now=NOW)

        with pytest.raises(InvalidAssignmentTransition, match="cannot retry login"):
            request_login_retry(connection, assignment_id, now=NOW)


def test_assignment_commands_require_a_real_transaction(app):
    from career_lens.fleet.assignments import AssignmentError, assign_account

    with pytest.raises(AssignmentError, match="real database transaction"):
        assign_account(app, account_id="la-a", worker_id="worker-a", now=NOW)
