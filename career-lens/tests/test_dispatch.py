"""Queued requests bind late to an existing ready assignment."""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from career_lens.store.ids import REQUEST, new_id

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class First:
    def uniform(self, start, end):
        return start


def _fleet(connection):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-a', 'Laptop', 'active', 'instance-a', 'host-a',
                'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES ('worker-a', 'worker-a', 'research', 'enabled', 'healthy', :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-a', 'Research', 'a@example.com',
                'https://www.linkedin.com/in/alice/', 'enabled', 20, 0,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-a', 'la-a', 'worker-a', 'ready', :now, :now,
                'www.linkedin.com')
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_schedule
            (account_id, enabled, timezone, seed, start_jitter_minutes,
             end_jitter_minutes, created_at, updated_at)
        VALUES ('la-a', true, 'UTC', 1, 0, 0, :now, :now)
    """), {"now": NOW})
    day_id = connection.execute(text("""
        INSERT INTO account_shift_day (account_id, local_date, generated_at)
        VALUES ('la-a', :today, :now)
        RETURNING id
    """), {"today": NOW.date(), "now": NOW}).scalar_one()
    connection.execute(text("""
        INSERT INTO account_shift_block
            (shift_day_id, account_id, ordinal, kind, starts_at, ends_at,
             pace)
        VALUES (:day, 'la-a', 0, 'work', :starts, :ends, 1.0)
    """), {
        "day": day_id, "starts": NOW - timedelta(hours=1),
        "ends": NOW + timedelta(hours=1),
    })


def _enable_google(connection, worker_id="worker-a"):
    connection.execute(text("""
        UPDATE worker
        SET capabilities='["google_search"]'::jsonb
        WHERE id=:worker
    """), {"worker": worker_id})
    connection.execute(text("""
        INSERT INTO worker_google_policy
            (worker_id, max_searches_hour, max_searches_24h)
        VALUES (:worker, 10, 20)
    """), {"worker": worker_id})


def _request(connection, at=NOW):
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :url, :url, 'linkedin_person', 'queued', :at)
    """), {"id": request_id, "url": f"https://www.linkedin.com/in/{request_id}/",
             "at": at})
    return request_id


def _linkedin_request(connection, at=NOW):
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :url, :url, 'linkedin_person', 'queued', :at)
    """), {
        "id": request_id,
        "url": f"https://www.linkedin.com/in/{request_id}/",
        "at": at,
    })
    return request_id


def _linkedin_company_request(connection, *, sections=None, at=NOW):
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at,
             linkedin_sections)
        VALUES (:id, :url, :url, 'linkedin_company', 'queued', :at,
                CAST(:sections AS jsonb))
    """), {
        "id": request_id,
        "url": f"https://www.linkedin.com/company/{request_id}/",
        "at": at,
        "sections": None if sections is None else json.dumps(sections),
    })
    return request_id


def test_due_account_recovery_queues_login_before_scrape(app_url):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _linkedin_request(connection)
        connection.execute(text("""
            UPDATE account_worker_assignment
            SET state='attention_required',
                operator_attention_code='linkedin_signed_out'
            WHERE id='as-a'
        """))
        connection.execute(text("""
            UPDATE linkedin_account
            SET recovery_phase='before_relogin',
                recovery_not_before=:due
            WHERE id='la-a'
        """), {"due": NOW - timedelta(seconds=1)})

        assert dispatch_one(connection, now=NOW, rng=First()) is None
        operation = connection.execute(text("""
            SELECT kind, status FROM worker_operation
            WHERE assignment_id='as-a'
        """)).one()
        state = connection.execute(text("""
            SELECT state FROM account_worker_assignment WHERE id='as-a'
        """)).scalar_one()
        wait = connection.execute(text("""
            SELECT dispatch_wait_reason FROM scrape_request WHERE id=:request
        """), {"request": request_id}).scalar_one()

    assert operation == ("login", "queued")
    assert state == "login_pending"
    assert wait == "relogin_pending"


def _ignore_profile_extraction(monkeypatch, tasks):
    """Keep runner-policy tests independent of their empty capture stub."""
    monkeypatch.setitem(
        tasks.EXTRACTORS, "linkedin_person", lambda *args, **kwargs: None)


def _google_request(
    connection, at=NOW, *, top_n=3, query="career lens", priority="normal"
):
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n, priority,
             kind, status, requested_at)
        VALUES (:id, :query, :query, :top_n, :priority,
                'google_search', 'queued', :at)
    """), {
        "id": request_id,
        "at": at,
        "top_n": top_n,
        "query": query,
        "priority": priority,
    })
    return request_id


def test_dispatch_targets_the_selected_slot(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT r.status, a.supervisor_id, a.worker_id, a.account_id, a.assignment_id,
               a.started_at, a.queue_job_id
        FROM scrape_request r JOIN scrape_attempt a ON a.request_id=r.id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    job = app.execute(text("""
        SELECT queue_name, task_name, lock, queueing_lock, args
        FROM procrastinate.procrastinate_jobs WHERE id=:job
    """), {"job": row.queue_job_id}).one()
    assert attempt_id is not None
    assert row.status == "assigned"
    assert (row.supervisor_id, row.worker_id, row.account_id, row.assignment_id) == (
        "worker-a", "worker-a", "la-a", "as-a")
    assert row.started_at is None
    assert (job.queue_name, job.task_name, job.lock, job.queueing_lock) == (
        "capture:worker-a", "scrape_assigned", "worker:worker-a",
        f"attempt:{attempt_id}")
    assert job.args == {"attempt_id": attempt_id}


def test_native_linkedin_worker_keeps_implicit_person_capability(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _linkedin_request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assert attempt_id is not None
    assert app.execute(text("""
        SELECT queue_job_id FROM scrape_attempt WHERE request_id=:request
    """), {"request": request_id}).scalar_one() is not None


def test_a_company_request_goes_to_a_native_worker(
        app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _linkedin_company_request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT request_id, worker_id, queue_job_id
        FROM scrape_attempt WHERE id=:attempt
    """), {"attempt": attempt_id}).one()

    assert row.request_id == request_id
    assert row.worker_id == "worker-a"
    assert row.queue_job_id is not None


