"""Central eligibility policy for browser work.

The LinkedIn and Google policies remain anchored to a ready
`account_worker_assignment`. Public fallback asks whether a compatible live
browser worker is free.
"""

from __future__ import annotations

import random
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from sqlalchemy import Connection, text



@dataclass(frozen=True)
class Candidate:
    assignment_id: str
    account_id: str
    worker_id: str
    supervisor_id: str
    remaining_attempts: int


@dataclass(frozen=True)
class PublicCandidate:
    """One live Chrome worker, with no account behind it."""

    worker_id: str
    supervisor_id: str


@dataclass(frozen=True)
class GoogleCandidate:
    assignment_id: str
    account_id: str
    worker_id: str
    supervisor_id: str
    remaining_searches: int


@dataclass(frozen=True)
class GoogleWorkerCandidate:
    """One browser worker that can run a SERP without a provider identity."""

    worker_id: str
    supervisor_id: str
    remaining_searches: int


#: The two differ in the headroom they carry and therefore in how they are
#: weighted, but both name the account they would run under. Policy that reads
#: only the identity -- `prefer_untried` -- is written against this instead of
#: being duplicated per kind.
Anchored = TypeVar("Anchored", Candidate, GoogleCandidate)


def _reason(rows) -> str:
    """Choose one stable diagnostic without pretending it is domain truth."""
    if not rows:
        return "no_assignments"
    if not any(row.worker_ok for row in rows):
        return "all_supervisors_offline"
    rows = [row for row in rows if row.worker_ok]
    if not any(row.worker_live for row in rows):
        return "all_workers_offline"
    rows = [row for row in rows if row.worker_live]
    if not any(row.worker_enabled and row.worker_healthy for row in rows):
        return "no_healthy_workers"
    rows = [row for row in rows if row.worker_enabled and row.worker_healthy]
    if not any(row.recovery_ready for row in rows):
        return "accounts_recovering"
    rows = [row for row in rows if row.recovery_ready]
    if not any(row.assignment_ready for row in rows):
        return "no_ready_assignments"
    rows = [row for row in rows if row.assignment_ready]
    if not any(row.account_enabled for row in rows):
        return "accounts_require_attention"
    rows = [row for row in rows if row.account_enabled]
    if not any(row.schedule_active for row in rows):
        return "schedule_required"
    rows = [row for row in rows if row.schedule_active]
    if not any(row.cooldown_ok for row in rows):
        return "accounts_cooling_down"
    rows = [row for row in rows if row.cooldown_ok]
    # Before budget deliberately: at 02:00 an account may be both off shift and
    # out of attempts, and "off shift" is the answer that says when it resumes.
    if not any(row.shift_planned for row in rows):
        return "shift_not_planned"
    rows = [row for row in rows if row.shift_planned]
    if not any(row.on_shift for row in rows):
        return "accounts_off_shift"
    rows = [row for row in rows if row.on_shift]
    if not any(row.budget_ok for row in rows):
        return "budgets_exhausted"
    rows = [row for row in rows if row.budget_ok]
    if not any(row.interval_ok for row in rows):
        return "minimum_interval"
    rows = [row for row in rows if row.interval_ok]
    if not any(not row.busy for row in rows):
        return "all_workers_busy"
    return "no_eligible_assignments"


