"""Execute observed LinkedIn session commands on their targeted worker."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from patchright.sync_api import TimeoutError as BrowserTimeoutError

from ..capture.browser import session as browser_session
from ..capture.urls import canonical
from ..linkedin import session as linkedin_session
from ..store.db import transaction
from .account_recovery import RecoveryState, configured_policy, login_finished
from .accounts import password_for_login
from .attention import (
    LINKEDIN_ACCOUNT_MISMATCH, LINKEDIN_CHALLENGE, LINKEDIN_CLEAR_FAILED,
    LINKEDIN_IDENTITY_UNOBSERVABLE, LINKEDIN_SIGNED_OUT,
    WORKER_OPERATION_FAILED,
)
from .assignments import InvalidAssignmentTransition, transition_assignment
from .credentials import CredentialKeyring
from .registration import (
    clear_completed_operation_attention, mark_worker_health,
)

DETAIL_LIMIT = 1_000


def _observe_for_operator(page, observer):
    try:
        return observer(page)
    except BrowserTimeoutError:
        return linkedin_session.SessionObservation(
            "unknown", detail="LinkedIn identity verification timed out")


def _manual_verification(page, row, observation, *, database, observer):
    from .manual_login import wait_for_verification

    def record_attention(connection, row, observation):
        _attention(connection, row, observation.detail or "Manual verification required",
                   datetime.now(UTC), operator_attention_code=(
                       LINKEDIN_CHALLENGE if observation.state == "challenge"
                       else LINKEDIN_IDENTITY_UNOBSERVABLE))

    return wait_for_verification(
        page, row, observation, database=database, provider="linkedin",
        observer=lambda page: _observe_for_operator(page, observer),
        record_attention=record_attention, transition_error=InvalidAssignmentTransition,
        matches=lambda value: value.state == "signed_in" and value.profile_url
        and (row.profile_url is None or _same(row.profile_url, value.profile_url)))


class SlotOperationError(RuntimeError):
    """A durable worker operation is missing or internally inconsistent."""


def _same(first: str | None, second: str | None) -> bool:
    return linkedin_session.same_profile(first, second)


def _event(connection, row, kind: str, now: datetime,
           detail: dict | None = None) -> None:
    import json
    connection.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES (:account, :worker, :kind, :now, CAST(:detail AS jsonb))
    """), {
        "account": row.account_id, "worker": row.worker_id, "kind": kind,
        "now": now, "detail": json.dumps(detail or {}),
    })


def _complete(connection, row, *, target: str, success: bool,
              detail: str | None, now: datetime,
              operator_attention_code: str | None = None,
              verified_host: str | None = None) -> None:
    detail = (" ".join(detail.split())[:DETAIL_LIMIT] if detail else None)
    expected = ("logging_in" if row.kind in {"login", "replace"}
                else row.assignment_state)
    transition_assignment(
        connection, row.assignment_id, expected=expected, target=target,
        detail=detail, operator_attention_code=operator_attention_code,
        verified_host=verified_host, now=now)
    connection.execute(text("""
        UPDATE worker_operation
        SET status=:status, ended_at=:now, failure_detail=:detail
        WHERE id=:operation AND status='running'
    """), {
        "operation": row.operation_id,
        "status": "succeeded" if success else "failed",
        "now": now, "detail": None if success else detail,
    })
    if target == "attention_required":
        connection.execute(text("""
            UPDATE linkedin_account
            SET state='attention_required', attention_detail=:detail,
                operator_attention_code=:attention_code,
                updated_at=:now WHERE id=:account
        """), {
            "account": row.account_id, "detail": detail,
            "attention_code": operator_attention_code, "now": now,
        })
    elif target == "ready":
        connection.execute(text("""
            UPDATE linkedin_account
            SET state=CASE WHEN state='attention_required'
                           THEN 'enabled' ELSE state END,
                attention_detail=CASE WHEN state='attention_required'
                                      THEN NULL ELSE attention_detail END,
                operator_attention_code=CASE WHEN state='attention_required'
                                             THEN NULL
                                             ELSE operator_attention_code END,
                recovery_phase=NULL, recovery_not_before=NULL,
                recovery_failures=0,
                updated_at=:now
            WHERE id=:account
        """), {"account": row.account_id, "now": now})
        clear_completed_operation_attention(connection, row.worker_id)
        from ..queue.enqueue import enqueue_dispatch

        enqueue_dispatch(connection)


