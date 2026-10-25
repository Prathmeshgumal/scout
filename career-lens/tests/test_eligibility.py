"""Eligibility combines durable policy with fresh browser truth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _fleet(connection, *, account="la-a", worker="worker-a", assignment="as-a",
           maximum=3, scheduled=True):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-a', 'Laptop', 'active', 'instance-a', 'host-a',
                'test', :now, :now)
        ON CONFLICT (id) DO NOTHING
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES (:worker, 'worker-a', :worker, 'enabled', 'healthy', :now)
    """), {"worker": worker, "now": NOW})
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES (:account, :account, :username, 'enabled', :maximum, 60,
                :now, :now)
    """), {"account": account, "username": f"{account}@example.com",
             "maximum": maximum, "now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at)
        VALUES (:assignment, :account, :worker, 'ready', :now, :now)
    """), {"assignment": assignment, "account": account, "worker": worker,
             "now": NOW})
    if scheduled:
        _scheduled(
            connection, account=account,
            blocks=[("work", -60, 60, "1.00")],
        )


def _eligible(connection):
    from career_lens.fleet.eligibility import eligible_assignments

    return eligible_assignments(
        connection, now=NOW, heartbeat_after=NOW - timedelta(seconds=30))


def _enable_google(connection, *, hourly=10, daily=20):
    connection.execute(text("""
        UPDATE worker
        SET capabilities='["google_search"]'::jsonb
        WHERE id='worker-a'
    """))
    connection.execute(text("""
        INSERT INTO worker_google_policy
            (worker_id, max_searches_hour, max_searches_24h)
        VALUES ('worker-a', :hourly, :daily)
    """), {"hourly": hourly, "daily": daily})


def _eligible_google(connection):
    from career_lens.fleet.eligibility import eligible_google_assignments

    return eligible_google_assignments(
        connection, now=NOW, heartbeat_after=NOW - timedelta(seconds=30))


def test_ready_fresh_assignment_reports_remaining_headroom(app):
    _fleet(app)
    candidates, reason = _eligible(app)
    assert reason is None
    assert [candidate.remaining_attempts for candidate in candidates] == [3]
    assert candidates[0].worker_id == "worker-a"


def test_native_company_worker_does_not_require_a_server_recipe(app):
    from career_lens.fleet.eligibility import eligible_assignments

    _fleet(app)

    candidates, reason = eligible_assignments(
        app,
        now=NOW,
        heartbeat_after=NOW - timedelta(seconds=30),
        kind="linkedin_company",
        linkedin_sections=(),
    )

    assert reason is None
    assert [candidate.worker_id for candidate in candidates] == ["worker-a"]


@pytest.mark.parametrize(("mutation", "reason"), [
    ("UPDATE supervisor SET last_heartbeat_at=:old", "all_supervisors_offline"),
    ("UPDATE worker SET health='attention_required', "
     "attention_recorded_at=now(), "
     "operator_attention_code='worker_exited'", "no_healthy_workers"),
    ("UPDATE account_worker_assignment SET state='attention_required', "
     "operator_attention_code='linkedin_challenge'",
     "no_ready_assignments"),
    ("UPDATE linkedin_account SET state='attention_required', "
     "operator_attention_code='manual_hold'",
     "accounts_require_attention"),
    ("UPDATE linkedin_account SET cooldown_until=:future",
     "accounts_cooling_down"),
])
def test_exclusion_reports_a_stable_operator_reason(app, mutation, reason):
    _fleet(app)
    app.execute(text(mutation), {
        "old": NOW - timedelta(minutes=5),
        "future": NOW + timedelta(minutes=5),
    })
    assert _eligible(app) == ([], reason)


def test_open_operation_makes_the_slot_busy(app):
    _fleet(app)
    app.execute(text("""
        INSERT INTO worker_operation
            (id, worker_id, assignment_id, kind, status, requested_at)
        VALUES ('op-a', 'worker-a', 'as-a', 'verify', 'queued', :now)
    """), {"now": NOW})
    assert _eligible(app) == ([], "all_workers_busy")


