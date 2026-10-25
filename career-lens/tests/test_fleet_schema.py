"""Database-enforced invariants for workers, workers, and accounts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from career_lens.store.ids import ASSIGNMENT, LINKEDIN_ACCOUNT, REQUEST, new_id

NOW = datetime.now(UTC)


def _worker(connection, supervisor_id: str = "worker-a") -> str:
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES (:id, 'Worker A', 'active', 'boot-a', 'host-a', '0.1', :t, :t)
    """), {"id": supervisor_id, "t": NOW})
    return supervisor_id


def _slot(connection, worker_id: str, supervisor_id: str = "worker-a") -> str:
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES (:id, :worker, :id, 'enabled', 'healthy', :t)
    """), {"id": worker_id, "worker": supervisor_id, "t": NOW})
    return worker_id


def _account(connection, username: str) -> str:
    account_id = new_id(LINKEDIN_ACCOUNT)
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES (:id, :username, :username, 'enabled', 20, 300, :t, :t)
    """), {"id": account_id, "username": username, "t": NOW})
    return account_id


def _assignment(connection, account_id: str, worker_id: str) -> str:
    assignment_id = new_id(ASSIGNMENT)
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at)
        VALUES (:id, :account, :worker, 'login_pending', :t)
    """), {"id": assignment_id, "account": account_id, "worker": worker_id,
           "t": NOW})
    return assignment_id


def test_one_account_can_only_have_one_open_assignment(app):
    _worker(app)
    first = _slot(app, "worker-a")
    second = _slot(app, "worker-b")
    account = _account(app, "one@example.com")
    _assignment(app, account, first)

    with pytest.raises(IntegrityError):
        _assignment(app, account, second)


def test_one_slot_can_only_have_one_open_assignment(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    first = _account(app, "one@example.com")
    second = _account(app, "two@example.com")
    _assignment(app, first, worker)

    with pytest.raises(IntegrityError):
        _assignment(app, second, worker)


def test_ended_assignment_releases_both_resources(app):
    _worker(app)
    first = _slot(app, "worker-a")
    second = _slot(app, "worker-b")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, first)
    app.execute(text("""
        UPDATE account_worker_assignment
        SET state = 'ended', ended_at = :t WHERE id = :id
    """), {"id": assignment, "t": NOW})

    assert _assignment(app, account, second)


def test_an_assignment_cannot_claim_to_end_without_a_time(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)

    with pytest.raises(IntegrityError):
        app.execute(text(
            "UPDATE account_worker_assignment SET state = 'ended' WHERE id = :id"),
            {"id": assignment})


def test_assignment_can_record_the_host_that_verified_its_session(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)

    app.execute(text("""
        UPDATE account_worker_assignment
        SET verified_host='in.linkedin.com' WHERE id=:id
    """), {"id": assignment})

    assert app.execute(text("""
        SELECT verified_host FROM account_worker_assignment WHERE id=:id
    """), {"id": assignment}).scalar_one() == "in.linkedin.com"


def test_assignment_rejects_a_non_linkedin_verification_host(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)

    with pytest.raises(IntegrityError):
        app.execute(text("""
            UPDATE account_worker_assignment
            SET verified_host='example.com' WHERE id=:id
        """), {"id": assignment})


@pytest.mark.parametrize(
    ("table", "state_column", "blocked_state"),
    [
        ("worker", "health", "attention_required"),
        ("worker", "health", "quarantined"),
        ("linkedin_account", "state", "attention_required"),
        ("account_worker_assignment", "state", "attention_required"),
        ("account_worker_assignment", "state", "account_mismatch"),
    ],
)
def test_operator_blocking_states_require_a_reason_code(
        app, table, state_column, blocked_state):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)
    row_id = {
        "worker": worker,
        "linkedin_account": account,
        "account_worker_assignment": assignment,
    }[table]

    with pytest.raises(IntegrityError):
        app.execute(text(f"""
            UPDATE {table} SET {state_column}=:state,
                operator_attention_code=NULL
            WHERE id=:id
        """), {"state": blocked_state, "id": row_id})


@pytest.mark.parametrize(
    ("table", "state_column", "ordinary_state"),
    [
        ("worker", "health", "healthy"),
        ("linkedin_account", "state", "enabled"),
        ("account_worker_assignment", "state", "ready"),
    ],
)
def test_nonblocking_states_reject_an_attention_code(
        app, table, state_column, ordinary_state):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)
    row_id = {
        "worker": worker,
        "linkedin_account": account,
        "account_worker_assignment": assignment,
    }[table]

    with pytest.raises(IntegrityError):
        app.execute(text(f"""
            UPDATE {table} SET {state_column}=:state,
                operator_attention_code='manual_hold'
            WHERE id=:id
        """), {"state": ordinary_state, "id": row_id})


def test_slot_operation_records_its_queue_job(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    assignment = _assignment(app, account, worker)

    stored = app.execute(text("""
        INSERT INTO worker_operation
            (id, worker_id, assignment_id, kind, status, requested_at,
             queue_job_id)
        VALUES ('so-a', :worker, :assignment, 'verify', 'queued', :now, 42)
        RETURNING queue_job_id
    """), {"worker": worker, "assignment": assignment,
             "now": NOW}).scalar_one()

    assert stored == 42


def test_account_events_are_append_only_for_app_and_owner(app, migrator):
    _worker(app)
    worker = _slot(app, "worker-a")
    account = _account(app, "one@example.com")
    event_id = app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at)
        VALUES (:account, :worker, 'blocked', :t) RETURNING id
    """), {"account": account, "worker": worker, "t": NOW}).scalar_one()

    with pytest.raises(DBAPIError) as app_error:
        app.execute(text(
            "UPDATE linkedin_account_event SET kind = 'checkpoint' WHERE id = :id"),
            {"id": event_id})
    assert app_error.value.orig.sqlstate == "42501"

    with pytest.raises(DBAPIError) as owner_error:
        migrator.execute(text(
            "UPDATE linkedin_account_event SET kind = 'checkpoint' WHERE id = :id"),
            {"id": event_id})
    assert owner_error.value.orig.sqlstate == "CL001"


