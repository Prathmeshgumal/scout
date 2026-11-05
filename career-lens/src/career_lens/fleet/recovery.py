"""Idempotent recovery for durable browser-worker operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from ..store.db import transaction
from . import retry
from .attention import (
    WORKER_OPERATION_ABANDONED, guidance,
)

ORPHAN_OPERATION_GRACE_SECONDS = 15 * 60
TERMINAL_JOB_STATES = ("failed", "cancelled", "aborted", "succeeded")


def sweep_orphaned_scrape_attempts(
        *, database: str | None = None,
        now: datetime | None = None) -> list[str]:
    """Settle native scrape attempts whose durable queue job already ended.

    An uncaught task exception is recorded by Procrastinate, but it cannot run
    the request finalizer that normally closes the attempt. Reconcile that
    split-brain state through the ordinary retry policy instead of leaving the
    public request permanently assigned or running.
    """
    checked_at = now or datetime.now(UTC)
    with transaction(database) as connection:
        rows = connection.execute(text("""
            SELECT attempt.id, attempt.request_id, attempt.attempt_no,
                   request.status AS request_status, request.max_attempts,
                   request.cancel_requested_at IS NOT NULL AS cancel_requested,
                   job.status AS job_status
            FROM scrape_attempt attempt
            JOIN scrape_request request ON request.id=attempt.request_id
            JOIN procrastinate.procrastinate_jobs job
              ON job.id=attempt.queue_job_id
            WHERE attempt.ended_at IS NULL
              AND request.status IN ('assigned', 'running', 'cancelled')
              AND job.status IN ('failed', 'cancelled', 'aborted', 'succeeded')
            ORDER BY attempt.id
            FOR UPDATE OF attempt, request SKIP LOCKED
        """)).all()
        closed: list[str] = []
        for row in rows:
            cancelled = row.cancel_requested or row.request_status == "cancelled"
            cause = "cancelled" if cancelled else "browser_error"
            detail = (
                "the capture queue job became terminal without closing its "
                f"attempt ({row.job_status})"
            )
            updated = connection.execute(text("""
                UPDATE scrape_attempt
                SET ended_at=:now, cause=:cause, cause_detail=:detail
                WHERE id=:attempt AND ended_at IS NULL
            """), {
                "attempt": row.id, "now": checked_at,
                "cause": cause, "detail": detail,
            }).rowcount
            if not updated:
                continue

            decision = retry.decide(
                cause=cause,
                attempt_no=retry.attempts_spent(connection, row.request_id),
                max_attempts=row.max_attempts,
                cancel_requested=cancelled,
            )
            if isinstance(decision, retry.Retry):
                retry.requeue(
                    connection, row.request_id, now=checked_at,
                    delay=decision.delay)
            elif row.request_status != "cancelled":
                retry.fail(
                    connection, row.request_id,
                    status=retry.terminal_status(cause, cancelled))
            closed.append(row.request_id)
        return closed


def sweep_restarted_native_attempts(
        *, database: str | None = None,
        now: datetime | None = None) -> list[str]:
    """Retry work that died with a native supervisor before it restarted.

    Procrastinate tracks a queue worker by its stable name.  When a supervisor
    returns quickly with that name, its new heartbeat can make an older running
    queue job look alive indefinitely.  The durable attempt carries the
    supervisor that owned it, and no native browser can survive a later
    supervisor start, so that boundary is authoritative here.

    Unlike an in-process browser crash, this does not put the replacement
    worker in attention-required: the replacement is exactly what can take the
    retry.
    """
    checked_at = now or datetime.now(UTC)
    with transaction(database) as connection:
        rows = connection.execute(text("""
            SELECT attempt.id, attempt.request_id, request.status,
                   request.max_attempts,
                   request.cancel_requested_at IS NOT NULL AS cancel_requested
            FROM scrape_attempt attempt
            JOIN scrape_request request ON request.id=attempt.request_id
            JOIN supervisor ON supervisor.id=attempt.supervisor_id
            WHERE attempt.ended_at IS NULL
              AND request.status IN ('assigned', 'running', 'cancelled')
              AND attempt.assigned_at < supervisor.started_at
            ORDER BY attempt.id
            FOR UPDATE OF attempt, request SKIP LOCKED
        """)).all()
        closed: list[str] = []
        for row in rows:
            updated = connection.execute(text("""
                UPDATE scrape_attempt
                SET ended_at=:now, cause='worker_lost',
                    cause_detail='the native supervisor restarted before this attempt closed'
                WHERE id=:attempt AND ended_at IS NULL
            """), {"attempt": row.id, "now": checked_at}).rowcount
            if not updated:
                continue
            decision = retry.decide(
                cause="worker_lost",
                attempt_no=retry.attempts_spent(connection, row.request_id),
                max_attempts=row.max_attempts,
                cancel_requested=row.cancel_requested,
            )
            if isinstance(decision, retry.Retry):
                retry.requeue(
                    connection, row.request_id, now=checked_at,
                    delay=decision.delay, reason="supervisor_restarted")
            elif row.status != "cancelled":
                retry.fail(
                    connection, row.request_id,
                    status=retry.terminal_status("worker_lost", row.cancel_requested))
            closed.append(row.request_id)
        return closed


def fail_worker_operation(connection, operation_id: str, *, code: str,
                        detail: str, now: datetime) -> bool:
    """Fail one still-open operation and block its attached resources."""
    guidance(code)
    clean_detail = " ".join(detail.split())[:1_000]
    row = connection.execute(text("""
        SELECT operation.id, operation.assignment_id,
               ass.account_id, ass.worker_id, ass.ended_at
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
        FOR UPDATE OF operation, ass, account, worker
    """), {"operation": operation_id}).one_or_none()
    if row is None:
        return False
    updated = connection.execute(text("""
        UPDATE worker_operation
        SET status='failed', started_at=COALESCE(started_at, :now),
            ended_at=:now, failure_detail=:detail
        WHERE id=:operation AND status IN ('queued', 'running')
    """), {
        "operation": operation_id, "now": now, "detail": clean_detail,
    }).rowcount
    if not updated:
        return False
    if row.ended_at is not None:
        return True

    connection.execute(text("""
        UPDATE account_worker_assignment
        SET state='attention_required', state_detail=:detail,
            operator_attention_code=:code
        WHERE id=:assignment AND ended_at IS NULL
    """), {
        "assignment": row.assignment_id, "detail": clean_detail, "code": code,
    })
    connection.execute(text("""
        UPDATE worker
        SET health='attention_required', health_detail=:detail,
            operator_attention_code=:code, attention_recorded_at=:now
        WHERE id=:worker
    """), {"worker": row.worker_id, "detail": clean_detail, "code": code,
           "now": now})
    connection.execute(text("""
        UPDATE linkedin_account
        SET state='attention_required', attention_detail=:detail,
            operator_attention_code=:code, updated_at=:now
        WHERE id=:account
    """), {
        "account": row.account_id, "detail": clean_detail,
        "code": code, "now": now,
    })
    connection.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES (:account, :worker, 'login_failed', :now,
                CAST(:event_detail AS jsonb))
    """), {
        "account": row.account_id, "worker": row.worker_id, "now": now,
        "event_detail": json.dumps({"reason": code}),
    })
    return True