def test_open_capture_attempt_makes_the_slot_busy(app):
    _fleet(app)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES ('rq-open', 'https://example.com/open',
                'https://example.com/open', 'generic', 'assigned', :now)
    """), {"now": NOW})
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
             account_id, assignment_id)
        VALUES ('rq-open', 1, :now, 'worker-a', 'worker-a', 'la-a', 'as-a')
    """), {"now": NOW})

    assert _eligible(app) == ([], "all_workers_busy")


def test_rolling_budget_and_minimum_interval_are_enforced(app):
    _fleet(app, maximum=2)
    for at in (NOW - timedelta(hours=2), NOW - timedelta(seconds=30)):
        app.execute(text("""
            INSERT INTO linkedin_account_event
                (account_id, worker_id, kind, occurred_at, detail)
            VALUES ('la-a', 'worker-a', 'attempt_started', :at, '{}'::jsonb)
        """), {"at": at})
    assert _eligible(app) == ([], "budgets_exhausted")

    app.execute(text(
        "UPDATE linkedin_account SET max_attempts_24h=3 WHERE id='la-a'"))
    assert _eligible(app) == ([], "minimum_interval")


def test_events_before_the_24_hour_boundary_do_not_consume_budget(app):
    _fleet(app, maximum=1)
    app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES ('la-a', 'worker-a', 'attempt_started', :at, '{}'::jsonb)
    """), {"at": NOW - timedelta(hours=24, microseconds=1)})
    candidates, reason = _eligible(app)
    assert reason is None
    assert candidates[0].remaining_attempts == 1


def _scheduled(connection, *, blocks, enabled=True, timezone="UTC",
               account="la-a", planned=True):
    """Give an account a schedule, and hand-write today's plan.

    The blocks are written directly rather than generated: this is a test of
    what eligibility does with a plan, not of how the plan was arrived at.
    """
    connection.execute(text("""
        INSERT INTO account_schedule
            (account_id, enabled, timezone, seed, start_jitter_minutes,
             end_jitter_minutes, created_at, updated_at)
        VALUES (:account, :enabled, :timezone, 1, 0, 0, :now, :now)
    """), {"account": account, "enabled": enabled, "timezone": timezone,
             "now": NOW})
    if not planned:
        return
    day_id = connection.execute(text("""
        INSERT INTO account_shift_day (account_id, local_date, generated_at)
        VALUES (:account, CAST(:now AS timestamptz) AT TIME ZONE :timezone, :now)
        RETURNING id
    """), {"account": account, "timezone": timezone, "now": NOW}).scalar_one()
    for ordinal, (kind, opens, closes, pace) in enumerate(blocks):
        connection.execute(text("""
            INSERT INTO account_shift_block
                (shift_day_id, account_id, ordinal, kind, starts_at, ends_at,
                 pace)
            VALUES (:day, :account, :ordinal, :kind, :starts_at, :ends_at,
                    :pace)
        """), {
            "day": day_id, "account": account, "ordinal": ordinal, "kind": kind,
            "starts_at": NOW + timedelta(minutes=opens),
            "ends_at": NOW + timedelta(minutes=closes), "pace": pace,
        })


def test_an_account_without_a_schedule_is_not_eligible(app):
    _fleet(app, scheduled=False)
    assert _eligible(app) == ([], "schedule_required")


def test_an_account_inside_a_work_block_is_eligible(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", -60, 60, "1.00")])
    assert _eligible(app)[1] is None


def test_an_account_on_a_break_is_not_dispatched(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", -120, -60, "1.00"),
                            ("break", -60, 60, None)])
    assert _eligible(app) == ([], "accounts_off_shift")


def test_an_account_whose_day_has_no_blocks_is_off_shift(app):
    """A day off is planned and empty, and reads as off shift, not as a fault."""
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[])
    assert _eligible(app) == ([], "accounts_off_shift")


def test_a_disabled_schedule_is_not_eligible(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("break", -60, 60, None)], enabled=False)
    assert _eligible(app) == ([], "schedule_required")


def test_a_day_that_was_never_planned_is_named_as_a_fault(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[], planned=False)
    assert _eligible(app) == ([], "shift_not_planned")


def test_a_block_boundary_is_half_open(app):
    """The instant a block ends belongs to the next one, never to both."""
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", -60, 0, "1.00")])
    assert _eligible(app) == ([], "accounts_off_shift")


def test_a_block_boundary_includes_its_first_instant(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", 0, 60, "1.00")])
    assert _eligible(app)[1] is None


def test_pace_stretches_the_minimum_interval(app):
    _fleet(app, scheduled=False)         # min_interval_seconds = 60
    _scheduled(app, blocks=[("work", -60, 60, "2.00")])
    app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES ('la-a', 'worker-a', 'attempt_started', :then, '{}'::jsonb)
    """), {"then": NOW - timedelta(seconds=90)})
    assert _eligible(app) == ([], "minimum_interval")