def test_slot_capabilities_must_be_a_json_array(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    assert app.execute(text("""
        SELECT capabilities FROM worker WHERE id=:worker
    """), {"worker": worker}).scalar_one() == []

    with pytest.raises(IntegrityError):
        app.execute(text("""
            UPDATE worker SET capabilities='{"google_search": true}'::jsonb
            WHERE id=:worker
        """), {"worker": worker})


def test_google_policy_defaults_are_independent_of_linkedin(app):
    _worker(app)
    worker = _slot(app, "worker-a")
    app.execute(text("""
        INSERT INTO worker_google_policy (worker_id) VALUES (:worker)
    """), {"worker": worker})

    policy = app.execute(text("""
        SELECT enabled, max_searches_hour, max_searches_24h
        FROM worker_google_policy WHERE worker_id=:worker
    """), {"worker": worker}).one()
    assert tuple(policy) == (True, 120, 1000)


@pytest.mark.parametrize("column", ["max_searches_hour", "max_searches_24h"])
def test_google_policy_limits_must_be_positive(app, column):
    _worker(app)
    worker = _slot(app, "worker-a")
    with pytest.raises(IntegrityError):
        app.execute(text(f"""
            INSERT INTO worker_google_policy (worker_id, {column})
            VALUES (:worker, 0)
        """), {"worker": worker})


def test_google_policy_is_mutable_but_search_events_are_append_only(
        app, migrator):
    _worker(app)
    worker = _slot(app, "worker-a")
    app.execute(text("""
        INSERT INTO worker_google_policy
            (worker_id, max_searches_hour, max_searches_24h)
        VALUES (:worker, 5, 20)
    """), {"worker": worker})
    app.execute(text("""
        UPDATE worker_google_policy
        SET max_searches_hour=6 WHERE worker_id=:worker
    """), {"worker": worker})

    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:request, 'AI', 'ai', 1, 'google_search', 'running', :now)
    """), {"request": request_id, "now": NOW})
    attempt_id = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker_id, started_at)
        VALUES (:request, 1, :worker, :now) RETURNING id
    """), {
        "request": request_id, "worker": worker, "now": NOW,
    }).scalar_one()
    event_id = app.execute(text("""
        INSERT INTO worker_google_event
            (worker_id, request_id, attempt_id, occurred_at, google_host)
        VALUES (:worker, :request, :attempt, :now, 'www.google.com')
        RETURNING id
    """), {
        "worker": worker, "request": request_id, "attempt": attempt_id,
        "now": NOW,
    }).scalar_one()

    with pytest.raises(DBAPIError) as app_error:
        app.execute(text("""
            UPDATE worker_google_event SET google_host='google.co.in'
            WHERE id=:id
        """), {"id": event_id})
    assert app_error.value.orig.sqlstate == "42501"

    with pytest.raises(DBAPIError) as owner_error:
        migrator.execute(text("""
            UPDATE worker_google_event SET google_host='google.co.in'
            WHERE id=:id
        """), {"id": event_id})
    assert owner_error.value.orig.sqlstate == "CL001"


def test_request_accepts_assigned_and_deduplicates_it(app):
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES ('rq-assigned', 'https://example.com/a', 'https://example.com/a',
                'generic', 'assigned', :t)
    """), {"t": NOW})

    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status, requested_at)
            VALUES ('rq-second', 'https://example.com/a', 'https://example.com/a',
                    'generic', 'queued', :t)
        """), {"t": NOW})