def _attention(connection, row, detail: str, now: datetime, *,
               operator_attention_code: str,
               event: str = "login_failed",
               event_detail: dict | None = None) -> None:
    _complete(connection, row, target="attention_required", success=False,
              detail=detail, now=now,
              operator_attention_code=operator_attention_code)
    connection.execute(text("""
        UPDATE linkedin_account
        SET recovery_phase=NULL, recovery_not_before=NULL, updated_at=:now
        WHERE id=:account
    """), {"account": row.account_id, "now": now})
    if not getattr(row, "manual_intervention_recorded", False):
        _event(connection, row, event, now, {
            "reason": detail[:DETAIL_LIMIT], **(event_detail or {}),
        })


def _finish_automatic_relogin(
        connection, row, observation, *, now: datetime) -> bool:
    """Settle one coordinator-scheduled login; return whether it handled it."""
    if row.recovery_phase != "before_relogin" or row.kind != "login":
        return False
    landed = (
        observation.state == "signed_in"
        and bool(observation.profile_url)
        and (row.profile_url is None
             or _same(row.profile_url, observation.profile_url))
    )
    if not landed and observation.state != "signed_out":
        return False
    transition = login_finished(
        RecoveryState(
            phase=row.recovery_phase,
            not_before=row.recovery_not_before,
            failures=row.recovery_failures,
        ),
        landed=landed,
        now=now,
        policy=configured_policy(
            row.recovery_login_retry_seconds,
            row.recovery_post_login_seconds,
        ),
    )
    if landed:
        if row.profile_url is None:
            connection.execute(text("""
                UPDATE linkedin_account SET profile_url=:url, updated_at=:now
                WHERE id=:account AND profile_url IS NULL
            """), {
                "account": row.account_id,
                "url": canonical(observation.profile_url),
                "now": now,
            })
        _complete(
            connection,
            row,
            target="ready",
            success=True,
            detail=None,
            now=now,
            verified_host=linkedin_session.verified_host(observation),
        )
        connection.execute(text("""
            UPDATE linkedin_account
            SET recovery_phase=:phase, recovery_not_before=:not_before,
                recovery_failures=:failures, updated_at=:now
            WHERE id=:account
        """), {
            "account": row.account_id,
            "phase": transition.state.phase,
            "not_before": transition.state.not_before,
            "failures": transition.state.failures,
            "now": now,
        })
        _event(connection, row, "login_succeeded", now, {"self_heal": True})
        return True
    detail = observation.detail or "LinkedIn login did not restore the session"
    if transition.action == "attention":
        _attention(
            connection,
            row,
            detail,
            now,
            operator_attention_code=LINKEDIN_SIGNED_OUT,
            event_detail={
                "self_heal": True,
                "terminal": True,
                "reason_code": LINKEDIN_SIGNED_OUT,
            },
        )
        connection.execute(text("""
            UPDATE linkedin_account
            SET recovery_phase=NULL, recovery_not_before=NULL,
                recovery_failures=:failures, updated_at=:now
            WHERE id=:account
        """), {
            "account": row.account_id,
            "failures": transition.state.failures,
            "now": now,
        })
        return True
    transition_assignment(
        connection,
        row.assignment_id,
        expected="logging_in",
        target="attention_required",
        detail=detail,
        operator_attention_code=LINKEDIN_SIGNED_OUT,
        now=now,
    )
    connection.execute(text("""
        UPDATE worker_operation
        SET status='failed', ended_at=:now, failure_detail=:detail
        WHERE id=:operation AND status='running'
    """), {
        "operation": row.operation_id,
        "now": now,
        "detail": detail[:DETAIL_LIMIT],
    })
    connection.execute(text("""
        UPDATE linkedin_account
        SET state='enabled', attention_detail=NULL,
            operator_attention_code=NULL,
            recovery_phase=:phase, recovery_not_before=:not_before,
            recovery_failures=:failures, updated_at=:now
        WHERE id=:account
    """), {
        "account": row.account_id,
        "phase": transition.state.phase,
        "not_before": transition.state.not_before,
        "failures": transition.state.failures,
        "now": now,
    })
    _event(connection, row, "login_failed", now, {
        "self_heal": True,
        "reason": detail[:DETAIL_LIMIT],
    })
    from ..queue.enqueue import enqueue_dispatch

    enqueue_dispatch(connection)
    return True