def test_a_quick_pace_shortens_the_minimum_interval(app):
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", -60, 60, "0.50")])
    app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES ('la-a', 'worker-a', 'attempt_started', :then, '{}'::jsonb)
    """), {"then": NOW - timedelta(seconds=45)})
    assert _eligible(app)[1] is None


def test_off_shift_outranks_an_exhausted_budget(app):
    """At 02:00 the useful answer is when it resumes, not that it is spent."""
    _fleet(app, maximum=1, scheduled=False)
    _scheduled(app, blocks=[("break", -60, 60, None)])
    app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES ('la-a', 'worker-a', 'attempt_started', :then, '{}'::jsonb)
    """), {"then": NOW - timedelta(hours=1)})
    assert _eligible(app) == ([], "accounts_off_shift")


def test_the_account_timezone_decides_which_day_is_today(app):
    """Same instant, same blocks; only the local date differs."""
    _fleet(app, scheduled=False)
    _scheduled(app, blocks=[("work", -60, 60, "1.00")],
               timezone="Pacific/Kiritimati")
    assert _eligible(app)[1] is None


def test_google_ignores_linkedin_schedule_cooldown_budget_and_interval(app):
    _fleet(app, maximum=1, scheduled=False)
    _enable_google(app)
    _scheduled(app, blocks=[("break", -60, 60, None)])
    app.execute(text("""
        UPDATE linkedin_account
        SET cooldown_until=:future, min_interval_seconds=86400
        WHERE id='la-a'
    """), {"future": NOW + timedelta(days=1)})
    app.execute(text("""
        INSERT INTO linkedin_account_event
            (account_id, worker_id, kind, occurred_at, detail)
        VALUES ('la-a', 'worker-a', 'attempt_started', :now, '{}'::jsonb)
    """), {"now": NOW})

    candidates, reason = _eligible_google(app)

    assert reason is None
    assert [(candidate.worker_id, candidate.remaining_searches)
            for candidate in candidates] == [("worker-a", 10)]
    assert _eligible(app) == ([], "accounts_cooling_down")


@pytest.mark.parametrize(("mutation", "reason"), [
    ("UPDATE supervisor SET last_heartbeat_at=:old", "all_supervisors_offline"),
    ("UPDATE worker SET last_heartbeat_at=:old", "all_workers_offline"),
    ("UPDATE worker SET desired_state='disabled'", "no_healthy_workers"),
    ("UPDATE worker SET health='attention_required', "
     "attention_recorded_at=now(), "
     "operator_attention_code='worker_exited'", "no_healthy_workers"),
    ("UPDATE account_worker_assignment SET state='attention_required', "
     "operator_attention_code='linkedin_challenge'", "no_ready_assignments"),
])
def test_google_rejects_unavailable_shared_fleet_resources(
        app, mutation, reason):
    _fleet(app)
    _enable_google(app)
    app.execute(text(mutation), {"old": NOW - timedelta(minutes=5)})
    assert _eligible_google(app) == ([], reason)


def test_google_requires_an_advertised_capability_and_enabled_policy(app):
    _fleet(app)
    _enable_google(app)
    app.execute(text("""
        UPDATE worker SET capabilities='[]'::jsonb WHERE id='worker-a'
    """))
    assert _eligible_google(app) == ([], "no_google_capable_workers")

    app.execute(text("""
        UPDATE worker SET capabilities='["google_search"]'::jsonb
        WHERE id='worker-a'
    """))
    app.execute(text("""
        UPDATE worker_google_policy SET enabled=false
        WHERE worker_id='worker-a'
    """))
    assert _eligible_google(app) == ([], "no_google_capable_workers")


