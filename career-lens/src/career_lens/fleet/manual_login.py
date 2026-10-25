"""Shared ownership of native browser windows awaiting operator verification."""

from types import SimpleNamespace

from patchright.sync_api import Error as BrowserError
from sqlalchemy import text

from ..store.db import transaction


def wait_for_verification(page, row, observation, *, database, observer,
                          provider, record_attention, matches, transition_error):
    """Keep the owning operation/browser alive; only an operator may verify.

    A running worker operation already excludes this worker from dispatch for
    every provider. No second browser, worker quarantine, or background login
    retry is needed while the operator uses the original window.
    """
    account_table, assignment_table, operation_table = {
        "linkedin": ("linkedin_account", "account_worker_assignment", "worker_operation"),
    }[provider]
    with transaction(database) as connection:
        account_state = connection.execute(text(f"""
            SELECT state FROM {account_table} WHERE id=:account FOR UPDATE
        """), {"account": row.account_id}).scalar_one()
        if account_state in {"disabled", "retired"}:
            raise transition_error("account was disabled during login")
        record_attention(connection, row, observation)
        connection.execute(text(f"""
            UPDATE {operation_table}
            SET status='running', ended_at=NULL, manual_intervention_at=now(),
                manual_verify_requested=false
            WHERE id=:operation
        """), {"operation": row.operation_id})
    # The assignment is now attention_required, including for a first login.
    # Subsequent completion must use that CAS state, not logging_in.
    waiting_row = SimpleNamespace(**{
        **dict(row._mapping), "kind": "verify",
        "assignment_state": "attention_required",
        "manual_intervention_recorded": True,
    })
    try:
        while not page.is_closed():
            with transaction(database) as connection:
                state = connection.execute(text(f"""
                    SELECT o.status, o.manual_verify_requested, a.state AS account_state,
                           w.desired_state, ass.ended_at
                    FROM {operation_table} o
                    JOIN {assignment_table} ass ON ass.id=o.assignment_id
                    JOIN {account_table} a ON a.id=ass.account_id
                    JOIN worker w ON w.id=o.worker_id
                    WHERE o.id=:operation FOR UPDATE OF o
                """), {"operation": row.operation_id}).one()
                if (state.status != "running" or state.ended_at is not None
                        or state.account_state in {"disabled", "retired"}
                        or state.desired_state != "enabled"):
                    raise transition_error("manual login was stopped")
                requested = state.manual_verify_requested
                if requested:
                    connection.execute(text(f"""
                        UPDATE {operation_table} SET manual_verify_requested=false
                        WHERE id=:operation
                    """), {"operation": row.operation_id})
            if requested:
                observation = observer(page)
                if matches(observation):
                    return waiting_row, observation
            # Pump Playwright events while waiting, without navigation or input.
            try:
                page.wait_for_timeout(1000)
            except BrowserError:
                if not page.is_closed():
                    raise
                break
        return waiting_row, observation
    finally:
        with transaction(database) as connection:
            connection.execute(text(f"""
                UPDATE {operation_table} SET manual_intervention_at=NULL,
                    manual_verify_requested=false WHERE id=:operation
            """), {"operation": row.operation_id})