def _close_changed_operation(connection, operation_id: str,
                             now: datetime) -> None:
    """Make a stale operation terminal without changing newer assignment state."""
    connection.execute(text("""
        UPDATE worker_operation
        SET status='failed', ended_at=:now,
            failure_detail='assignment changed during worker operation'
        WHERE id=:operation AND status='running'
    """), {"operation": operation_id, "now": now})


def _login_result(connection, row, observation, *, logged_in: bool,
                  now: datetime) -> None:
    if _finish_automatic_relogin(connection, row, observation, now=now):
        return
    if observation.state == "challenge":
        _attention(connection, row, observation.detail or "LinkedIn challenge",
                   now, operator_attention_code=LINKEDIN_CHALLENGE,
                   event="checkpoint")
        return
    if observation.state != "signed_in" or not observation.profile_url:
        attention_code = (LINKEDIN_SIGNED_OUT
                          if observation.state == "signed_out"
                          else LINKEDIN_IDENTITY_UNOBSERVABLE)
        _attention(connection, row,
                   observation.detail or "LinkedIn identity was not observable",
                   now, operator_attention_code=attention_code)
        return
    if row.profile_url is None and not logged_in:
        _complete(connection, row, target="account_mismatch", success=False,
                  detail="existing signed-in account is not yet identified",
                  now=now,
                  operator_attention_code=LINKEDIN_ACCOUNT_MISMATCH)
        return
    if row.profile_url is not None and not _same(
            row.profile_url, observation.profile_url):
        _complete(connection, row, target="account_mismatch", success=False,
                  detail="browser is signed in as a different LinkedIn account",
                  now=now,
                  operator_attention_code=LINKEDIN_ACCOUNT_MISMATCH)
        return
    if row.profile_url is None:
        connection.execute(text("""
            UPDATE linkedin_account SET profile_url=:url, updated_at=:now
            WHERE id=:account AND profile_url IS NULL
        """), {
            "account": row.account_id,
            "url": canonical(observation.profile_url), "now": now,
        })
    _complete(
        connection, row, target="ready", success=True, detail=None, now=now,
        verified_host=linkedin_session.verified_host(observation))
    _event(connection, row, "login_succeeded", now)


def _finish_clear_result(database, row, observation) -> None:
    """Record a clear from the observation that verified it, and only that."""
    from .assignments import finish_clear

    with transaction(database) as connection:
        success = observation.state == "signed_out"
        detail = (None if success else
                  observation.detail or "session remained signed in")
        finish_clear(
            connection, row.assignment_id, row.operation_id,
            success=success, detail=detail)
        if success:
            clear_completed_operation_attention(connection, row.worker_id)
            from ..queue.enqueue import enqueue_dispatch

            enqueue_dispatch(connection)
            return

        now = datetime.now(UTC)
        connection.execute(text("""
            UPDATE linkedin_account
            SET state='attention_required', attention_detail=:detail,
                operator_attention_code=:attention_code, updated_at=:now
            WHERE id=:account
        """), {
            "account": row.account_id, "detail": detail,
            "attention_code": LINKEDIN_CLEAR_FAILED, "now": now,
        })