def sweep_orphaned_worker_operations(*, database: str | None = None,
                                   now: datetime | None = None) -> list[str]:
    """Fail open operations whose queue job is terminal or truly absent."""
    checked_at = now or datetime.now(UTC)
    orphan_before = checked_at - timedelta(
        seconds=ORPHAN_OPERATION_GRACE_SECONDS)
    with transaction(database) as connection:
        rows = connection.execute(text("""
            SELECT operation.id, operation.queue_job_id, job.status AS job_status
            FROM worker_operation operation
            JOIN worker ON worker.id=operation.worker_id
            JOIN supervisor ON supervisor.id=worker.supervisor_id
            LEFT JOIN procrastinate.procrastinate_jobs job
              ON job.id=operation.queue_job_id
            WHERE operation.status IN ('queued', 'running')
              AND (
                job.status IN ('failed', 'cancelled', 'aborted', 'succeeded')
                OR (
                  (operation.queue_job_id IS NULL OR job.id IS NULL)
                  AND operation.requested_at < :orphan_before
                )
              )
            ORDER BY operation.requested_at, operation.id
            FOR UPDATE OF operation SKIP LOCKED
        """), {"orphan_before": orphan_before}).all()
        closed = []
        for row in rows:
            if row.job_status is None:
                detail = "the worker operation has no live durable queue job"
            else:
                detail = (
                    "the worker operation queue job became terminal without "
                    f"closing it ({row.job_status})")
            if fail_worker_operation(
                    connection, row.id, code=WORKER_OPERATION_ABANDONED,
                    detail=detail, now=checked_at):
                closed.append(row.id)
        return closed