def test_dispatch_routes_google_search_through_the_same_ready_profile(
        app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _enable_google(connection)
        request_id = _google_request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT request.status, request.kind, attempt.supervisor_id,
               attempt.worker_id, attempt.account_id, attempt.assignment_id,
               attempt.queue_job_id
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert attempt_id is not None
    assert row._mapping == {
        "status": "assigned", "kind": "google_search",
        "supervisor_id": "worker-a", "worker_id": "worker-a",
        "account_id": "la-a", "assignment_id": "as-a",
        "queue_job_id": row.queue_job_id,
    }
    assert row.queue_job_id is not None


def test_serp_only_search_dispatches_without_a_provider_account(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _public_worker(connection)
        _enable_google(connection, "worker-public")
        request_id = _google_request(connection, top_n=0)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT request.status, attempt.supervisor_id, attempt.worker_id,
               attempt.account_id, attempt.assignment_id, attempt.queue_job_id
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert attempt_id is not None
    assert row.status == "assigned"
    assert (row.supervisor_id, row.worker_id) == (
        "supervisor-public", "worker-public")
    assert row.account_id is None
    assert row.assignment_id is None
    assert row.queue_job_id is not None


def test_high_priority_search_runs_before_older_normal_search(app_url, app):
    from datetime import timedelta

    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _public_worker(connection)
        _enable_google(connection, "worker-public")
        normal_id = _google_request(
            connection,
            NOW - timedelta(seconds=1),
            top_n=0,
            query="person discovery",
        )
        high_id = _google_request(
            connection,
            top_n=0,
            query="company discovery",
            priority="high",
        )
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assigned_id = app.execute(text("""
        SELECT request_id FROM scrape_attempt WHERE id=:attempt
    """), {"attempt": attempt_id}).scalar_one()
    assert assigned_id == high_id
    assert app.execute(text("""
        SELECT status FROM scrape_request WHERE id=:request
    """), {"request": normal_id}).scalar_one() == "queued"


def test_blocked_oldest_google_request_does_not_stop_newer_runnable_work(
        app_url, app):
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)  # A LinkedIn-ready profile without Google capability.
        google_id = _google_request(connection, NOW - timedelta(seconds=1))
        linked_in_id = _request(connection, NOW)
        assert dispatch_one(connection, now=NOW, rng=First()) is not None

    rows = app.execute(text("""
        SELECT id, status, dispatch_wait_reason, dispatch_checked_at
        FROM scrape_request WHERE id IN (:google, :linkedin)
    """), {"google": google_id, "linkedin": linked_in_id}).all()
    by_id = {row.id: row for row in rows}
    assert by_id[google_id].status == "queued"
    assert by_id[google_id].dispatch_wait_reason == "no_google_capable_workers"
    assert by_id[google_id].dispatch_checked_at == NOW
    assert by_id[linked_in_id].status == "assigned"
    assert by_id[linked_in_id].dispatch_checked_at == NOW


def test_dispatch_targets_a_slot_while_control_worker_pool_is_open(
        app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.queue.app import app as queue_app, make_app
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)

    def dispatch():
        with transaction(app_url) as connection:
            return dispatch_one(connection, now=NOW, rng=First())

    async def run_with_worker_pool():
        with queue_app.replace_connector(make_app(app_url).connector):
            async with queue_app.open_async():
                return await asyncio.to_thread(dispatch)

    attempt_id = asyncio.run(run_with_worker_pool())
    row = app.execute(text("""
        SELECT r.status, a.worker_id, a.queue_job_id
        FROM scrape_request r JOIN scrape_attempt a ON a.request_id=r.id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    assert attempt_id is not None
    assert row.status == "assigned"
    assert row.worker_id == "worker-a"
    assert row.queue_job_id is not None


def test_dispatch_considers_the_oldest_request_first(app_url, app):
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        oldest = _request(connection, NOW - timedelta(minutes=1))
        newest = _request(connection, NOW)
        dispatch_one(connection, now=NOW, rng=First())
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:id"),
        {"id": oldest}).scalar_one() == "assigned"
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:id"),
        {"id": newest}).scalar_one() == "queued"


def test_dispatch_records_why_the_oldest_request_waits(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id = _request(connection)
        assert dispatch_one(connection, now=NOW, rng=First()) is None
    row = app.execute(text("""
        SELECT status, dispatch_wait_reason, dispatch_checked_at
        FROM scrape_request WHERE id=:id
    """), {"id": request_id}).one()
    assert row.status == "queued"
    assert row.dispatch_wait_reason == "no_assignments"
    assert row.dispatch_checked_at == NOW


def test_second_dispatch_waits_while_the_only_slot_has_an_open_attempt(
        app_url, app):
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        first_request = _request(connection, NOW - timedelta(seconds=1))
        second_request = _request(connection)
        first_attempt = dispatch_one(connection, now=NOW, rng=First())
    with transaction(app_url) as connection:
        second_attempt = dispatch_one(connection, now=NOW, rng=First())

    assert first_attempt is not None
    assert second_attempt is None
    rows = app.execute(text("""
        SELECT id, status, dispatch_wait_reason
        FROM scrape_request ORDER BY id
    """)).all()
    by_id = {row.id: row for row in rows}
    assert by_id[first_request].status == "assigned"
    assert by_id[second_request].status == "queued"
    assert by_id[second_request].dispatch_wait_reason == "all_workers_busy"


def test_racing_dispatchers_assign_only_one_request_to_a_slot(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_ids = {_request(connection), _request(connection)}

    def dispatch():
        with transaction(app_url) as connection:
            return dispatch_one(connection, now=NOW, rng=First())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: dispatch(), range(2)))

    assert sum(result is not None for result in results) == 1
    rows = app.execute(text("""
        SELECT id, status, dispatch_wait_reason FROM scrape_request
    """)).all()
    assert {row.id for row in rows} == request_ids
    assert sorted(row.status for row in rows) == ["assigned", "queued"]
    waiting = next(row for row in rows if row.status == "queued")
    assert waiting.dispatch_wait_reason == "all_workers_busy"


def test_database_rejects_two_open_attempts_for_one_worker(app_url):
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        first = _request(connection)
        second = _request(connection)
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            VALUES (:request, 1, :now, 'worker-a', 'worker-a', 'la-a', 'as-a')
        """), {"request": first, "now": NOW})
    with pytest.raises(IntegrityError) as failure:
        with transaction(app_url) as connection:
            connection.execute(text("""
                INSERT INTO scrape_attempt
                    (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                     account_id, assignment_id)
                VALUES (:request, 1, :now, 'worker-a', 'worker-a',
                        'la-a', 'as-a')
            """), {"request": second, "now": NOW})
    assert "uq_scrape_attempt_open_worker" in str(failure.value)


def test_database_rejects_two_open_attempts_for_one_request(app_url):
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            VALUES (:request, 1, :now, 'worker-a', 'worker-a', 'la-a', 'as-a')
        """), {"request": request_id, "now": NOW})
    with pytest.raises(IntegrityError) as failure:
        with transaction(app_url) as connection:
            connection.execute(text("""
                INSERT INTO scrape_attempt
                    (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                     account_id, assignment_id)
                VALUES (:request, 2, :now, 'worker-a', NULL, NULL, 'as-a')
            """), {"request": request_id, "now": NOW})
    assert "uq_scrape_attempt_open_fleet_request" in str(failure.value)


def test_submission_queues_a_control_kick_not_a_capture(app_url, app):
    from career_lens.api.requests import submit

    request_id, created, *_ = submit("https://example.com/new", database=app_url)
    jobs = app.execute(text("""
        SELECT queue_name, task_name, queueing_lock, args
        FROM procrastinate.procrastinate_jobs
    """)).all()
    assert created is True
    assert len(jobs) == 1
    assert (jobs[0].queue_name, jobs[0].task_name,
            jobs[0].queueing_lock, jobs[0].args) == (
        "control", "dispatch_pending_requests", "dispatch:pending", {})
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:id"),
        {"id": request_id}).scalar_one() == "queued"


def test_dispatch_kicks_are_transactionally_coalesced(app_url, app):
    from career_lens.queue.enqueue import enqueue_dispatch
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        first = enqueue_dispatch(connection)
        second = enqueue_dispatch(connection)

    assert second == first
    assert app.execute(text("""
        SELECT count(*) FROM procrastinate.procrastinate_jobs
        WHERE queueing_lock='dispatch:pending'
    """)).scalar_one() == 1


def test_dispatch_kick_uses_worker_safe_sync_manager(
        app_url, monkeypatch):
    from career_lens.queue import enqueue as module
    from career_lens.store.db import transaction

    expected_manager = object()
    received = []

    class Configured:
        def defer(self):
            return 7

    def configured(**kwargs):
        received.append(kwargs["job_manager"])
        return Configured()

    monkeypatch.setattr(module, "sync_job_manager",
                        lambda: expected_manager)
    monkeypatch.setattr(module, "configure_task", configured)
    with transaction(app_url) as connection:
        assert module.enqueue_dispatch(connection) == 7
    assert received == [expected_manager]


def test_a_completed_dispatch_kick_does_not_block_the_next_one(app_url, app):
    from career_lens.queue.enqueue import enqueue_dispatch
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        first = enqueue_dispatch(connection)
    app.execute(text("SET search_path TO procrastinate, public"))
    app.execute(text("""
        UPDATE procrastinate.procrastinate_jobs SET status='succeeded'
        WHERE id=:job
    """), {"job": first})
    with transaction(app_url) as connection:
        second = enqueue_dispatch(connection)

    assert second != first


def test_control_pass_reconciles_before_dispatch(monkeypatch):
    from career_lens.queue import tasks

    calls = []
    monkeypatch.setattr(
        "career_lens.journal.sweep_stalled", lambda: calls.append("stalled"))
    monkeypatch.setattr(
        "career_lens.fleet.recovery.sweep_restarted_native_attempts",
        lambda: calls.append("restarted_native"))
    monkeypatch.setattr(
        "career_lens.fleet.recovery.sweep_orphaned_scrape_attempts",
        lambda: calls.append("scrape_orphaned"))
    monkeypatch.setattr(
        "career_lens.fleet.recovery.sweep_orphaned_worker_operations",
        lambda: calls.append("orphaned"))
    monkeypatch.setattr(
        "career_lens.fleet.dispatch.sweep_expired_claims",
        lambda: calls.append("unclaimed"))
    monkeypatch.setattr(
        "career_lens.fleet.dispatch.dispatch_pending",
        lambda: calls.append("dispatch"))

    tasks.dispatch_pending_requests()

    assert calls == [
        "restarted_native", "stalled", "scrape_orphaned", "orphaned",
        "unclaimed", "dispatch",
    ]


def test_assigned_runner_starts_budget_and_attributes_work(
        app_url, app, monkeypatch):
    from career_lens.capture import driver
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.linkedin.session import SessionObservation
    from career_lens.queue import tasks
    from career_lens.store.db import transaction
    from contextlib import contextmanager

    class Store:
        pass

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text("""
            UPDATE scrape_request
            SET tempo='human'
            WHERE id=:request
        """), {"request": request_id})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
    capture_options = []
    monkeypatch.setattr(
        tasks, "capture_request",
        lambda *a, **k: capture_options.append(k) or iter(()))
    monkeypatch.setattr(tasks, "open_store", Store)
    monkeypatch.setattr(driver, "LINKEDIN_GOOGLE_SEARCH_CHANCE", 1.0)
    _ignore_profile_extraction(monkeypatch, tasks)

    @contextmanager
    def page_session(*, profile):
        yield object()

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=lambda page: SessionObservation(
            "signed_in",
            "https://in.linkedin.com/in/alice/?isSelfProfile=true"))

    row = app.execute(text("""
        SELECT r.status, a.started_at, a.ended_at, a.cause
        FROM scrape_request r JOIN scrape_attempt a ON a.request_id=r.id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    assert row.status == "succeeded"
    assert row.started_at is not None and row.ended_at is not None
    assert row.cause is None
    assert capture_options[0]["tempo"].name == "human"
    assert capture_options[0]["discovery_chance"] == 1.0
    assert capture_options[0]["page"] is not None
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event
        WHERE attempt_id=:attempt AND kind='attempt_started'
    """), {"attempt": attempt_id}).scalar_one() == 1


def test_assigned_runner_skips_identity_navigation_when_verification_is_fresh(
        app_url, monkeypatch):
    from contextlib import contextmanager
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    class Store:
        pass

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
    monkeypatch.setattr(tasks, "capture_request", lambda *a, **k: iter(()))
    monkeypatch.setattr(tasks, "open_store", Store)
    _ignore_profile_extraction(monkeypatch, tasks)

    @contextmanager
    def page_session(*, profile):
        yield object()

    def unexpected_observation(page):
        raise AssertionError("fresh verification must not navigate to /in/me/")

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=unexpected_observation)

    with transaction(app_url) as connection:
        assert connection.execute(text("""
            SELECT status FROM scrape_request WHERE id=:request
        """), {"request": request_id}).scalar_one() == "succeeded"


def test_serp_only_runner_never_checks_a_linkedin_session(
        app_url, app, monkeypatch):
    from contextlib import contextmanager

    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        heartbeat = datetime.now(UTC)
        _public_worker(
            connection, supervisor_beat=heartbeat, worker_beat=heartbeat)
        _enable_google(connection, "worker-public")
        request_id = _google_request(connection, top_n=0)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    calls = []
    monkeypatch.setattr(
        tasks,
        "_work_google",
        lambda *args, **kwargs: calls.append(kwargs["browser_profile"]),
    )

    @contextmanager
    def page_session(*, profile):
        yield object()

    tasks.run_assigned(
        attempt_id,
        url=app_url,
        session_factory=page_session,
        observer=lambda page: pytest.fail("SERP-only search must not inspect LinkedIn"),
    )

    assert calls == ["profile-worker-public"]
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:request"
    ), {"request": request_id}).scalar_one() == "succeeded"


def test_capture_login_wall_quarantines_the_identity_before_retry(
        app_url, app, monkeypatch):
    """A fresh `/in/me/` observation cannot outlive a later authwall.

    The production incident reached LinkedIn successfully, then encountered an
    authwall during the target capture.  That failure must close through the
    same quarantine path as an authwall seen during session verification;
    otherwise the immediate retry can run on the just-failed account again.
    """
    from contextlib import contextmanager
    from career_lens.capture.driver import CaptureFailed
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    def authwall(*args, **kwargs):
        raise CaptureFailed(
            "login_required", "linkedin_authwall: url contains '/authwall'")

    @contextmanager
    def page_session(*, profile):
        yield object()

    monkeypatch.setattr(tasks, "_work", authwall)
    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=lambda page: pytest.fail(
            "fresh verification must not navigate to /in/me/"))

    row = app.execute(text("""
        SELECT request.status, attempt.cause,
               ass.state AS assignment_state,
               worker.health AS worker_health,
               account.state AS account_state,
               account.recovery_phase,
               ass.operator_attention_code AS assignment_code,
               worker.operator_attention_code AS worker_code,
               account.operator_attention_code AS account_code
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        JOIN worker ON worker.id=attempt.worker_id
        JOIN linkedin_account account ON account.id=attempt.account_id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert row._mapping == {
        "status": "queued",
        "cause": "login_required",
        "assignment_state": "attention_required",
        # Untouched: the browser worked, the session did not.
        "worker_health": "healthy",
        "account_state": "enabled",
        "recovery_phase": "before_relogin",
        "assignment_code": "linkedin_signed_out",
        "worker_code": None,
        "account_code": None,
    }

    # Even though the retry is due immediately, the failed identity is no
    # longer eligible.  With no other account, it waits for an operator rather
    # than spending attempts two and three against the same authwall.
    checked_at = datetime.now(UTC) + timedelta(seconds=1)
    with transaction(app_url) as connection:
        connection.execute(text("""
            UPDATE supervisor SET last_heartbeat_at=:now
        """), {"now": checked_at})
        connection.execute(text("""
            UPDATE worker SET last_heartbeat_at=:now
        """), {"now": checked_at})
        assert dispatch_one(
            connection, now=checked_at, rng=First()) is None
    assert app.execute(text("""
        SELECT count(*) FROM scrape_attempt WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 1


def test_verification_does_not_expire_while_the_worker_instance_is_unchanged(
        app_url, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    class Store:
        pass

    with transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text("""
            UPDATE supervisor SET started_at=:started WHERE id='worker-a'
        """), {"started": NOW - timedelta(days=30)})
        connection.execute(text("""
            UPDATE account_worker_assignment SET verified_at=:verified
            WHERE id='as-a'
        """), {"verified": NOW - timedelta(days=29)})
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
    monkeypatch.setattr(tasks, "capture_request", lambda *a, **k: iter(()))
    monkeypatch.setattr(tasks, "open_store", Store)
    _ignore_profile_extraction(monkeypatch, tasks)

    @contextmanager
    def page_session(*, profile):
        yield object()

    def unexpected_observation(page):
        raise AssertionError("verification has no arbitrary time-to-live")

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=unexpected_observation)

    with transaction(app_url) as connection:
        assert connection.execute(text("""
            SELECT status FROM scrape_request WHERE id=:request
        """), {"request": request_id}).scalar_one() == "succeeded"


def test_assigned_runner_reverifies_after_worker_restart(
        app_url, app, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.linkedin.session import SessionObservation
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    class Store:
        pass

    restarted = NOW + timedelta(minutes=1)
    with transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text("""
            UPDATE supervisor
            SET started_at=:restarted, last_heartbeat_at=:restarted
            WHERE id='worker-a'
        """), {"restarted": restarted})
        connection.execute(text("""
            UPDATE worker SET last_heartbeat_at=:restarted
            WHERE id='worker-a'
        """), {"restarted": restarted})
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=restarted, rng=First())
    monkeypatch.setattr(tasks, "capture_request", lambda *a, **k: iter(()))
    monkeypatch.setattr(tasks, "open_store", Store)
    _ignore_profile_extraction(monkeypatch, tasks)
    observations = []

    @contextmanager
    def page_session(*, profile):
        yield object()

    def observe(page):
        observations.append(True)
        return SessionObservation(
            "signed_in", "https://www.linkedin.com/in/alice/",
            host="in.linkedin.com")

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=observe)

    row = app.execute(text("""
        SELECT request.status, ass.verified_at, ass.verified_host
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert observations == [True]
    assert row.status == "succeeded"
    assert row.verified_at > restarted
    assert row.verified_host == "in.linkedin.com"
    assert app.execute(text("""
        SELECT count(*)
        FROM attempt_event
        WHERE attempt_id=:attempt AND kind='work'
          AND phase='store session verification'
    """), {"attempt": attempt_id}).scalar_one() == 1


def test_assigned_runner_never_captures_with_a_mismatched_identity(
        app_url, app, monkeypatch):
    from contextlib import contextmanager
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.linkedin.session import SessionObservation
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text("""
            UPDATE account_worker_assignment
            SET verified_at=NULL, verified_host=NULL WHERE id='as-a'
        """))
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
    captures = []
    monkeypatch.setattr(
        tasks, "capture_request", lambda *a, **k: captures.append(True))

    @contextmanager
    def page_session(*, profile):
        yield object()

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=lambda page: SessionObservation(
            "signed_in", "https://www.linkedin.com/in/bob/"))
    row = app.execute(text("""
        SELECT r.status, attempt.cause, ass.state, worker.health,
               ass.operator_attention_code AS assignment_code,
               worker.operator_attention_code AS worker_code
        FROM scrape_request r
        JOIN scrape_attempt attempt ON attempt.request_id=r.id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        JOIN worker worker ON worker.id=attempt.worker_id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    assert captures == []
    assert row._mapping == {
        # Queued, not failed: a mismatched identity is worth another attempt
        # elsewhere. The quarantine below is what this test is about, and it
        # happens either way.
        "status": "queued", "cause": "account_mismatch",
        "state": "account_mismatch", "health": "healthy",
        "assignment_code": "linkedin_account_mismatch",
        "worker_code": None,
    }
    assert app.execute(text("""
        SELECT count(*)
        FROM attempt_event
        WHERE attempt_id=:attempt AND kind='work'
          AND phase='finalize request'
    """), {"attempt": attempt_id}).scalar_one() == 1


def test_assigned_runner_does_not_report_inconclusive_identity_as_mismatch(
        app_url, app, monkeypatch):
    from contextlib import contextmanager
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.linkedin.session import SessionObservation
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        connection.execute(text("""
            UPDATE account_worker_assignment
            SET verified_at=NULL, verified_host=NULL WHERE id='as-a'
        """))
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
    captures = []
    monkeypatch.setattr(
        tasks, "capture_request", lambda *a, **k: captures.append(True))

    @contextmanager
    def page_session(*, profile):
        yield object()

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=page_session,
        observer=lambda page: SessionObservation(
            "unknown", detail="identity redirect was inconclusive"))
    row = app.execute(text("""
        SELECT r.status, attempt.cause, attempt.cause_detail,
               ass.state, worker.health, account.state AS account_state,
               ass.operator_attention_code AS assignment_code,
               worker.operator_attention_code AS worker_code,
               account.operator_attention_code AS account_code
        FROM scrape_request r
        JOIN scrape_attempt attempt ON attempt.request_id=r.id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        JOIN worker worker ON worker.id=attempt.worker_id
        JOIN linkedin_account account ON account.id=attempt.account_id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    assert captures == []
    assert row._mapping == {
        # Queued, not failed: a browser error earns a retry. What this test
        # asserts is that an inconclusive observation is not called a mismatch.
        "status": "queued",
        "cause": "browser_error",
        "cause_detail": "identity redirect was inconclusive",
        "state": "attention_required",
        "health": "healthy",
        "account_state": "attention_required",
        "assignment_code": "linkedin_identity_unobservable",
        "worker_code": None,
        "account_code": "linkedin_identity_unobservable",
    }


def test_unclaimed_attempt_is_requeued_without_quarantining_slot(app_url, app):
    from datetime import timedelta
    from career_lens.fleet.dispatch import dispatch_one, sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        dispatch_one(connection, now=NOW, rng=First())
    assert sweep_expired_claims(
        database=app_url, now=NOW + timedelta(minutes=11)) == [request_id]
    row = app.execute(text("""
        SELECT r.status, attempt.cause, ass.state, ass.ended_at, worker.health
        FROM scrape_request r
        JOIN scrape_attempt attempt ON attempt.request_id=r.id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        JOIN worker worker ON worker.id=attempt.worker_id
        WHERE r.id=:request
    """), {"request": request_id}).one()
    assert row.status == "queued" and row.cause == "worker_lost"
    assert row.state == "ready" and row.ended_at is None
    assert row.health == "healthy"


def test_stale_native_worker_keeps_its_unclaimed_attempt_until_claim_deadline(
        app_url, app):
    """Native queue delivery retains its existing ten-minute claim window."""
    from career_lens.fleet.dispatch import dispatch_one, sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        assert dispatch_one(connection, now=NOW, rng=First()) is not None

    assert sweep_expired_claims(
        database=app_url, now=NOW + timedelta(seconds=31),
    ) == []
    row = app.execute(text("""
        SELECT request.status, attempt.ended_at
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert row._mapping == {"status": "assigned", "ended_at": None}


def test_restarted_native_supervisor_requeues_a_started_attempt(app_url, app):
    from career_lens.fleet.recovery import sweep_restarted_native_attempts
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.journal import Journal
    from career_lens.store.db import transaction

    restarted = NOW + timedelta(minutes=1)
    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
        Journal.start(connection, attempt_id, url=app_url)
        connection.execute(text("""
            UPDATE supervisor
            SET started_at=:restarted, last_heartbeat_at=:restarted
            WHERE id='worker-a'
        """), {"restarted": restarted})
        connection.execute(text("""
            UPDATE worker SET last_heartbeat_at=:restarted WHERE id='worker-a'
        """), {"restarted": restarted})

    assert sweep_restarted_native_attempts(
        database=app_url, now=restarted) == [request_id]
    row = app.execute(text("""
        SELECT request.status, attempt.cause, worker.health
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN worker ON worker.id=attempt.worker_id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert row._mapping == {
        "status": "queued", "cause": "worker_lost", "health": "healthy",
    }


def test_repeated_unclaimed_attempts_stay_queued_without_spending_budget(
        app_url, app):
    from datetime import timedelta
    from career_lens.fleet import retry
    from career_lens.fleet.dispatch import dispatch_one, sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)

    for expiration in range(1, 4):
        # A lost claim now schedules its retry a minute out, so each round has
        # to dispatch after the previous round's backoff has passed.
        dispatched_at = NOW + timedelta(minutes=20 * (expiration - 1))
        with transaction(app_url) as connection:
            # The fleet is still alive at each round: this test is about claim
            # expiry, and a stale heartbeat would fail it for the wrong reason.
            connection.execute(text(
                "UPDATE supervisor SET last_heartbeat_at=:now"),
                {"now": dispatched_at})
            connection.execute(text(
                "UPDATE worker SET last_heartbeat_at=:now"),
                {"now": dispatched_at})
            assert dispatch_one(
                connection, now=dispatched_at, rng=First()) is not None
        assert sweep_expired_claims(
            database=app_url,
            now=dispatched_at + timedelta(minutes=11),
        ) == [request_id]
        assert app.execute(text("""
            SELECT status FROM scrape_request WHERE id=:request
        """), {"request": request_id}).scalar_one() == "queued"

    attempts = app.execute(text("""
        SELECT cause, started_at, ended_at
        FROM scrape_attempt WHERE request_id=:request ORDER BY attempt_no
    """), {"request": request_id}).all()
    assert len(attempts) == 3
    assert all(row.cause == "worker_lost" for row in attempts)
    assert all(row.started_at is None and row.ended_at is not None
               for row in attempts)
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event
        WHERE kind='attempt_started'
    """)).scalar_one() == 0
    assert retry.attempts_spent(app, request_id) == 0
    resources = app.execute(text("""
        SELECT ass.state, ass.ended_at, account.state AS account_state,
               worker.health
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE ass.id='as-a'
    """)).one()
    assert resources._mapping == {
        "state": "ready", "ended_at": None,
        "account_state": "enabled", "health": "healthy",
    }


def test_stalled_started_attempt_marks_its_slot_for_attention(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.journal import Journal, sweep_stalled
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
        Journal.start(connection, attempt_id, url=app_url)

    class Stalled:
        id = 42
        task_kwargs = {"attempt_id": attempt_id}

    assert sweep_stalled(
        app_url, stalled=lambda: [Stalled()], finish=lambda job: None,
    ) == [request_id]
    row = app.execute(text("""
        SELECT attempt.cause, request.status, worker.health, ass.state,
               ass.ended_at, worker.operator_attention_code AS worker_code
        FROM scrape_attempt attempt
        JOIN scrape_request request ON request.id=attempt.request_id
        JOIN worker worker ON worker.id=attempt.worker_id
        JOIN account_worker_assignment ass ON ass.id=attempt.assignment_id
        WHERE attempt.id=:attempt
    """), {"attempt": attempt_id}).one()
    assert row._mapping == {
        "cause": "worker_lost", "status": "queued",
        "health": "attention_required", "state": "ready", "ended_at": None,
        "worker_code": "capture_worker_lost",
    }


def test_a_worker_lost_mid_capture_records_when_it_was_lost(app_url, app):
    """Recovery compares this against the heartbeat, so it cannot be missing."""
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.journal import Journal, sweep_stalled
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
        Journal.start(connection, attempt_id, url=app_url)

    class Stalled:
        id = 42
        task_kwargs = {"attempt_id": attempt_id}

    sweep_stalled(app_url, stalled=lambda: [Stalled()], finish=lambda job: None)

    recorded = app.execute(text("""
        SELECT worker.attention_recorded_at
        FROM worker JOIN scrape_attempt attempt ON attempt.worker_id=worker.id
        WHERE attempt.id=:attempt
    """), {"attempt": attempt_id}).scalar_one()
    assert recorded is not None


def _second_account(connection):
    """A second ready assignment on its own worker, so a retry has somewhere
    else to go."""
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES ('worker-b', 'worker-a', 'research-2', 'enabled', 'healthy', :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-b', 'Research 2', 'b@example.com',
                'https://www.linkedin.com/in/bob/', 'enabled', 20, 0,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-b', 'la-b', 'worker-b', 'ready', :now, :now,
                'www.linkedin.com')
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_schedule
            (account_id, enabled, timezone, seed, start_jitter_minutes,
             end_jitter_minutes, created_at, updated_at)
        VALUES ('la-b', true, 'UTC', 1, 0, 0, :now, :now)
    """), {"now": NOW})
    day_id = connection.execute(text("""
        INSERT INTO account_shift_day (account_id, local_date, generated_at)
        VALUES ('la-b', :today, :now)
        RETURNING id
    """), {"today": NOW.date(), "now": NOW}).scalar_one()
    connection.execute(text("""
        INSERT INTO account_shift_block
            (shift_day_id, account_id, ordinal, kind, starts_at, ends_at,
             pace)
        VALUES (:day, 'la-b', 0, 'work', :starts, :ends, 1.0)
    """), {
        "day": day_id, "starts": NOW - timedelta(hours=1),
        "ends": NOW + timedelta(hours=1),
    })


def test_a_request_in_backoff_is_not_dispatched_before_it_is_due(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET next_attempt_at=:due WHERE id=:r"),
            {"r": request_id, "due": NOW + timedelta(minutes=2)})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assert attempt_id is None
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "queued"


def test_dispatch_clears_the_due_time_it_has_just_honoured(app_url, app):
    """A due time is a promise to run at that moment. Once the dispatcher has
    kept it, leaving it on the row advertises a promise already met, which
    reads as a pending retry to anyone querying the table."""
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET next_attempt_at=:due WHERE id=:r"),
            {"r": request_id, "due": NOW - timedelta(minutes=1)})
        dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("assigned", None)


def test_a_request_in_backoff_is_dispatched_once_it_is_due(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET next_attempt_at=:due WHERE id=:r"),
            {"r": request_id, "due": NOW + timedelta(seconds=20)})
        attempt_id = dispatch_one(
            connection, now=NOW + timedelta(seconds=25), rng=First())

    assert attempt_id is not None
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "assigned"


def test_a_backed_off_request_does_not_block_the_one_behind_it(app_url, app):
    """dispatch_one considers the oldest eligible request. If the due time were
    checked after selection rather than in it, the oldest request would be
    picked, rejected, and everything behind it would starve."""
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        waiting = _request(connection, at=NOW - timedelta(minutes=10))
        ready = _request(connection, at=NOW - timedelta(minutes=1))
        connection.execute(text(
            "UPDATE scrape_request SET next_attempt_at=:due WHERE id=:r"),
            {"r": waiting, "due": NOW + timedelta(minutes=5)})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assert attempt_id is not None
    assert app.execute(text(
        "SELECT request_id FROM scrape_attempt WHERE id=:a"),
        {"a": attempt_id}).scalar_one() == ready


def test_a_retry_lands_on_an_account_the_request_has_not_tried(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _second_account(connection)
        request_id = _request(connection)
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, ended_at, cause,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :now, :now, 'blocked', 'worker-a', 'worker-a',
                    'la-a', 'as-a')
        """), {"r": request_id, "now": NOW})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assert app.execute(text(
        "SELECT account_id FROM scrape_attempt WHERE id=:a"),
        {"a": attempt_id}).scalar_one() == "la-b"


def test_a_due_retry_waits_briefly_for_a_busy_untried_account(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _second_account(connection)
        request_id = _linkedin_request(
            connection, at=NOW - timedelta(minutes=10))
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, ended_at, cause,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :ended, :ended, 'browser_error',
                    'worker-a', 'worker-a', 'la-a', 'as-a')
        """), {"r": request_id, "ended": NOW - timedelta(minutes=3)})
        connection.execute(text("""
            UPDATE scrape_request
            SET next_attempt_at=:due, dispatch_wait_reason='retry_backoff'
            WHERE id=:r
        """), {"r": request_id, "due": NOW - timedelta(minutes=1)})

        blocker = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET status='running' WHERE id=:r"),
            {"r": blocker})
        blocking_attempt = connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, started_at,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :now, :now, 'worker-a', 'worker-b', 'la-b', 'as-b')
            RETURNING id
        """), {"r": blocker, "now": NOW}).scalar_one()

        assert dispatch_one(connection, now=NOW, rng=First()) is None

    waiting = app.execute(text("""
        SELECT status, dispatch_wait_reason
        FROM scrape_request WHERE id=:r
    """), {"r": request_id}).one()
    assert tuple(waiting) == ("queued", "waiting_for_untried_account")

    available_at = NOW + timedelta(seconds=5)
    with transaction(app_url) as connection:
        connection.execute(text("""
            UPDATE scrape_attempt SET ended_at=:now WHERE id=:attempt
        """), {"attempt": blocking_attempt, "now": available_at})
        connection.execute(text("""
            UPDATE scrape_request SET status='succeeded' WHERE id=:request
        """), {"request": blocker})
        _heartbeat(connection, available_at)
        retry_attempt = dispatch_one(
            connection, now=available_at, rng=First())

    assert retry_attempt is not None
    assert app.execute(text(
        "SELECT account_id FROM scrape_attempt WHERE id=:a"),
        {"a": retry_attempt}).scalar_one() == "la-b"


def test_a_retry_reuses_the_only_account_there_is(app_url, app):
    """Preference, not exclusion: a one-account fleet must still make progress
    rather than strand the request with attempts left unspent."""
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, ended_at, cause,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :now, :now, 'timeout', 'worker-a', 'worker-a',
                    'la-a', 'as-a')
        """), {"r": request_id, "now": NOW})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    assert attempt_id is not None
    assert app.execute(text(
        "SELECT account_id, attempt_no FROM scrape_attempt WHERE id=:a"),
        {"a": attempt_id}).one() == ("la-a", 2)