def eligible_assignments(
        connection: Connection, *, now: datetime,
        heartbeat_after: datetime,
        kind: str = "linkedin_person",
        linkedin_sections: Collection[str] | None = None,
        ignore_attempt_id: int | None = None,
        include_temporarily_unavailable: bool = False,
) -> tuple[list[Candidate], str | None]:
    """Return assignments usable now, or also ones blocked only briefly.

    The broader view exists for retry handoff: it lets dispatch see that an
    untried identity is on shift and otherwise usable while its worker is busy
    or its minimum interval is running. Such a candidate is never selected for
    an attempt until the ordinary eligibility call says it is ready.
    """
    window_start = now - timedelta(hours=24)
    rows = connection.execute(text("""
        SELECT ass.id AS assignment_id, ass.account_id, ass.worker_id,
               worker.supervisor_id,
                              (supervisor.desired_state = 'active'
                AND supervisor.last_heartbeat_at >= :heartbeat_after) AS worker_ok,
               (worker.last_heartbeat_at IS NOT NULL
                AND worker.last_heartbeat_at >= :heartbeat_after) AS worker_live,
               (worker.desired_state = 'enabled') AS worker_enabled,
               -- `unknown` is dispatchable. Nothing native sets a
               -- worker `healthy` any more, and a session that is not
               -- ready is excluded by `assignment_ready` and
               -- `account_enabled` independently of health.
               (worker.health IN ('healthy', 'unknown'))
                    AS worker_healthy,
               (ass.state = 'ready' AND ass.ended_at IS NULL) AS assignment_ready,
               (account.state = 'enabled') AS account_enabled,
               (sched.account_id IS NOT NULL AND sched.enabled)
                    AS schedule_active,
               (account.cooldown_until IS NULL
                OR account.cooldown_until <= :now) AS cooldown_ok,
               (account.recovery_phase IS NULL) AS recovery_ready,
               (shift.pace IS NOT NULL) AS on_shift,
               (sched.enabled AND EXISTS (
                    SELECT 1 FROM account_shift_day day
                    WHERE day.account_id = account.id
                      AND day.local_date =
                          ((:now AT TIME ZONE sched.timezone)::date)))
                    AS shift_planned,
               (account.max_attempts_24h - COALESCE(events.used, 0))
                    AS remaining_attempts,
               (COALESCE(events.used, 0) < account.max_attempts_24h)
                    AS budget_ok,
               -- The current work block's pace stretches or shortens the
               -- account's baseline spacing, so the day is not uniform.
               (events.latest IS NULL OR events.latest <=
                    :now - make_interval(secs => account.min_interval_seconds
                                                 * COALESCE(shift.pace, 1)))
                    AS interval_ok,
               (EXISTS (
                    SELECT 1 FROM worker_operation operation
                    WHERE operation.worker_id = worker.id
                      AND operation.status IN ('queued', 'running'))
                OR EXISTS (
                    SELECT 1 FROM scrape_attempt attempt
                    WHERE attempt.worker_id = worker.id
                      AND attempt.ended_at IS NULL
                      AND (CAST(:ignore_attempt_id AS bigint) IS NULL
                           OR attempt.id <>
                              CAST(:ignore_attempt_id AS bigint)))) AS busy
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id = ass.account_id
        JOIN worker ON worker.id = ass.worker_id
        JOIN supervisor ON supervisor.id = worker.supervisor_id
        LEFT JOIN account_schedule sched ON sched.account_id = account.id
        -- The block covering this instant, if there is one. Half-open, so the
        -- moment a block ends belongs to the next block and never to both.
        LEFT JOIN LATERAL (
            SELECT block.pace
            FROM account_shift_block block
            WHERE block.account_id = account.id AND block.kind = 'work'
              AND :now >= block.starts_at AND :now < block.ends_at
            LIMIT 1
        ) shift ON TRUE
        LEFT JOIN LATERAL (
            SELECT count(*)::integer AS used, max(occurred_at) AS latest
            FROM linkedin_account_event event
            WHERE event.account_id = account.id
              AND event.kind = 'attempt_started'
              AND event.occurred_at >= :window_start
              AND event.occurred_at <= :now
        ) events ON TRUE
        WHERE ass.ended_at IS NULL
        ORDER BY ass.assigned_at, ass.id
    """), {
        "now": now, "heartbeat_after": heartbeat_after,
        "window_start": window_start, "kind": kind,
        "ignore_attempt_id": ignore_attempt_id,
    }).all()

    candidates = [
        Candidate(
            assignment_id=row.assignment_id, account_id=row.account_id,
            worker_id=row.worker_id, supervisor_id=row.supervisor_id,
            remaining_attempts=row.remaining_attempts,)
        for row in rows
        if (row.worker_ok and row.worker_live and row.worker_enabled
            and row.worker_healthy and row.assignment_ready
            and row.account_enabled and row.schedule_active
            and row.cooldown_ok and row.recovery_ready
            and row.shift_planned and row.on_shift
            and row.budget_ok
            and (include_temporarily_unavailable
                 or (row.interval_ok and not row.busy)))
    ]
    return (candidates, None) if candidates else ([], _reason(rows))


