"""Whether a failed attempt gets another one, and when.

Retry policy here is domain logic keyed on the cause and on the identity that
ran -- a different account after a challenge, a wait after a timeout, never
after a 404. That is not "run this task again with backoff", which is why v2
§2 declines the queue library's own retry mechanism and why this is a table
rather than a flag.

Every member of core.FAILURE_KINDS appears below explicitly and there is no
default branch. v2 §4.5 asked that "every cause carries an explicit terminal
flag ... so that a cause added later cannot default to retrying forever"; the
flag is this mapping, and tests/test_retry_policy.py is what enforces it. When
the Google search work adds its causes, that test fails until each one has been
dispositioned here. `cancelled` is in that vocabulary for the same reason and is
terminal by the same mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, text

#: The failed account is already quarantined by the time this is read, so a
#: different identity is free at commit and a delay would buy nothing.
IMMEDIATE = timedelta(0)
WORKER_LOST_DELAY = timedelta(seconds=60)
#: Doubles per attempt: two minutes, then four. At a cap of three that is two
#: values, which is a backoff and not a curve.
TRANSIENT_DELAY = timedelta(seconds=120)

CAUSE_IS_TERMINAL = "cause_is_terminal"
ATTEMPTS_EXHAUSTED = "attempts_exhausted"
#: An operator stopped this request. Outranks both of the above, so the reason
#: they read is the one they caused.
CANCELLED = "cancelled"

#: A different identity may succeed where this one did not.
IDENTITY_CAUSES = frozenset({"blocked", "login_required", "account_mismatch"})
#: Neither the target nor the identity failed; the deployment did. These
#: attempts remain in history, but do not consume the request's scrape budget.
#: A central restart, lost worker host, or temporarily unavailable object store
#: may last longer than a small request-local retry ceiling.
INFRASTRUCTURE_CAUSES = frozenset({"worker_lost", "storage_error"})
TRANSIENT_CAUSES = frozenset({"timeout", "browser_error"})
#: `not_found` means the target does not exist. The next two mean the bytes
#: are good and the parser could not read them -- re-running the scrape spends
#: real LinkedIn traffic to fetch the same page and get the same result. Those
#: are fixed by re-extracting stored HTML under a new extraction generation,
#: with no traffic to the site at all. `cancelled` is terminal for a different
#: reason: a request a human stopped is not one the policy gets to restart.
TERMINAL_CAUSES = frozenset({
    "not_found", "extract_failed", "parser_drift", "cancelled"})


@dataclass(frozen=True)
class Retry:
    delay: timedelta
    reason: str = "retry_backoff"


@dataclass(frozen=True)
class Terminal:
    reason: str


def attempts_spent(connection: Connection, request_id: str) -> int:
    """Count scrape-budget executions without charging infrastructure loss.

    A Google budget preflight closes its attempt cleanly and leaves the request
    queued. Any later attempt on that same request must not lose retry capacity
    merely because no search was allowed to start. Worker and storage outages
    are likewise operational history rather than evidence that the target has
    exhausted its scrape attempts.
    """
    return connection.execute(text("""
        SELECT count(*)
        FROM scrape_attempt
        WHERE request_id=:request
          AND (ended_at IS NULL OR cause IS NOT NULL)
          AND cause IS DISTINCT FROM 'worker_lost'
          AND cause IS DISTINCT FROM 'storage_error'
    """), {"request": request_id}).scalar_one()


def _disposition(cause: str, attempt_no: int) -> Retry | Terminal:
    if cause in TERMINAL_CAUSES:
        return Terminal(CAUSE_IS_TERMINAL)
    if cause in IDENTITY_CAUSES:
        return Retry(IMMEDIATE)
    if cause == "worker_lost":
        return Retry(WORKER_LOST_DELAY)
    if cause == "storage_error":
        return Retry(TRANSIENT_DELAY)
    if cause in TRANSIENT_CAUSES:
        return Retry(TRANSIENT_DELAY * 2 ** (attempt_no - 1))
    raise ValueError(f"no retry disposition for cause {cause!r}")


def decide(*, cause: str, attempt_no: int, max_attempts: int,
           cancel_requested: bool = False) -> Retry | Terminal:
    """Retry or stop, for an attempt that has just failed.

    `attempt_no` is the number of scrape-budget attempts spent so far. Durable
    history can contain additional infrastructure failures.

    Three tiers, in this order. The disposition is computed first, so an
    unknown cause raises whether or not anything else applies. A pending cancel
    then outranks everything: the operator's stop is the reason, not the
    ceiling and not the cause. Infrastructure failures bypass the scrape
    ceiling. The ceiling wins over every other retry disposition last.
    """
    disposition = _disposition(cause, attempt_no)
    if cancel_requested:
        return Terminal(CANCELLED)
    if cause in INFRASTRUCTURE_CAUSES:
        return disposition
    if attempt_no >= max_attempts:
        return Terminal(ATTEMPTS_EXHAUSTED)
    return disposition


def terminal_status(cause: str | None, cancel_requested: bool) -> str:
    """What a request that is stopping should read as.

    A NULL cause always means `succeeded`, even under a pending cancel: a run
    whose last page landed and whose extraction was written before the poll
    fired has a complete result, and recording it as cancelled would discard
    that to honour an instruction the run outran.
    """
    if cause is None:
        return "succeeded"
    return "cancelled" if cancel_requested else "failed"


def decide_for_attempt(connection, attempt_id, *, cause, attempt_no,
                       max_attempts, cancel_requested=False, now, rate_limit=None):
    from .site_cooldowns import settle_rate_limit

    limit_decision = (
        settle_rate_limit(connection, attempt_id, now=now, metadata=rate_limit)
        if cause == "blocked" else None
    )
    decision = decide(cause=cause, attempt_no=attempt_no,
                      max_attempts=max_attempts, cancel_requested=cancel_requested)
    # Recording the site restriction matters even when this request exhausted
    # its budget or was cancelled; it protects other requests on the node.
    return decision if isinstance(decision, Terminal) else limit_decision or decision


def requeue(connection: Connection, request_id: str, *, now: datetime,
            delay: timedelta, reason: str = "retry_backoff",
            source: str = "automatic") -> None:
    """Return a request to the queue with its next attempt scheduled.

    Called inside the transaction that closes the failed attempt, so a request
    being retried never passes through `failed`: no callback announces a
    failure we intend to undo, and the outstanding-URL uniqueness index goes on
    holding, because `queued` is one of the statuses it covers.

    `reason` is what the console shows while the request waits. The claim sweep
    passes its own, more specific one.
    """
    if source not in {"automatic", "manual"}:
        raise ValueError(f"unknown retry source {source!r}")
    connection.execute(text("""
        INSERT INTO scrape_request_retry_event
            (request_id, occurred_at, source, reason, delay_seconds)
        VALUES (:request, :now, :source, :reason, :delay_seconds)
    """), {
        "request": request_id, "now": now, "source": source,
        "reason": reason, "delay_seconds": max(0, int(delay.total_seconds())),
    })
    connection.execute(text("""
        UPDATE scrape_request
        SET status='queued', next_attempt_at=:due,
            dispatch_checked_at=:now, dispatch_wait_reason=:reason,
            cancel_requested_at=NULL, satisfied_at=NULL
        WHERE id=:request
    """), {"request": request_id, "now": now, "due": now + delay,
           "reason": reason})


def fail(connection: Connection, request_id: str, *,
         status: str = "failed") -> None:
    """Stop for good. The attempt carrying the cause is closed by the caller.

    `status` is what this ending is called -- `failed` normally, `cancelled`
    when an operator asked for it. Passed in rather than recomputed here,
    because the caller is the one holding the cause.
    """
    connection.execute(text("""
        UPDATE scrape_request
        SET status=:status, next_attempt_at=NULL WHERE id=:request
    """), {"request": request_id, "status": status})