def test_a_claim_expiry_bypasses_the_request_cap(app_url, app):
    """Infrastructure loss is retried even when earlier scrapes spent the cap."""
    from career_lens.fleet.dispatch import sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET status='assigned' WHERE id=:r"),
            {"r": request_id})
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, ended_at, cause)
            VALUES (:r, 1, :now, :now, 'timeout'), (:r, 2, :now, :now, 'timeout')
        """), {"r": request_id, "now": NOW})
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, claim_deadline_at,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 3, :now, :deadline, 'worker-a', 'worker-a', 'la-a',
                    'as-a')
        """), {"r": request_id, "now": NOW,
               "deadline": NOW - timedelta(minutes=1)})

    sweep_expired_claims(database=app_url, now=NOW)

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert row.status == "queued"
    assert row.next_attempt_at == NOW + timedelta(minutes=1)


def test_a_claim_expiry_with_budget_left_is_queued_again(app_url, app):
    from career_lens.fleet.dispatch import sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET status='assigned' WHERE id=:r"),
            {"r": request_id})
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, claim_deadline_at,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :now, :deadline, 'worker-a', 'worker-a', 'la-a',
                    'as-a')
        """), {"r": request_id, "now": NOW,
               "deadline": NOW - timedelta(minutes=1)})

    sweep_expired_claims(database=app_url, now=NOW)

    row = app.execute(text("""
        SELECT status, dispatch_wait_reason, next_attempt_at
        FROM scrape_request WHERE id=:r
    """), {"r": request_id}).one()
    assert row.status == "queued"
    # The specific diagnostic survives the move to the shared policy: the
    # console already explains this one better than a generic backoff would.
    assert row.dispatch_wait_reason == "claim_expired"
    assert row.next_attempt_at == NOW + timedelta(seconds=60)
    assert app.execute(text("""
        SELECT cause FROM scrape_attempt WHERE request_id=:r AND attempt_no=1
    """), {"r": request_id}).scalar_one() == "worker_lost"


def _second_google_account(connection):
    """A second ready, Google-capable assignment on its own worker.

    `_fleet` builds one of each, which is enough to prove a request binds. It
    cannot prove anything about *choosing* between identities, and the retry
    policy is entirely about that choice.
    """
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at, capabilities)
        VALUES ('worker-b', 'worker-a', 'research-b', 'enabled', 'healthy', :now,
                '["google_search"]'::jsonb)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-b', 'Research B', 'b@example.com',
                'https://www.linkedin.com/in/bob/', 'enabled', 20, 0,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-b', 'la-b', 'worker-b', 'ready', :now, :now,
                'www.linkedin.com')
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker_google_policy
            (worker_id, max_searches_hour, max_searches_24h)
        VALUES ('worker-b', 10, 20)
    """))


