"""Google query budgets are per browser worker and independent of LinkedIn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _slot(connection, *, hourly=3, daily=4):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-google', 'Google', 'active', 'instance-google',
                'host-google', 'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at, capabilities)
        VALUES ('worker-google', 'worker-google', 'google', 'enabled',
                'healthy', :now, '["google_search"]'::jsonb)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker_google_policy
            (worker_id, max_searches_hour, max_searches_24h)
        VALUES ('worker-google', :hourly, :daily)
    """), {"hourly": hourly, "daily": daily})


def _event(connection, *, at, suffix):
    request_id = f"rq-google-budget-{suffix}"
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:request, :query, :query, 1, 'google_search', 'running', :at)
    """), {"request": request_id, "query": suffix, "at": at})
    attempt_id = connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, started_at,
             supervisor_id, worker_id)
        VALUES (:request, 1, :at, :at, 'worker-google', 'worker-google')
        RETURNING id
    """), {"request": request_id, "at": at}).scalar_one()
    connection.execute(text("""
        INSERT INTO worker_google_event
            (worker_id, request_id, attempt_id, occurred_at, google_host)
        VALUES ('worker-google', :request, :attempt, :at, 'www.google.com')
    """), {"request": request_id, "attempt": attempt_id, "at": at})
    connection.execute(text("""
        UPDATE scrape_attempt SET ended_at=:at WHERE id=:attempt
    """), {"attempt": attempt_id, "at": at})
    return request_id, attempt_id


def test_headroom_counts_hour_and_day_windows_inclusively(app):
    from career_lens.google.budget import headroom

    _slot(app)
    for suffix, at in (
        ("hour-boundary", NOW - timedelta(hours=1)),
        ("day-only", NOW - timedelta(hours=2)),
        ("day-boundary", NOW - timedelta(hours=24)),
        ("too-old", NOW - timedelta(hours=24, microseconds=1)),
        ("future", NOW + timedelta(microseconds=1)),
    ):
        _event(app, at=at, suffix=suffix)

    value = headroom(app, "worker-google", now=NOW)

    assert value is not None
    assert (value.hour, value.day, value.remaining) == (2, 1, 1)


def test_disabled_or_missing_policy_has_no_headroom(app):
    from career_lens.google.budget import headroom

    _slot(app)
    app.execute(text("""
        UPDATE worker_google_policy SET enabled=false
        WHERE worker_id='worker-google'
    """))
    assert headroom(app, "worker-google", now=NOW) is None
    assert headroom(app, "missing-worker", now=NOW) is None


def test_charge_search_inserts_one_google_event_only(app_url, app):
    from career_lens.google.budget import charge_search

    _slot(app)
    request_id, attempt_id = _event(
        app, at=NOW - timedelta(hours=2), suffix="existing")

    charge_search(
        app_url, worker_id="worker-google", request_id=request_id,
        attempt_id=attempt_id, google_host="www.google.com", now=NOW)

    assert app.execute(text("""
        SELECT count(*) FROM worker_google_event
        WHERE worker_id='worker-google'
    """)).scalar_one() == 2
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event
    """)).scalar_one() == 0


@pytest.mark.parametrize("window", ["hour", "day"])
def test_charge_search_raises_before_insert_when_a_budget_is_exhausted(
        app_url, app, window):
    from career_lens.google.budget import SearchBudgetExhausted, charge_search

    _slot(app, hourly=1, daily=2)
    if window == "hour":
        request_id, attempt_id = _event(
            app, at=NOW - timedelta(minutes=5), suffix="hour")
    else:
        app.execute(text("""
            UPDATE worker_google_policy SET max_searches_hour=5
            WHERE worker_id='worker-google'
        """))
        _event(app, at=NOW - timedelta(hours=2), suffix="day-one")
        request_id, attempt_id = _event(
            app, at=NOW - timedelta(hours=3), suffix="day-two")

    before = app.execute(text(
        "SELECT count(*) FROM worker_google_event")).scalar_one()
    with pytest.raises(SearchBudgetExhausted, match="worker-google"):
        charge_search(
            app_url, worker_id="worker-google", request_id=request_id,
            attempt_id=attempt_id, google_host="www.google.com", now=NOW)
    after = app.execute(text(
        "SELECT count(*) FROM worker_google_event")).scalar_one()

    assert after == before


@pytest.mark.parametrize("host", ["", "   ", "https://google.com", "google.com/path",
                                  "not a host", "x" * 256])
def test_charge_search_rejects_an_invalid_google_hostname(
        app_url, app, host):
    from career_lens.google.budget import charge_search

    _slot(app)
    request_id, attempt_id = _event(
        app, at=NOW - timedelta(hours=2), suffix="host")

    with pytest.raises(ValueError, match="google_host"):
        charge_search(
            app_url, worker_id="worker-google", request_id=request_id,
            attempt_id=attempt_id, google_host=host, now=NOW)
