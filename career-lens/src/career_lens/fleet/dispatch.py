"""Oldest-runnable late binding from queued requests to browser capacity."""

from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

from ..capture.options import LINKEDIN_ACCOUNT_KINDS
from . import retry
from .site_cooldowns import filter_candidates
from .eligibility import (
    choose_candidate,
    choose_google_candidate,
    choose_public_candidate,
    eligible_assignments,
    eligible_google_assignments,
    eligible_google_workers,
    eligible_public_workers,
    prefer_untried,
    prefer_untried_workers,
)

HEARTBEAT_SECONDS = 30
CLAIM_SECONDS = 600
RETRY_HANDOFF_GRACE_VARIABLE = "CAREER_LENS_RETRY_HANDOFF_GRACE_SECONDS"
DEFAULT_RETRY_HANDOFF_GRACE_SECONDS = 300


class DispatchError(RuntimeError):
    """A dispatch command was called outside its required transaction."""


def _require_transaction(connection: Connection) -> None:
    isolation = connection.get_execution_options().get("isolation_level")
    if isolation == "AUTOCOMMIT" or not connection.in_transaction():
        raise DispatchError("dispatch requires a real database transaction")


def _eligible_for(connection: Connection, kind: str, *, now: datetime,
                  heartbeat_after: datetime, request, linkedin_sections=None):
    if kind == "google_search":
        candidates, reason = eligible_google_assignments(
            connection, now=now, heartbeat_after=heartbeat_after)
    else:
        candidates, reason = eligible_assignments(
            connection, now=now, heartbeat_after=heartbeat_after, kind=kind,
            linkedin_sections=linkedin_sections)
    return filter_candidates(connection, request, candidates, reason, now=now)


def _choose_for(kind: str, candidates, rng: random.Random):
    if kind == "google_search":
        return choose_google_candidate(candidates, rng)
    return choose_candidate(candidates, rng)


def _retry_handoff_grace() -> timedelta:
    """Operator-configured window for preferring a different identity."""
    raw = os.environ.get(RETRY_HANDOFF_GRACE_VARIABLE)
    try:
        seconds = int(raw) if raw else DEFAULT_RETRY_HANDOFF_GRACE_SECONDS
    except ValueError:
        seconds = DEFAULT_RETRY_HANDOFF_GRACE_SECONDS
    if seconds < 0:
        seconds = DEFAULT_RETRY_HANDOFF_GRACE_SECONDS
    return timedelta(seconds=seconds)


def dispatch_one(connection: Connection, *, now: datetime,
                 rng: random.Random) -> int | None:
    """Assign the oldest runnable request and record why older work waits."""
    _require_transaction(connection)
    # The due time is part of the selection rather than a check after it: a
    # request in backoff is not considered at all. Snapshot the due ids, then
    # lock and recheck them one at a time. Locking the whole snapshot would
    # hide later requests from a concurrent dispatcher even when another
    # worker could run them.
    request_ids = connection.execute(text("""
        SELECT id FROM scrape_request
        WHERE status = 'queued'
          AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
        ORDER BY CASE priority WHEN 'high' THEN 0 ELSE 1 END,
                 requested_at, id
    """), {"now": now}).scalars().all()
    for request_id in request_ids:
        request = connection.execute(text("""
            SELECT id, kind, google_top_n, linkedin_sections, next_attempt_at,
                   url_normalised
            FROM scrape_request
            WHERE id=:request AND status='queued'
              AND (next_attempt_at IS NULL OR next_attempt_at <= :now)
            FOR UPDATE SKIP LOCKED
        """), {"request": request_id, "now": now}).one_or_none()
        if request is None:
            continue
        attempt_id = _dispatch_selected_request(
            connection, request, now=now, rng=rng)
        if attempt_id is not None:
            return attempt_id
    return None