def _google_reason(rows) -> str:
    if not rows:
        return "no_assignments"
    if not any(row.worker_ok for row in rows):
        return "all_supervisors_offline"
    rows = [row for row in rows if row.worker_ok]
    if not any(row.worker_live for row in rows):
        return "all_workers_offline"
    rows = [row for row in rows if row.worker_live]
    if not any(row.worker_enabled and row.worker_healthy for row in rows):
        return "no_healthy_workers"
    rows = [row for row in rows if row.worker_enabled and row.worker_healthy]
    if not any(row.assignment_ready for row in rows):
        return "no_ready_assignments"
    rows = [row for row in rows if row.assignment_ready]
    if not any(row.google_capable for row in rows):
        return "no_google_capable_workers"
    rows = [row for row in rows if row.google_capable]
    if not any(row.budget_ok for row in rows):
        return "google_budget_exhausted"
    rows = [row for row in rows if row.budget_ok]
    if not any(not row.busy for row in rows):
        return "all_workers_busy"
    return "no_eligible_assignments"


def eligible_google_assignments(
        connection: Connection, *, now: datetime,
        heartbeat_after: datetime) -> tuple[list[GoogleCandidate], str | None]:
    """Return workers that can run Google without applying LinkedIn pacing.

    A search that opens result pages still needs a LinkedIn session, so this
    path is anchored on an assignment.
    """
    hour_start = now - timedelta(hours=1)
    day_start = now - timedelta(hours=24)
    rows = connection.execute(text("""
        SELECT ass.id AS assignment_id, ass.account_id, ass.worker_id,
               worker.supervisor_id,
               (supervisor.desired_state = 'active'
                AND supervisor.last_heartbeat_at >= :heartbeat_after) AS worker_ok,
               (worker.last_heartbeat_at IS NOT NULL
                AND worker.last_heartbeat_at >= :heartbeat_after) AS worker_live,
               (worker.desired_state = 'enabled') AS worker_enabled,
               -- `unknown` is dispatchable. Nothing native sets a
               -- worker `healthy` any more, and a session that is not
               -- ready is excluded by `assignment_ready` and
               -- `account_enabled` independently of health.
               (worker.health IN ('healthy', 'unknown'))
                    AS worker_healthy,
               (ass.state = 'ready' AND ass.ended_at IS NULL)
                    AS assignment_ready,
               (worker.capabilities @> '["google_search"]'::jsonb
                AND policy.worker_id IS NOT NULL
                AND policy.enabled) AS google_capable,
               LEAST(
                    COALESCE(policy.max_searches_hour, 0)
                        - COALESCE(events.hour_used, 0),
                    COALESCE(policy.max_searches_24h, 0)
                        - COALESCE(events.day_used, 0)) AS remaining_searches,
               (policy.enabled
                AND COALESCE(events.hour_used, 0)
                    < policy.max_searches_hour
                AND COALESCE(events.day_used, 0)
                    < policy.max_searches_24h) AS budget_ok,
               (EXISTS (
                    SELECT 1 FROM worker_operation operation
                    WHERE operation.worker_id = worker.id
                      AND operation.status IN ('queued', 'running'))
                OR EXISTS (
                    SELECT 1 FROM scrape_attempt attempt
                    WHERE attempt.worker_id = worker.id
                      AND attempt.ended_at IS NULL)) AS busy
        FROM account_worker_assignment ass
        JOIN worker ON worker.id = ass.worker_id
        JOIN supervisor ON supervisor.id = worker.supervisor_id
        LEFT JOIN worker_google_policy policy
               ON policy.worker_id = worker.id
        LEFT JOIN LATERAL (
            SELECT
                count(*) FILTER (
                    WHERE event.occurred_at >= :hour_start)::integer
                    AS hour_used,
                count(*)::integer AS day_used
            FROM worker_google_event event
            WHERE event.worker_id = worker.id
              AND event.occurred_at >= :day_start
              AND event.occurred_at <= :now
        ) events ON TRUE
        WHERE ass.ended_at IS NULL
        ORDER BY ass.assigned_at, ass.id
    """), {
        "now": now, "heartbeat_after": heartbeat_after,
        "hour_start": hour_start, "day_start": day_start,
    }).all()

    candidates = [
        GoogleCandidate(
            assignment_id=row.assignment_id, account_id=row.account_id,
            worker_id=row.worker_id, supervisor_id=row.supervisor_id,
            remaining_searches=row.remaining_searches,)
        for row in rows
        if (row.worker_ok and row.worker_live and row.worker_enabled
            and row.worker_healthy and row.assignment_ready
            and row.google_capable and row.budget_ok and not row.busy)
    ]
    return (candidates, None) if candidates else ([], _google_reason(rows))