@pytest.mark.parametrize(("hourly", "daily", "then"), [
    (1, 10, NOW - timedelta(minutes=5)),
    (10, 1, NOW - timedelta(hours=2)),
])
def test_google_rejects_exhausted_hourly_or_daily_budget(
        app, hourly, daily, then):
    _fleet(app)
    _enable_google(app, hourly=hourly, daily=daily)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES ('rq-google-used', 'used', 'used', 1,
                'google_search', 'running', :then)
    """), {"then": then})
    attempt_id = app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, started_at,
             supervisor_id, worker_id, account_id, assignment_id, ended_at)
        VALUES ('rq-google-used', 1, :then, :then, 'worker-a', 'worker-a',
                'la-a', 'as-a', :then)
        RETURNING id
    """), {"then": then}).scalar_one()
    app.execute(text("""
        INSERT INTO worker_google_event
            (worker_id, request_id, attempt_id, occurred_at, google_host)
        VALUES ('worker-a', 'rq-google-used', :attempt, :then,
                'www.google.com')
    """), {"attempt": attempt_id, "then": then})

    assert _eligible_google(app) == ([], "google_budget_exhausted")


def test_an_open_operation_makes_a_google_slot_busy(app):
    _fleet(app)
    _enable_google(app)
    app.execute(text("""
        INSERT INTO worker_operation
            (id, worker_id, assignment_id, kind, status, requested_at)
        VALUES ('op-google', 'worker-a', 'as-a', 'verify', 'running', :now)
    """), {"now": NOW})
    assert _eligible_google(app) == ([], "all_workers_busy")


class BoundaryRng:
    def __init__(self, mark):
        self.mark = mark

    def uniform(self, start, end):
        assert start == 0
        assert end == 4
        return self.mark


@pytest.mark.parametrize(("mark", "account"), [
    (0, "la-one"), (0.999, "la-one"), (1, "la-two"), (3.999, "la-two"),
])
def test_weighted_choice_uses_remaining_headroom(mark, account):
    from career_lens.fleet.eligibility import Candidate, choose_candidate

    candidates = [
        Candidate("as-one", "la-one", "worker-one", "worker", 1),
        Candidate("as-two", "la-two", "worker-two", "worker", 3),
    ]
    assert choose_candidate(candidates, BoundaryRng(mark)).account_id == account


def test_weighted_choice_rejects_an_empty_sequence():
    from career_lens.fleet.eligibility import choose_candidate

    with pytest.raises(ValueError, match="no eligible candidates"):
        choose_candidate([], BoundaryRng(0))


def _candidate(account_id: str, remaining: int = 5):
    from career_lens.fleet.eligibility import Candidate

    return Candidate(
        assignment_id=f"as-{account_id}", account_id=account_id,
        worker_id=f"worker-{account_id}", supervisor_id="worker-a",
        remaining_attempts=remaining)


def test_a_retry_prefers_an_account_this_request_has_not_tried():
    from career_lens.fleet.eligibility import prefer_untried

    candidates = [_candidate("la-a"), _candidate("la-b")]

    assert prefer_untried(candidates, {"la-a"}) == [candidates[1]]


def test_a_retry_falls_back_to_a_tried_account_when_that_is_all_there_is():
    """A hard exclusion would strand the work on a small fleet, with attempts
    left unspent. Preference degrades; exclusion deadlocks."""
    from career_lens.fleet.eligibility import prefer_untried

    candidates = [_candidate("la-a")]

    assert prefer_untried(candidates, {"la-a"}) == candidates


def test_a_first_attempt_has_tried_nothing_and_keeps_every_candidate():
    from career_lens.fleet.eligibility import prefer_untried

    candidates = [_candidate("la-a"), _candidate("la-b")]

    assert prefer_untried(candidates, set()) == candidates