def _dispatch_selected_request(connection: Connection, request, *,
                               now: datetime,
                               rng: random.Random) -> int | None:
    """Try one already locked request without making it block later work."""

    heartbeat_after = now - timedelta(seconds=HEARTBEAT_SECONDS)
    if request.kind == "generic":
        # Arbitrary websites do not consume or diagnose provider identities,
        # regardless of whether a native or extension worker captures them.
        return _dispatch_public(
            connection, request, now=now,
            heartbeat_after=heartbeat_after, rng=rng)
    if request.kind == "google_search" and request.google_top_n == 0:
        return _dispatch_public(
            connection, request, now=now,
            heartbeat_after=heartbeat_after, rng=rng)
    from .assignments import release_due_relogin_cooldowns

    release_due_relogin_cooldowns(connection, now=now)
    candidates, reason = _eligible_for(
        connection, request.kind, now=now,
        heartbeat_after=heartbeat_after,
        linkedin_sections=request.linkedin_sections, request=request)
    if not candidates:
        if reason == "accounts_recovering":
            from .assignments import queue_due_relogin

            if queue_due_relogin(connection, now=now) is not None:
                reason = "relogin_pending"
        connection.execute(text("""
            UPDATE scrape_request
            SET dispatch_checked_at=:now, dispatch_wait_reason=:reason
            WHERE id=:request
        """), {"request": request.id, "now": now, "reason": reason})
        return None

    # v2 §5.3: a retry after an identity failure should not land on an identity
    # this request has already tried. A preference rather than an exclusion --
    # see eligibility.prefer_untried. It applies to every kind: a Google
    # candidate carries an account_id too, and a search blocked on one identity
    # has the same reason to move as a profile fetch does.
    tried = set(connection.execute(text("""
        SELECT account_id FROM scrape_attempt
        WHERE request_id = :request AND account_id IS NOT NULL
    """), {"request": request.id}).scalars().all())
    untried = [candidate for candidate in candidates
               if candidate.account_id not in tried]
    handoff_deadline = (
        request.next_attempt_at + _retry_handoff_grace()
        if request.next_attempt_at is not None and tried else None
    )
    if (request.kind in LINKEDIN_ACCOUNT_KINDS
            and not untried
            and handoff_deadline is not None
            and now < handoff_deadline):
        potential, _ = eligible_assignments(
            connection, now=now, heartbeat_after=heartbeat_after,
            kind=request.kind, linkedin_sections=request.linkedin_sections,
            include_temporarily_unavailable=True,
        )
        if any(candidate.account_id not in tried for candidate in potential):
            _wait(
                connection, request.id, now=now,
                reason="waiting_for_untried_account",
            )
            return None
    preferred = untried or prefer_untried(candidates, tried)
    candidate = _choose_for(request.kind, preferred, rng)
    locked = connection.execute(text("""
        SELECT ass.id
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker ON worker.id=ass.worker_id
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        WHERE ass.id=:assignment
        FOR UPDATE OF ass, account, worker, supervisor
    """), {"assignment": candidate.assignment_id}).one_or_none()
    if locked is None:
        connection.execute(text("""
            UPDATE scrape_request
            SET dispatch_checked_at=:now,
                dispatch_wait_reason='assignment_changed'
            WHERE id=:request
        """), {"request": request.id, "now": now})
        return None
    refreshed, reason = _eligible_for(
        connection, request.kind, now=now,
        heartbeat_after=heartbeat_after,
        linkedin_sections=request.linkedin_sections, request=request)
    selected = next((item for item in refreshed
                     if item.assignment_id == candidate.assignment_id), None)
    if selected is None:
        connection.execute(text("""
            UPDATE scrape_request
            SET dispatch_checked_at=:now, dispatch_wait_reason=:reason
            WHERE id=:request
        """), {
            "request": request.id, "now": now,
            "reason": reason or "assignment_changed",
        })
        return None

    attempt_id = connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, claim_deadline_at,
             supervisor_id, worker_id, account_id, assignment_id)
        SELECT :request, COALESCE(MAX(attempt_no), 0) + 1, :now, :deadline,
               :supervisor, :worker, :account, :assignment
        FROM scrape_attempt WHERE request_id=:request
        RETURNING id
    """), {
        "request": request.id, "now": now,
        "deadline": now + timedelta(seconds=CLAIM_SECONDS),
        "supervisor": selected.supervisor_id, "worker": selected.worker_id,
        "account": selected.account_id,
        "assignment": selected.assignment_id,
    }).scalar_one()

    from ..queue.enqueue import enqueue_capture

    job_id = enqueue_capture(
        connection, attempt_id=attempt_id, worker_id=selected.worker_id)
    connection.execute(text(
        "UPDATE scrape_attempt SET queue_job_id=:job WHERE id=:attempt"),
        {"attempt": attempt_id, "job": job_id})
    connection.execute(text("""
        UPDATE scrape_request
        SET status='assigned', dispatch_checked_at=:now,
            dispatch_wait_reason=NULL, next_attempt_at=NULL
        WHERE id=:request AND status='queued'
    """), {"request": request.id, "now": now})
    return attempt_id


def _wait(connection: Connection, request_id: str, *, now: datetime,
          reason: str) -> None:
    connection.execute(text("""
        UPDATE scrape_request
        SET dispatch_checked_at=:now, dispatch_wait_reason=:reason
        WHERE id=:request
    """), {"request": request_id, "now": now, "reason": reason})


def _dispatch_public(connection: Connection, request, *, now: datetime,
                     heartbeat_after: datetime,
                     rng: random.Random) -> int | None:
    """Bind one accountless request to a compatible live worker.

    The same shape as the assignment path -- choose, lock, re-evaluate under
    the lock, then insert and enqueue in one transaction -- with the account
    and the assignment left NULL. SERP-only Google work additionally applies
    the worker's Google capability, policy and independent budget.
    """
    google_only = request.kind == "google_search"

    def eligible():
        if google_only:
            candidates, reason = eligible_google_workers(
                connection, now=now, heartbeat_after=heartbeat_after)
        else:
            candidates, reason = eligible_public_workers(
                connection,
                now=now,
                heartbeat_after=heartbeat_after,
            )
        return filter_candidates(connection, request, candidates, reason, now=now)

    candidates, reason = eligible()
    if not candidates:
        _wait(connection, request.id, now=now, reason=reason or "no_workers")
        return None

    tried = set(connection.execute(text("""
        SELECT worker_id FROM scrape_attempt
        WHERE request_id = :request AND worker_id IS NOT NULL
    """), {"request": request.id}).scalars().all())
    preferred = prefer_untried_workers(candidates, tried)
    candidate = (
        choose_google_candidate(preferred, rng)
        if google_only
        else choose_public_candidate(preferred, rng)
    )

    locked = connection.execute(text("""
        SELECT worker.id
        FROM worker
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        WHERE worker.id=:worker
        FOR UPDATE OF worker, supervisor
    """), {"worker": candidate.worker_id}).one_or_none()
    if locked is None:
        _wait(connection, request.id, now=now, reason="worker_changed")
        return None
    refreshed, reason = eligible()
    if not any(item.worker_id == candidate.worker_id for item in refreshed):
        _wait(connection, request.id, now=now,
              reason=reason or "worker_changed")
        return None

    attempt_id = connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, claim_deadline_at,
             supervisor_id, worker_id, account_id, assignment_id)
        SELECT :request, COALESCE(MAX(attempt_no), 0) + 1, :now, :deadline,
               :supervisor, :worker, NULL, NULL
        FROM scrape_attempt WHERE request_id=:request
        RETURNING id
    """), {
        "request": request.id, "now": now,
        "deadline": now + timedelta(seconds=CLAIM_SECONDS),
        "supervisor": candidate.supervisor_id, "worker": candidate.worker_id,
    }).scalar_one()

    from ..queue.enqueue import enqueue_capture

    job_id = enqueue_capture(
        connection, attempt_id=attempt_id, worker_id=candidate.worker_id)
    connection.execute(text(
        "UPDATE scrape_attempt SET queue_job_id=:job WHERE id=:attempt"),
        {"attempt": attempt_id, "job": job_id})
    connection.execute(text("""
        UPDATE scrape_request
        SET status='assigned', dispatch_checked_at=:now,
            dispatch_wait_reason=NULL, next_attempt_at=NULL
        WHERE id=:request AND status='queued'
    """), {"request": request.id, "now": now})
    return attempt_id