def eligible_google_workers(
        connection: Connection, *, now: datetime,
        heartbeat_after: datetime,
        allow_attempt_id: int | None = None,
) -> tuple[list[GoogleWorkerCandidate], str | None]:
    """Return workers that can capture a SERP without a provider account.

    A worker advertises the `google_search` capability token.
    """
    hour_start = now - timedelta(hours=1)
    day_start = now - timedelta(hours=24)
    rows = connection.execute(text("""
        SELECT worker.id AS worker_id, worker.supervisor_id,
               (supervisor.desired_state = 'active'
                AND supervisor.last_heartbeat_at >= :heartbeat_after)
                    AS supervisor_ok,
               (worker.last_heartbeat_at IS NOT NULL
                AND worker.last_heartbeat_at >= :heartbeat_after) AS worker_live,
               (worker.desired_state = 'enabled') AS worker_enabled,
               (worker.health IN ('healthy', 'unknown')) AS browser_usable,
               (worker.capabilities @> '["google_search"]'::jsonb
                AND policy.worker_id IS NOT NULL
                AND policy.enabled) AS google_capable,
               LEAST(
                    COALESCE(policy.max_searches_hour, 0)
                        - COALESCE(events.hour_used, 0),
                    COALESCE(policy.max_searches_24h, 0)
                        - COALESCE(events.day_used, 0)) AS remaining_searches,
               (policy.enabled
                AND COALESCE(events.hour_used, 0)
                    < policy.max_searches_hour
                AND COALESCE(events.day_used, 0)
                    < policy.max_searches_24h) AS budget_ok,
               (EXISTS (
                    SELECT 1 FROM worker_operation operation
                    WHERE operation.worker_id = worker.id
                      AND operation.status IN ('queued', 'running'))
                OR EXISTS (
                    SELECT 1 FROM scrape_attempt attempt
                    WHERE attempt.worker_id = worker.id
                      AND attempt.ended_at IS NULL
                      AND (CAST(:allow_attempt_id AS bigint) IS NULL
                           OR attempt.id <>
                              CAST(:allow_attempt_id AS bigint)))) AS busy
        FROM worker
        JOIN supervisor ON supervisor.id = worker.supervisor_id
        LEFT JOIN worker_google_policy policy
               ON policy.worker_id = worker.id
        LEFT JOIN LATERAL (
            SELECT
                count(*) FILTER (
                    WHERE event.occurred_at >= :hour_start)::integer
                    AS hour_used,
                count(*)::integer AS day_used
            FROM worker_google_event event
            WHERE event.worker_id = worker.id
              AND event.occurred_at >= :day_start
              AND event.occurred_at <= :now
        ) events ON TRUE
        ORDER BY worker.id
    """), {
        "now": now,
        "heartbeat_after": heartbeat_after,
        "hour_start": hour_start,
        "day_start": day_start,
        "allow_attempt_id": allow_attempt_id,
    }).all()

    candidates = [
        GoogleWorkerCandidate(
            worker_id=row.worker_id,
            supervisor_id=row.supervisor_id,
            remaining_searches=row.remaining_searches,
        )
        for row in rows
        if (row.supervisor_ok and row.worker_live and row.worker_enabled
            and row.browser_usable and row.google_capable
            and row.budget_ok and not row.busy)
    ]
    return (candidates, None) if candidates else ([], _google_worker_reason(rows))


