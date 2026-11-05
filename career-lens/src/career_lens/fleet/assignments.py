"""Guarded, operator-owned account-to-browser assignment transitions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

from ..store.ids import ASSIGNMENT, WORKER_OPERATION, new_id
from .attention import LINKEDIN_CLEAR_FAILED, guidance

DETAIL_LIMIT = 1_000
_TRANSITIONS = {
    "login_pending": {"logging_in"},
    "logging_in": {"ready", "account_mismatch", "attention_required"},
    "ready": {"ready", "account_mismatch", "attention_required"},
    "account_mismatch": {"ready", "account_mismatch", "attention_required"},
    "attention_required": {"ready", "account_mismatch", "attention_required"},
    "clearing": {"attention_required"},
}


class AssignmentError(RuntimeError):
    """An assignment command could not be applied safely."""


class AssignmentConflict(AssignmentError):
    """An account or worker is unavailable for a new assignment."""


class InvalidAssignmentTransition(AssignmentError):
    """The assignment no longer has the state expected by the caller."""


def _require_transaction(connection: Connection) -> None:
    isolation = connection.get_execution_options().get("isolation_level")
    if isolation == "AUTOCOMMIT" or not connection.in_transaction():
        raise AssignmentError(
            "assignment commands require a real database transaction")


def _detail(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AssignmentError("assignment detail must be text")
    cleaned = " ".join(value.split())
    if len(cleaned) > DETAIL_LIMIT:
        raise AssignmentError(
            f"assignment detail must not exceed {DETAIL_LIMIT} characters")
    return cleaned or None


def _insert_operation(connection: Connection, *, worker_id: str,
                      assignment_id: str, kind: str,
                      now: datetime, enqueue: bool = True) -> str:
    operation_id = new_id(WORKER_OPERATION)
    connection.execute(text("""
        INSERT INTO worker_operation
            (id, worker_id, assignment_id, kind, status, requested_at)
        VALUES (:id, :worker, :assignment, :kind, 'queued', :now)
    """), {
        "id": operation_id, "worker": worker_id,
        "assignment": assignment_id, "kind": kind, "now": now,
    })
    if not enqueue:
        return operation_id

    from ..queue.enqueue import enqueue_worker_operation
    queue_job_id = enqueue_worker_operation(
        connection, operation_id=operation_id, worker_id=worker_id)
    connection.execute(text("""
        UPDATE worker_operation SET queue_job_id=:queue_job
        WHERE id=:operation
    """), {"operation": operation_id, "queue_job": queue_job_id})
    return operation_id


def _open_operation(connection: Connection, assignment_id: str,
                    kind: str) -> str | None:
    return connection.execute(text("""
        SELECT id FROM worker_operation
        WHERE assignment_id=:assignment AND kind=:kind
          AND status IN ('queued', 'running')
        ORDER BY requested_at, id LIMIT 1
    """), {"assignment": assignment_id, "kind": kind}).scalar_one_or_none()


def _open_worker_operation(connection: Connection, worker_id: str):
    return connection.execute(text("""
        SELECT id, assignment_id, kind FROM worker_operation
        WHERE worker_id=:worker AND status IN ('queued', 'running')
        ORDER BY requested_at, id LIMIT 1
    """), {"worker": worker_id}).one_or_none()


def assign_account(connection: Connection, *, account_id: str, worker_id: str,
                   now: datetime | None = None) -> tuple[str, str]:
    """Attach an enabled account to an enabled worker and queue its login."""
    _require_transaction(connection)
    assigned_at = now or datetime.now(UTC)

    account = connection.execute(text("""
        SELECT a.state, c.account_id IS NOT NULL AS configured
        FROM linkedin_account a
        LEFT JOIN linkedin_account_credential c ON c.account_id = a.id
        WHERE a.id = :id
        FOR UPDATE OF a
    """), {"id": account_id}).one_or_none()
    if account is None:
        raise AssignmentConflict(f"no LinkedIn account {account_id}")
    if account.state != "enabled":
        raise AssignmentConflict(f"LinkedIn account {account_id} is not enabled")
    if not account.configured:
        raise AssignmentConflict(
            f"LinkedIn account {account_id} has no configured credential")

    worker = connection.execute(text("""
        SELECT s.desired_state, w.desired_state AS worker_state
        FROM worker s
        JOIN supervisor w ON w.id = s.supervisor_id
        WHERE s.id = :id
        FOR UPDATE OF s, w
    """), {"id": worker_id}).one_or_none()
    if worker is None:
        raise AssignmentConflict(f"no browser worker {worker_id}")
    if worker.desired_state != "enabled" or worker.worker_state != "active":
        raise AssignmentConflict(f"browser worker {worker_id} is not enabled")

    occupied = connection.execute(text("""
        SELECT id, account_id, worker_id, state
        FROM account_worker_assignment
        WHERE ended_at IS NULL
          AND (account_id = :account OR worker_id = :worker)
    """), {"account": account_id, "worker": worker_id}).all()
    same = next((row for row in occupied
                 if row.account_id == account_id and row.worker_id == worker_id),
                None)
    if same is not None and same.state in {"login_pending", "logging_in"}:
        operation_id = _open_operation(connection, same.id, "login")
        if operation_id:
            return same.id, operation_id
    if any(row.account_id == account_id for row in occupied):
        raise AssignmentConflict(
            f"LinkedIn account {account_id} is already assigned")
    if occupied:
        raise AssignmentConflict(f"browser worker {worker_id} is already assigned")

    assignment_id = new_id(ASSIGNMENT)
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at)
        VALUES (:id, :account, :worker, 'login_pending', :now)
    """), {
        "id": assignment_id, "account": account_id, "worker": worker_id,
        "now": assigned_at,
    })
    operation_id = _insert_operation(
        connection, worker_id=worker_id, assignment_id=assignment_id,
        kind="login", now=assigned_at)
    return assignment_id, operation_id