def dispatch_pending(*, database: str | None = None,
                     now: datetime | None = None,
                     rng: random.Random | None = None) -> list[int]:
    """Fill every currently eligible worker, one atomic assignment at a time."""
    from ..store.db import transaction
    from .assignments import release_due_relogin_cooldowns

    clock = now or datetime.now(UTC)
    source = rng or random.SystemRandom()
    # Recovery deadlines advance even when there is no request waiting to
    # force the provider-specific dispatch path. Besides restoring capacity,
    # this keeps the operator API and UI from showing an expired cooldown.
    with transaction(database) as connection:
        release_due_relogin_cooldowns(connection, now=clock)
    assigned: list[int] = []
    while True:
        with transaction(database) as connection:
            attempt_id = dispatch_one(connection, now=clock, rng=source)
        if attempt_id is None:
            # Recovery is opportunistic but not request-dependent. Dispatch
            # ordinary work first so a due login never jumps an interactive
            # search, then use otherwise-idle native workers to restore
            # provider capacity even when another account handled the request.
            with transaction(database) as connection:
                from .assignments import queue_due_relogin

                while queue_due_relogin(connection, now=clock) is not None:
                    pass
            return assigned
        assigned.append(attempt_id)


def sweep_expired_claims(*, database: str | None = None,
                         now: datetime | None = None) -> list[str]:
    """Release claims that expired before a worker picked them up."""
    from ..store.db import transaction

    checked_at = now or datetime.now(UTC)
    with transaction(database) as connection:
        rows = connection.execute(text("""
            SELECT attempt.id, attempt.request_id, attempt.worker_id,
                   attempt.attempt_no, request.max_attempts,
                   request.cancel_requested_at IS NOT NULL AS cancel_requested
            FROM scrape_attempt attempt
            JOIN scrape_request request ON request.id = attempt.request_id
            WHERE attempt.started_at IS NULL AND attempt.ended_at IS NULL
              AND attempt.claim_deadline_at IS NOT NULL
              AND attempt.claim_deadline_at < :now
            FOR UPDATE OF attempt SKIP LOCKED
        """), {
            "now": checked_at,
        }).all()
        for row in rows:
            connection.execute(text("""
                UPDATE scrape_attempt
                SET ended_at=:now, cause='worker_lost',
                    cause_detail='the assigned worker did not claim this attempt in time'
                WHERE id=:attempt
            """), {"attempt": row.id, "now": checked_at})
            # One cap, not two. This used to count only `worker_lost`
            # expirations against a limit of its own, which was a second and
            # narrower budget for the thing max_attempts now governs.
            decision = retry.decide(
                cause="worker_lost",
                attempt_no=retry.attempts_spent(connection, row.request_id),
                max_attempts=row.max_attempts,
                cancel_requested=row.cancel_requested)
            if isinstance(decision, retry.Retry):
                # Its own wait reason rather than the generic backoff: the
                # console already explains this one precisely.
                retry.requeue(connection, row.request_id, now=checked_at,
                              delay=decision.delay, reason="claim_expired")
            else:
                retry.fail(connection, row.request_id,
                           status=retry.terminal_status(
                               "worker_lost", row.cancel_requested))
        return [row.request_id for row in rows]