def _google_worker_reason(rows) -> str:
    if not rows:
        return "no_workers"
    if not any(row.supervisor_ok for row in rows):
        return "all_supervisors_offline"
    rows = [row for row in rows if row.supervisor_ok]
    if not any(row.worker_live for row in rows):
        return "all_workers_offline"
    rows = [row for row in rows if row.worker_live]
    if not any(row.worker_enabled and row.browser_usable for row in rows):
        return "no_healthy_workers"
    rows = [row for row in rows if row.worker_enabled and row.browser_usable]
    if not any(row.google_capable for row in rows):
        return "no_google_capable_workers"
    rows = [row for row in rows if row.google_capable]
    if not any(row.budget_ok for row in rows):
        return "google_budget_exhausted"
    rows = [row for row in rows if row.budget_ok]
    if not any(not row.busy for row in rows):
        return "all_workers_busy"
    return "no_eligible_workers"


def choose_candidate(candidates: Sequence[Candidate],
                     rng: random.Random) -> Candidate:
    """Select proportionally to remaining headroom using an injected RNG."""
    if not candidates:
        raise ValueError("no eligible candidates")
    total = sum(candidate.remaining_attempts for candidate in candidates)
    if total <= 0:
        raise ValueError("eligible candidates have no remaining headroom")
    mark = rng.uniform(0, total)
    cumulative = 0
    for candidate in candidates:
        cumulative += candidate.remaining_attempts
        if mark < cumulative:
            return candidate
    return candidates[-1]


def prefer_untried(candidates: Sequence[Anchored],
                   tried: Collection[str]) -> list[Anchored]:
    """Candidates on an account this request has not attempted, or all of them.

    v2 §5.3 says a retry after an identity failure "must select an identity not
    already tried for that job". Read strictly that is an exclusion, and on a
    two-account fleet an exclusion strands the request with attempts left
    unspent. A preference honours the rule wherever the fleet can honour it and
    degrades to trying the same account again where it cannot.

    Selection proper is left to the caller's chooser: weighted rotation then
    applies within the preferred set, so the two policies compose instead of
    competing. This never returns an empty list for a non-empty input, so it
    cannot make a request ineligible or produce a new wait reason.

    Both candidate kinds are accepted, because both are anchored to an account
    and the rule is about the identity rather than about what it is running.
    """
    untried = [candidate for candidate in candidates
               if candidate.account_id not in tried]
    return untried or list(candidates)


def choose_google_candidate(
        candidates: Sequence[GoogleCandidate | GoogleWorkerCandidate],
        rng: random.Random) -> GoogleCandidate | GoogleWorkerCandidate:
    """Select a Google worker proportionally to its independent headroom."""
    if not candidates:
        raise ValueError("no eligible Google candidates")
    total = sum(candidate.remaining_searches for candidate in candidates)
    if total <= 0:
        raise ValueError("eligible Google candidates have no remaining headroom")
    mark = rng.uniform(0, total)
    cumulative = 0
    for candidate in candidates:
        cumulative += candidate.remaining_searches
        if mark < cumulative:
            return candidate
    return candidates[-1]