def transition_assignment(connection: Connection, assignment_id: str, *,
                          expected: str, target: str,
                          detail: str | None = None,
                          operator_attention_code: str | None = None,
                          verified_host: str | None = None,
                          now: datetime | None = None) -> None:
    """Compare-and-set one browser-observed assignment transition."""
    _require_transaction(connection)
    if target not in _TRANSITIONS.get(expected, set()):
        raise InvalidAssignmentTransition(
            f"transition {expected} to {target} is not allowed")
    blocked = target in {"attention_required", "account_mismatch"}
    if blocked != (operator_attention_code is not None):
        raise InvalidAssignmentTransition(
            f"transition to {target} has invalid operator attention code")
    if operator_attention_code is not None:
        guidance(operator_attention_code)
    changed_at = now or datetime.now(UTC)
    values = {
        "id": assignment_id, "expected": expected, "target": target,
        "detail": _detail(detail), "verified_host": verified_host,
        "attention_code": operator_attention_code, "now": changed_at,
    }
    row = connection.execute(text("""
        UPDATE account_worker_assignment
        SET state = :target, state_detail = :detail,
            operator_attention_code = :attention_code,
            verified_at = CASE WHEN :target = 'ready' THEN :now
                               ELSE verified_at END,
            verified_host = CASE WHEN :target = 'ready'
                                 THEN COALESCE(:verified_host, verified_host)
                                 ELSE verified_host END
        WHERE id = :id AND state = :expected AND ended_at IS NULL
        RETURNING id
    """), values).one_or_none()
    if row is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} is not open in expected state "
            f"{expected}")

    if target == "logging_in":
        connection.execute(text("""
            UPDATE worker_operation
            SET status = 'running', started_at = :now
            WHERE assignment_id = :id AND status = 'queued'
              AND kind IN ('login', 'replace')
        """), {"id": assignment_id, "now": changed_at})
    elif expected == "logging_in":
        status = "succeeded" if target == "ready" else "failed"
        connection.execute(text("""
            UPDATE worker_operation
            SET status = :status, ended_at = :now,
                failure_detail = CASE WHEN :status = 'failed'
                                      THEN :detail ELSE NULL END
            WHERE assignment_id = :id AND status = 'running'
              AND kind IN ('login', 'replace')
        """), {
            "id": assignment_id, "status": status, "now": changed_at,
            "detail": values["detail"],
        })


def request_clear(connection: Connection, assignment_id: str, *,
                  now: datetime | None = None) -> str:
    """Queue sign-out and retain ownership until it is verified complete."""
    _require_transaction(connection)
    requested_at = now or datetime.now(UTC)
    existing = _open_operation(connection, assignment_id, "clear")
    if existing:
        return existing
    row = connection.execute(text("""
        UPDATE account_worker_assignment
        SET state = 'clearing', clearing_at = :now, state_detail = NULL,
            operator_attention_code = NULL
        WHERE id = :id AND ended_at IS NULL
          AND state IN ('ready', 'attention_required', 'account_mismatch')
        RETURNING worker_id
    """), {"id": assignment_id, "now": requested_at}).one_or_none()
    if row is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} cannot enter clearing")
    return _insert_operation(
        connection, worker_id=row.worker_id, assignment_id=assignment_id,
        kind="clear", now=requested_at)


