from datetime import UTC, datetime, timedelta
import random

import pytest
from sqlalchemy import text

from fleet_support import _anonymous_attempt, _page_session
from career_lens.capture.failures import CaptureFailed
from career_lens.fleet import retry
from career_lens.queue import tasks
from career_lens.store.db import transaction


def test_native_429_waits_without_quarantining_identity(app, app_url, monkeypatch):
    request, attempt = _anonymous_attempt(app_url, linkedin=True, kind="generic")

    def refuse(*args, **kwargs):
        failure = CaptureFailed("blocked", "rate_limited: http 429")
        failure.rate_limit = {"url": "https://example.com/", "retry_after": "60"}
        raise failure

    monkeypatch.setattr(tasks, "_work", refuse)
    tasks.run_assigned(attempt, url=app_url, session_factory=_page_session)
    row = app.execute(text("SELECT status, next_attempt_at, dispatch_wait_reason FROM scrape_request WHERE id=:id"), {"id": request}).one()
    assert row.status == "queued"
    assert row.dispatch_wait_reason == "site_rate_limit"
    assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=50)
    assert app.execute(text("SELECT count(*) FROM linkedin_account_event")).scalar_one() == 0


@pytest.mark.parametrize("header, seconds", [("60", 60), ("0", 0), ("Tue, 13 Oct 2026 10:01:00 GMT", 60)])
def test_retry_after_is_respected(header, seconds):
    from career_lens.fleet.site_cooldowns import retry_delay

    now = datetime(2026, 10, 13, 10, tzinfo=UTC)
    assert retry_delay(header, now=now, strikes=3, rng=random.Random(0)) == timedelta(seconds=seconds)


def test_missing_header_uses_increasing_bounded_backoff():
    from career_lens.fleet.site_cooldowns import retry_delay

    now = datetime.now(UTC)
    for strikes, low, high in [(1, 54, 66), (2, 270, 330), (3, 810, 900), (4, 810, 900)]:
        seconds = retry_delay("invalid", now=now, strikes=strikes, rng=random.Random(0)).total_seconds()
        assert low <= seconds <= high


def test_long_retry_after_stops_request_but_retains_cooldown(app, app_url):
    from career_lens.fleet.site_cooldowns import settle_rate_limit

    request, attempt = _anonymous_attempt(app_url, kind="generic")
    now = datetime.now(UTC)
    with transaction(app_url) as connection:
        decision = settle_rate_limit(connection, attempt, now=now, metadata={"url": "https://example.com/", "retry_after": "86400"})
        assert isinstance(decision, retry.Terminal)
    assert app.execute(text("SELECT not_before FROM site_cooldown")).scalar_one() == now + timedelta(days=1)


def test_cooldown_blocks_same_site_not_other_sites(app, app_url):
    from career_lens.fleet.dispatch import dispatch_one
    from career_lens.fleet.site_cooldowns import settle_rate_limit

    request, attempt = _anonymous_attempt(app_url, kind="generic")
    now = datetime.now(UTC)
    with transaction(app_url) as connection:
        connection.execute(text("UPDATE supervisor SET last_heartbeat_at=:now"), {"now": now})
        connection.execute(text("UPDATE worker SET last_heartbeat_at=:now"), {"now": now})
        url = connection.execute(text("SELECT url_normalised FROM scrape_request WHERE id=:id"), {"id": request}).scalar_one()
        settle_rate_limit(connection, attempt, now=now, metadata={"url": url, "retry_after": "3600"})
        connection.execute(text("UPDATE scrape_attempt SET ended_at=:now, cause='blocked' WHERE id=:id"), {"now": now, "id": attempt})
        connection.execute(text("UPDATE scrape_request SET status='queued' WHERE id=:id"), {"id": request})
        assert dispatch_one(connection, now=now, rng=random.Random(0)) is None
        assert connection.execute(text("SELECT dispatch_wait_reason FROM scrape_request WHERE id=:id"), {"id": request}).scalar_one() == "site_rate_limit"
        # A different site still uses this same node.
        connection.execute(text("INSERT INTO scrape_request (id, kind, url_requested, url_normalised, requested_at) VALUES ('rq-other', 'generic', 'https://elsewhere.test/', 'https://elsewhere.test/', :now)"), {"now": now})
        other = dispatch_one(connection, now=now, rng=random.Random(0))
        assert other is not None
        connection.execute(text("UPDATE scrape_attempt SET ended_at=:now WHERE id=:id"), {"now": now, "id": other})
        connection.execute(text("UPDATE scrape_request SET status='succeeded' WHERE id='rq-other'"))


def test_native_response_preserves_retry_after():
    from types import SimpleNamespace
    from career_lens.capture.failures import raise_for_rate_limit

    with pytest.raises(CaptureFailed) as error:
        raise_for_rate_limit(SimpleNamespace(status=429, url="https://example.org/", headers={"Retry-After": "120"}))
    assert error.value.rate_limit == {"url": "https://example.org/", "retry_after": "120"}