def _heartbeat(connection, at):
    """Keep the fleet live at `at`.

    Dispatch only considers workers that beat within HEARTBEAT_SECONDS, so a
    test that advances the clock must advance the heartbeats with it or it
    measures liveness instead of the policy it meant to measure.
    """
    connection.execute(text(
        "UPDATE supervisor SET last_heartbeat_at=:at"), {"at": at})
    connection.execute(text(
        "UPDATE worker SET last_heartbeat_at=:at"), {"at": at})


def test_a_google_retry_moves_to_an_account_it_has_not_tried(app_url, app):
    """Identity rotation reaches the Google dispatch path.

    Google selection weights by search headroom rather than by attempt
    headroom, so it runs through its own chooser -- but the rule about not
    reusing a just-challenged identity is about the account, not about what
    the account was asked to do. A search blocked on one account is retried
    on the other.
    """
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _enable_google(connection)
        _second_google_account(connection)
        request_id = _google_request(connection)
        first = dispatch_one(connection, now=NOW, rng=First())

    first_account = app.execute(text(
        "SELECT account_id FROM scrape_attempt WHERE id=:a"),
        {"a": first}).scalar_one()

    with transaction(app_url) as connection:
        connection.execute(text("""
            UPDATE scrape_attempt SET ended_at=:now, cause='blocked'
            WHERE id=:a
        """), {"a": first, "now": NOW})
        connection.execute(text(
            "UPDATE scrape_request SET status='queued' WHERE id=:r"),
            {"r": request_id})
        retried_at = NOW + timedelta(minutes=1)
        _heartbeat(connection, retried_at)
        second = dispatch_one(connection, now=retried_at, rng=First())

    assert second is not None, "the retry was never dispatched"
    second_account = app.execute(text(
        "SELECT account_id FROM scrape_attempt WHERE id=:a"),
        {"a": second}).scalar_one()
    assert second_account != first_account, (
        f"the retry landed back on {second_account}, just blocked")