def confirm_replace(connection: Connection, assignment_id: str, *,
                    now: datetime | None = None) -> str:
    """Explicitly authorize replacing a mismatched browser session."""
    _require_transaction(connection)
    requested_at = now or datetime.now(UTC)
    existing = _open_operation(connection, assignment_id, "replace")
    if existing:
        return existing
    row = connection.execute(text("""
        UPDATE account_worker_assignment
        SET state = 'login_pending', state_detail = NULL,
            operator_attention_code = NULL
        WHERE id = :id AND state = 'account_mismatch' AND ended_at IS NULL
        RETURNING worker_id
    """), {"id": assignment_id}).one_or_none()
    if row is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} is not an account mismatch")
    return _insert_operation(
        connection, worker_id=row.worker_id, assignment_id=assignment_id,
        kind="replace", now=requested_at)


def request_verify(connection: Connection, assignment_id: str, *,
                   now: datetime | None = None) -> str:
    """Ask the owning worker to re-observe an open assignment."""
    _require_transaction(connection)
    requested_at = now or datetime.now(UTC)
    row = connection.execute(text("""
        SELECT assignment.worker_id
        FROM account_worker_assignment assignment
        WHERE assignment.id=:id AND assignment.ended_at IS NULL
          AND state IN ('ready', 'attention_required', 'account_mismatch')
        FOR UPDATE OF assignment
    """), {"id": assignment_id}).one_or_none()
    if row is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} cannot be verified")
    # The native runner still owns this browser. Signal it rather than queueing
    # another browser launch against the same persistent profile.
    waiting = connection.execute(text("""
        UPDATE worker_operation SET manual_verify_requested=true
        WHERE assignment_id=:assignment AND status='running'
          AND manual_intervention_at IS NOT NULL
        RETURNING id
    """), {"assignment": assignment_id}).scalar_one_or_none()
    if waiting:
        return waiting
    existing = _open_operation(connection, assignment_id, "verify")
    if existing:
        return existing
    return _insert_operation(
        connection, worker_id=row.worker_id, assignment_id=assignment_id,
        kind="verify", now=requested_at)


def request_login_retry(connection: Connection, assignment_id: str, *,
                        now: datetime | None = None) -> str:
    """Retry stored credentials for an assignment that needs an operator."""
    _require_transaction(connection)
    requested_at = now or datetime.now(UTC)
    existing = _open_operation(connection, assignment_id, "login")
    if existing:
        return existing

    assignment = connection.execute(text("""
        SELECT worker_id, state FROM account_worker_assignment
        WHERE id=:id AND ended_at IS NULL
        FOR UPDATE
    """), {"id": assignment_id}).one_or_none()
    if assignment is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} cannot retry login")

    # A concurrent retry may have completed while this transaction waited for
    # the assignment lock. Return its operation instead of reporting a stale
    # transition to the duplicate caller.
    existing = _open_operation(connection, assignment_id, "login")
    if existing:
        return existing
    if assignment.state != "attention_required":
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} cannot retry login")

    blocking = _open_worker_operation(connection, assignment.worker_id)
    if blocking is not None:
        raise InvalidAssignmentTransition(
            f"browser worker {assignment.worker_id} has an open "
            f"{blocking.kind} operation")

    row = connection.execute(text("""
        UPDATE account_worker_assignment
        SET state='login_pending', state_detail=NULL,
            operator_attention_code=NULL
        WHERE id=:id AND state='attention_required' AND ended_at IS NULL
        RETURNING worker_id
    """), {"id": assignment_id}).one_or_none()
    if row is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} cannot retry login")
    # This command is operator-initiated, not the recovery scheduler below.
    connection.execute(text("""
        UPDATE linkedin_account SET recovery_phase=NULL, recovery_not_before=NULL
        WHERE id=(SELECT account_id FROM account_worker_assignment WHERE id=:id)
          AND recovery_phase='before_relogin'
    """), {"id": assignment_id})
    return _insert_operation(
        connection, worker_id=row.worker_id, assignment_id=assignment_id,
        kind="login", now=requested_at)