def test_preference_never_invents_or_drops_every_candidate():
    from career_lens.fleet.eligibility import prefer_untried

    candidates = [_candidate("la-a"), _candidate("la-b")]
    for tried in ({"la-a"}, {"la-a", "la-b"}, set(), {"la-z"}):
        preferred = prefer_untried(candidates, tried)
        assert set(preferred) <= set(candidates), tried
        assert preferred != [], tried


def test_google_weighted_choice_uses_search_headroom():
    from career_lens.fleet.eligibility import (
        GoogleCandidate, choose_google_candidate,
    )

    candidates = [
        GoogleCandidate("as-one", "la-one", "worker-one", "worker", 1),
        GoogleCandidate("as-two", "la-two", "worker-two", "worker", 3),
    ]
    assert choose_google_candidate(
        candidates, BoundaryRng(1)).worker_id == "worker-two"


def test_identity_preference_applies_to_google_candidates_too():
    """The rule is about the identity, not about what it is running, so a
    Google retry moves off a tried account exactly as a profile fetch does."""
    from career_lens.fleet.eligibility import GoogleCandidate, prefer_untried

    candidates = [
        GoogleCandidate("as-one", "la-one", "worker-one", "worker", 1),
        GoogleCandidate("as-two", "la-two", "worker-two", "worker", 3),
    ]

    assert prefer_untried(candidates, {"la-one"}) == [candidates[1]]
    assert prefer_untried(candidates, {"la-one", "la-two"}) == candidates


def test_a_worker_at_unknown_health_is_dispatchable(app):
    """A native worker rests at `unknown`; nothing sets it `healthy`."""
    _fleet(app)
    app.execute(text("UPDATE worker SET health='unknown' WHERE id='worker-a'"))

    candidates, reason = _eligible(app)

    assert reason is None
    assert [candidate.worker_id for candidate in candidates] == ["worker-a"]


def test_a_worker_at_unknown_health_is_dispatchable_for_google(app):
    _fleet(app)
    _enable_google(app)
    app.execute(text("UPDATE worker SET health='unknown' WHERE id='worker-a'"))

    candidates, reason = _eligible_google(app)

    assert reason is None
    assert [candidate.worker_id for candidate in candidates] == ["worker-a"]


def test_a_worker_needing_attention_is_still_refused(app):
    """The relaxation must not reach a worker that is genuinely held."""
    _fleet(app)
    app.execute(text("""
        UPDATE worker
        SET health='attention_required',
            operator_attention_code='worker_exited',
            attention_recorded_at=:now
        WHERE id='worker-a'
    """), {"now": NOW})

    candidates, reason = _eligible(app)

    assert candidates == []
    assert reason is not None


def test_the_public_allowlist_is_gone(app):
    """Every code a worker can now carry means the browser is suspect."""
    import career_lens.fleet.eligibility as eligibility

    assert not hasattr(eligibility, "PUBLIC_LINKEDIN_ATTENTION_CODES")


def test_any_held_worker_is_refused_for_public_capture(app):
    from career_lens.fleet.eligibility import eligible_public_workers

    _fleet(app)
    app.execute(text("""
        UPDATE worker
        SET health='attention_required',
            operator_attention_code='worker_operation_failed',
            attention_recorded_at=:now
        WHERE id='worker-a'
    """), {"now": NOW})

    assert eligible_public_workers(
        app, now=NOW, heartbeat_after=NOW - timedelta(seconds=30),
    ) == ([], "no_healthy_workers")


def _eligible_google_workers(connection):
    from career_lens.fleet.eligibility import eligible_google_workers

    return eligible_google_workers(
        connection, now=NOW, heartbeat_after=NOW - timedelta(seconds=30))


def test_a_native_worker_still_needs_its_advertised_google_capability(app):
    _fleet(app)
    _enable_google(app)
    candidates, _reason = _eligible_google_workers(app)
    assert [item.worker_id for item in candidates] == ["worker-a"]

    app.execute(text("""
        UPDATE worker SET capabilities='[]'::jsonb WHERE id='worker-a'
    """))
    assert _eligible_google_workers(app) == ([], "no_google_capable_workers")