def test_a_google_request_in_backoff_waits_for_its_due_time(app_url, app):
    """The due-time gate covers every kind.

    Selecting the request and filtering it by due time are one statement, so
    a Google request serving a backoff is not merely rejected after being
    chosen -- it is never chosen, and the queue behind it does not starve.
    """
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _enable_google(connection)
        request_id = _google_request(connection)
        connection.execute(text(
            "UPDATE scrape_request SET next_attempt_at=:due WHERE id=:r"),
            {"r": request_id, "due": NOW + timedelta(minutes=5)})
        early = dispatch_one(connection, now=NOW, rng=First())

    assert early is None, "a Google request in backoff was dispatched early"

    with transaction(app_url) as connection:
        due_at = NOW + timedelta(minutes=6)
        _heartbeat(connection, due_at)
        later = dispatch_one(connection, now=due_at, rng=First())

    assert later is not None, "the Google request never became due"
    # Cleared on assignment, so the next failure schedules from a clean slate
    # rather than inheriting the delay that has already been served.
    assert app.execute(text(
        "SELECT next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() is None


def test_a_claim_expiry_on_a_cancelled_request_does_not_requeue_it(
        app_url, app):
    """The reason `cancel_requested` reaches the policy rather than living in
    the queue path: this request has budget left and a retryable cause, and it
    must still stop, because a human stopped it."""
    from career_lens.fleet.dispatch import sweep_expired_claims
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        connection.execute(text("""
            UPDATE scrape_request
            SET status='assigned', cancel_requested_at=:now WHERE id=:r
        """), {"r": request_id, "now": NOW})
        connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, claim_deadline_at,
                 supervisor_id, worker_id, account_id, assignment_id)
            VALUES (:r, 1, :now, :deadline, 'worker-a', 'worker-a', 'la-a',
                    'as-a')
        """), {"r": request_id, "now": NOW,
               "deadline": NOW - timedelta(minutes=1)})

    sweep_expired_claims(database=app_url, now=NOW)

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("cancelled", None)
    # The attempt still records what actually happened to it.
    assert app.execute(text(
        "SELECT cause FROM scrape_attempt WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == "worker_lost"


# --- Public page dispatch -----------------------------------------------
#
# A public page attempt needs a live Chrome worker and nothing else. These fix
# that boundary: no assignment, no account, and no LinkedIn readiness anywhere
# in the decision.


def _public_worker(connection, *, worker_id="worker-public",
                   supervisor_id="supervisor-public",
                   health="unknown", attention_code=None,
                   desired_state="enabled", supervisor_state="active",
                   supervisor_beat=NOW, worker_beat=NOW):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES (:supervisor, 'Public host', :state, :instance,
                'host-public', 'test', :now, :beat)
        ON CONFLICT (id) DO NOTHING
    """), {"supervisor": supervisor_id, "state": supervisor_state,
           "instance": f"instance-{supervisor_id}", "now": NOW,
           "beat": supervisor_beat})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             operator_attention_code, health_detail, attention_recorded_at,
             last_heartbeat_at)
        VALUES (:worker, :supervisor, :name, :desired, :health, :code,
                CASE WHEN CAST(:code AS text) IS NULL THEN NULL
                     ELSE 'LinkedIn state' END,
                CASE WHEN CAST(:code AS text) IS NULL THEN NULL
                     ELSE :now END,
                :beat)
    """), {"worker": worker_id, "supervisor": supervisor_id,
           "name": f"profile-{worker_id}", "desired": desired_state,
           "health": health, "code": attention_code, "beat": worker_beat,
           "now": NOW})
    return worker_id


def _wait_reason(connection, request_id: str) -> str | None:
    return connection.execute(text(
        "SELECT dispatch_wait_reason FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one()


def test_a_lost_worker_is_stamped_by_the_database_clock(app_url, app,
                                                        monkeypatch):
    """The control plane writes this; the supervisor writes what it is
    compared against. They are not always the same machine."""
    import career_lens.journal as journal_module
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.journal import Journal, sweep_stalled
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        _request(connection)
        attempt_id = dispatch_one(connection, now=NOW, rng=First())
        Journal.start(connection, attempt_id, url=app_url)

    class Stalled:
        id = 42
        task_kwargs = {"attempt_id": attempt_id}

    class Skewed:
        @staticmethod
        def now(tz=None):
            return datetime(2020, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(journal_module, "datetime", Skewed)
    sweep_stalled(app_url, stalled=lambda: [Stalled()], finish=lambda job: None)

    recorded, database_now = app.execute(text("""
        SELECT worker.attention_recorded_at, now()
        FROM worker JOIN scrape_attempt attempt ON attempt.worker_id=worker.id
        WHERE attempt.id=:attempt
    """), {"attempt": attempt_id}).one()
    assert recorded.year != 2020
    assert abs((recorded - database_now).total_seconds()) < 60


def test_a_native_worker_takes_the_search(
        app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _public_worker(connection)
        _enable_google(connection, "worker-public")
        request_id = _google_request(connection, top_n=0)
        dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT worker_id, queue_job_id FROM scrape_attempt
        WHERE request_id=:request
    """), {"request": request_id}).one()
    assert row.worker_id == "worker-public"
    assert row.queue_job_id is not None