def queue_due_relogin(connection: Connection, *, now: datetime) -> str | None:
    """Queue one due automatic LinkedIn login without reserving a request."""
    _require_transaction(connection)
    row = connection.execute(text("""
        SELECT assignment.id, assignment.worker_id
        FROM account_worker_assignment assignment
        JOIN linkedin_account account ON account.id=assignment.account_id
        JOIN account_schedule schedule
          ON schedule.account_id=account.id AND schedule.enabled
        JOIN account_shift_block shift
          ON shift.account_id=account.id AND shift.kind='work'
         AND :now >= shift.starts_at AND :now < shift.ends_at
        JOIN worker ON worker.id=assignment.worker_id
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        WHERE assignment.state='attention_required'
          AND assignment.operator_attention_code='linkedin_signed_out'
          AND assignment.ended_at IS NULL
          AND account.state='enabled'
          AND account.recovery_phase='before_relogin'
          AND account.recovery_not_before <= :now
          AND (account.cooldown_until IS NULL OR account.cooldown_until <= :now)
          AND worker.desired_state='enabled'
          AND worker.health IN ('healthy', 'unknown')
          AND worker.last_heartbeat_at >= :heartbeat_after
          AND supervisor.desired_state='active'
          AND supervisor.last_heartbeat_at >= :heartbeat_after
          AND NOT EXISTS (
              SELECT 1 FROM scrape_attempt attempt
              WHERE attempt.worker_id=worker.id AND attempt.ended_at IS NULL)
          AND NOT EXISTS (
              SELECT 1 FROM worker_operation operation
              WHERE operation.worker_id=worker.id
                AND operation.status IN ('queued', 'running'))
        ORDER BY account.recovery_not_before, assignment.assigned_at
        LIMIT 1
        FOR UPDATE OF assignment, account, worker SKIP LOCKED
    """), {
        "now": now,
        "heartbeat_after": now - timedelta(seconds=30),
    }).one_or_none()
    if row is None:
        return None
    changed = connection.execute(text("""
        UPDATE account_worker_assignment
        SET state='login_pending', state_detail=NULL,
            operator_attention_code=NULL
        WHERE id=:assignment AND state='attention_required'
          AND operator_attention_code='linkedin_signed_out'
          AND ended_at IS NULL
        RETURNING id
    """), {"assignment": row.id}).one_or_none()
    if changed is None:
        return None
    return _insert_operation(
        connection,
        worker_id=row.worker_id,
        assignment_id=row.id,
        kind="login",
        now=now,
    )


def release_due_relogin_cooldowns(
        connection: Connection, *, now: datetime) -> int:
    """Return accounts whose post-login rest has elapsed to eligibility."""
    result = connection.execute(text("""
        UPDATE linkedin_account
        SET recovery_phase=NULL, recovery_not_before=NULL, updated_at=:now
        WHERE recovery_phase='after_relogin' AND recovery_not_before <= :now
    """), {"now": now})
    return result.rowcount


def finish_clear(connection: Connection, assignment_id: str,
                 operation_id: str, *, success: bool,
                 detail: str | None, now: datetime | None = None) -> None:
    """Record clearing evidence; only success releases account and worker."""
    _require_transaction(connection)
    ended_at = now or datetime.now(UTC)
    clean_detail = _detail(detail)
    operation = connection.execute(text("""
        UPDATE worker_operation
        SET status = :status, started_at = COALESCE(started_at, :now),
            ended_at = :now, failure_detail = :detail
        WHERE id = :operation AND assignment_id = :assignment
          AND kind = 'clear' AND status IN ('queued', 'running')
        RETURNING id
    """), {
        "operation": operation_id, "assignment": assignment_id,
        "status": "succeeded" if success else "failed", "now": ended_at,
        "detail": None if success else clean_detail,
    }).one_or_none()
    if operation is None:
        raise InvalidAssignmentTransition(
            f"clear operation {operation_id} is not open for assignment "
            f"{assignment_id}")

    if success:
        result = connection.execute(text("""
            UPDATE account_worker_assignment
            SET state = 'ended', state_detail = NULL, ended_at = :now,
                operator_attention_code = NULL
            WHERE id = :id AND state = 'clearing' AND ended_at IS NULL
            RETURNING id
        """), {"id": assignment_id, "now": ended_at}).one_or_none()
    else:
        result = connection.execute(text("""
            UPDATE account_worker_assignment
            SET state = 'attention_required', state_detail = :detail,
                operator_attention_code = :attention_code
            WHERE id = :id AND state = 'clearing' AND ended_at IS NULL
            RETURNING id
        """), {
            "id": assignment_id, "detail": clean_detail,
            "attention_code": LINKEDIN_CLEAR_FAILED,
        }).one_or_none()
    if result is None:
        raise InvalidAssignmentTransition(
            f"assignment {assignment_id} is not being cleared")
    if success:
        connection.execute(text("""
            UPDATE linkedin_account account
            SET recovery_phase=NULL, recovery_not_before=NULL,
                recovery_failures=0, updated_at=:now
            FROM account_worker_assignment assignment
            WHERE assignment.id=:assignment
              AND account.id=assignment.account_id
        """), {"assignment": assignment_id, "now": ended_at})