def _public_reason(rows) -> str:
    """The closest existing stable reason, in capacity terms.

    Deliberately reuses the vocabulary the console already explains. None of
    these mention assignments, accounts, cooldown, shifts or budgets, because
    a public capture is blocked by none of those and saying otherwise would
    send an operator to repair something that is not broken.
    """
    if not rows:
        return "no_workers"
    if not any(row.supervisor_ok for row in rows):
        return "all_supervisors_offline"
    rows = [row for row in rows if row.supervisor_ok]
    if not any(row.worker_live for row in rows):
        return "all_workers_offline"
    rows = [row for row in rows if row.worker_live]
    if not any(row.worker_enabled and row.browser_usable for row in rows):
        return "no_healthy_workers"
    rows = [row for row in rows if row.worker_enabled and row.browser_usable]
    if not any(not row.busy for row in rows):
        return "all_workers_busy"
    return "no_eligible_workers"


def eligible_public_workers(
        connection: Connection, *, now: datetime,
        heartbeat_after: datetime,
        allow_attempt_id: int | None = None,
) -> tuple[list[PublicCandidate], str | None]:
    """Every worker that can open a public page, and one reason when none can.

    `allow_attempt_id` exempts one open attempt from the busy test. Dispatch
    passes None, because at that point no attempt exists; the claim recheck
    passes its own attempt id, so a worker is not judged busy by the very work
    it is being asked to confirm it may do.
    """
    rows = connection.execute(text("""
        SELECT worker.id AS worker_id, worker.supervisor_id,
               (supervisor.desired_state = 'active'
                AND supervisor.last_heartbeat_at >= :heartbeat_after)
                    AS supervisor_ok,
               (worker.last_heartbeat_at IS NOT NULL
                AND worker.last_heartbeat_at >= :heartbeat_after) AS worker_live,
               (worker.desired_state = 'enabled') AS worker_enabled,
               -- Every attention code a worker can carry is an observation
               -- of our own machinery, so all of them block a public capture
               -- and no exception list is needed.
               (worker.health IN ('healthy', 'unknown')) AS browser_usable,
               (EXISTS (
                    SELECT 1 FROM worker_operation operation
                    WHERE operation.worker_id = worker.id
                      AND operation.status IN ('queued', 'running'))
                OR EXISTS (
                    SELECT 1 FROM scrape_attempt attempt
                    WHERE attempt.worker_id = worker.id
                      AND attempt.ended_at IS NULL
                      -- Cast because the only use of this parameter is an
                      -- IS NULL test, which leaves its type undetermined.
                      AND (CAST(:allow_attempt AS bigint) IS NULL
                           OR attempt.id <> :allow_attempt))) AS busy
        FROM worker
        JOIN supervisor ON supervisor.id = worker.supervisor_id
        ORDER BY worker.id
    """), {
        "heartbeat_after": heartbeat_after,
        "allow_attempt": allow_attempt_id,
    }).all()

    candidates = [
        PublicCandidate(worker_id=row.worker_id,
                        supervisor_id=row.supervisor_id,)
        for row in rows
        if (row.supervisor_ok and row.worker_live and row.worker_enabled
            and row.browser_usable and not row.busy)
    ]
    return (candidates, None) if candidates else ([], _public_reason(rows))


def choose_public_candidate(candidates: Sequence[PublicCandidate],
                            rng: random.Random) -> PublicCandidate:
    """Select uniformly. A public worker carries no headroom to weight by."""
    if not candidates:
        raise ValueError("no eligible public workers")
    mark = rng.uniform(0, len(candidates))
    index = min(int(mark), len(candidates) - 1)
    return candidates[index]


def prefer_untried_workers(
        candidates: Sequence[PublicCandidate],
        tried: Collection[str]) -> list[PublicCandidate]:
    """Workers this request has not run on, or all of them.

    The same preference `prefer_untried` applies to identities, read against
    the only thing an anonymous attempt has: the browser it ran in. A
    preference rather than an exclusion, so a one-worker fleet still spends
    the attempts the request was given.
    """
    untried = [candidate for candidate in candidates
               if candidate.worker_id not in tried]
    return untried or list(candidates)