def test_linkedin_request_without_account_stays_queued_with_reason(app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id = _request(connection)
        assert dispatch_one(connection, now=NOW, rng=First()) is None

    row = app.execute(text("""
        SELECT status, dispatch_wait_reason FROM scrape_request WHERE id=:request
    """), {"request": request_id}).one()
    assert row.status == "queued"
    assert row.dispatch_wait_reason == "no_assignments"


def test_linkedin_request_outside_account_shift_stays_queued_with_reason(
        app_url, app):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = _request(connection)
        # The only work block closed an hour ago.
        connection.execute(text("""
            UPDATE account_shift_block SET starts_at=:starts, ends_at=:ends
        """), {"starts": NOW - timedelta(hours=2),
               "ends": NOW - timedelta(hours=1)})
        assert dispatch_one(connection, now=NOW, rng=First()) is None

    row = app.execute(text("""
        SELECT status, dispatch_wait_reason FROM scrape_request WHERE id=:request
    """), {"request": request_id}).one()
    assert row.status == "queued"
    assert row.dispatch_wait_reason == "accounts_off_shift"


@pytest.mark.parametrize("kind,url", [
    ("linkedin_job", "https://www.linkedin.com/jobs/view/4012345678/"),
    ("linkedin_job_search",
     "https://www.linkedin.com/jobs/search/?keywords=go"),
])
def test_job_requests_run_on_a_signed_in_account(app_url, app, kind, url):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _fleet(connection)
        request_id = new_id(REQUEST)
        connection.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status, requested_at)
            VALUES (:id, :url, :url, :kind, 'queued', :at)
        """), {"id": request_id, "url": url, "kind": kind, "at": NOW})
        attempt_id = dispatch_one(connection, now=NOW, rng=First())

    row = app.execute(text("""
        SELECT request_id, account_id, assignment_id
        FROM scrape_attempt WHERE id=:attempt
    """), {"attempt": attempt_id}).one()
    assert row.request_id == request_id
    assert row.account_id is not None
    assert row.assignment_id is not None