def run_worker_operation(
        operation_id: str, *, database: str | None = None,
        session_factory=browser_session,
        observer=linkedin_session.observe_session,
        login_action=linkedin_session.login,
        clear_action=linkedin_session.clear_session,
        reset_action=linkedin_session.clear_browser_state,
        keyring: CredentialKeyring | None = None,
) -> None:
    """Run once; a duplicate delivery sees a terminal operation and returns."""
    with transaction(database) as connection:
        row = connection.execute(text("""
            SELECT operation.id AS operation_id, operation.kind,
                   operation.status, operation.assignment_id,
                   ass.account_id, ass.worker_id, ass.state AS assignment_state,
                   account.username, account.profile_url,
                   account.recovery_phase, account.recovery_not_before,
                   account.recovery_failures,
                   account.recovery_login_retry_seconds,
                   account.recovery_post_login_seconds, worker.local_name
            FROM worker_operation operation
            JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
            JOIN linkedin_account account ON account.id=ass.account_id
            JOIN worker ON worker.id=ass.worker_id
            WHERE operation.id=:operation
            FOR UPDATE OF operation, ass, account, worker
        """), {"operation": operation_id}).one_or_none()
        if row is None:
            raise SlotOperationError(f"no worker operation {operation_id}")
        if row.status in {"succeeded", "failed"}:
            return
        if row.status != "queued":
            return
        now = datetime.now(UTC)
        if row.kind in {"login", "replace"}:
            expected = "login_pending"
            if row.assignment_state != expected:
                raise SlotOperationError(
                    f"assignment {row.assignment_id} is not {expected}")
            transition_assignment(
                connection, row.assignment_id, expected="login_pending",
                target="logging_in", now=now)
        else:
            connection.execute(text("""
                UPDATE worker_operation SET status='running', started_at=:now
                WHERE id=:operation AND status='queued'
            """), {"operation": operation_id, "now": now})

    automatic_login = row.kind == "login" and row.recovery_phase == "before_relogin"
    try:
        if row.kind == "clear":
            with session_factory(profile=row.local_name) as page:
                initial = observer(page)
                if initial.state == "signed_out":
                    reset_action(page)
                else:
                    clear_action(page)
            # Only a browser started after the sign-out can settle this. The
            # context that just cleared its own state would agree with itself,
            # while LinkedIn's `li_rm` restores the session from disk on the
            # next visit -- which is exactly the case being tested for.
            with session_factory(profile=row.local_name) as page:
                verified = observer(page)
                if verified.state == "signed_out":
                    # Re-observing signed the browser back into LinkedIn's
                    # device cookies. Drop them too, or the worker's next
                    # occupant is linkable to this one.
                    reset_action(page)
            _finish_clear_result(database, row, verified)
            return

        with session_factory(profile=row.local_name) as page:
            observation = (_observe_for_operator(page, observer)
                           if not automatic_login else observer(page))
            logged_in = False
            if row.kind == "replace":
                observation = clear_action(page)
                if observation.state != "signed_out":
                    with transaction(database) as connection:
                        _attention(connection, row,
                                   observation.detail or "could not clear session",
                                   datetime.now(UTC),
                                   operator_attention_code=LINKEDIN_CLEAR_FAILED)
                    return
            if observation.state == "signed_out" and row.kind != "verify":
                with transaction(database) as connection:
                    password = password_for_login(
                        connection, row.account_id, keyring=keyring)
                try:
                    observation = login_action(
                        page, username=row.username, password=password)
                except BrowserTimeoutError:
                    if automatic_login:
                        raise
                    observation = linkedin_session.SessionObservation(
                        "unknown", detail="LinkedIn login timed out; inspect the window")
                finally:
                    password = None
                logged_in = True
            if (not automatic_login and (
                    observation.state != "signed_in" or not observation.profile_url
                    or (row.profile_url is not None
                        and not _same(row.profile_url, observation.profile_url)))):
                row, observation = _manual_verification(
                    page, row, observation, database=database, observer=observer)
                # Explicit Verify can enroll the identity for a first assignment.
                logged_in = observation.state == "signed_in"
            with transaction(database) as connection:
                _login_result(connection, row, observation,
                              logged_in=logged_in, now=datetime.now(UTC))
    except InvalidAssignmentTransition:
        # A concurrent operator command won the compare-and-set. Preserve its
        # state and close only this now-stale operation.
        with transaction(database) as connection:
            _close_changed_operation(
                connection, row.operation_id, datetime.now(UTC))
    except Exception as exc:
        # Exception text can contain browser input values. Record only its
        # class; credentials must remain write-only even on failure.
        try:
            with transaction(database) as connection:
                _attention(connection, row,
                           f"worker operation failed ({type(exc).__name__})",
                           datetime.now(UTC),
                           operator_attention_code=WORKER_OPERATION_FAILED)
                # `_complete` no longer reaches the worker. An exception
                # thrown while driving Chrome is a claim about this machine,
                # so it is recorded here explicitly. No `now`: the default is
                # the database clock, which is what recovery compares against.
                mark_worker_health(
                    connection, row.worker_id, "attention_required",
                    f"worker operation failed ({type(exc).__name__})",
                    operator_attention_code=WORKER_OPERATION_FAILED)
        except InvalidAssignmentTransition:
            # Failure recording races the same operator transitions as normal
            # completion. Never let that secondary CAS failure strand the
            # operation in `running`.
            with transaction(database) as connection:
                _close_changed_operation(
                    connection, row.operation_id, datetime.now(UTC))
