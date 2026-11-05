"""Independent, per-worker Google query budgets."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

_HOSTNAME = re.compile(
    r"(?=.{1,253}\.?$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}"
    r"[A-Za-z0-9])?\.?"
)


@dataclass(frozen=True)
class GoogleHeadroom:
    """Searches left in each rolling window for one browser worker."""

    hour: int
    day: int

    @property
    def remaining(self) -> int:
        return min(self.hour, self.day)


class SearchBudgetExhausted(RuntimeError):
    """A worker has no independent Google search headroom."""


def _counts(connection: Connection, worker_id: str, *, now: datetime):
    return connection.execute(text("""
        SELECT
            count(*) FILTER (
                WHERE occurred_at >= :hour_start
                  AND occurred_at <= :now)::integer AS hour_used,
            count(*) FILTER (
                WHERE occurred_at >= :day_start
                  AND occurred_at <= :now)::integer AS day_used
        FROM worker_google_event
        WHERE worker_id=:worker
          AND occurred_at >= :day_start
          AND occurred_at <= :now
    """), {
        "worker": worker_id, "now": now,
        "hour_start": now - timedelta(hours=1),
        "day_start": now - timedelta(hours=24),
    }).one()


def headroom(connection: Connection, worker_id: str, *,
             now: datetime) -> GoogleHeadroom | None:
    """Return current rolling-window headroom for an enabled worker policy."""
    policy = connection.execute(text("""
        SELECT max_searches_hour, max_searches_24h
        FROM worker_google_policy
        WHERE worker_id=:worker AND enabled
    """), {"worker": worker_id}).one_or_none()
    if policy is None:
        return None
    used = _counts(connection, worker_id, now=now)
    return GoogleHeadroom(
        hour=max(0, policy.max_searches_hour - used.hour_used),
        day=max(0, policy.max_searches_24h - used.day_used),
    )


def _validated_host(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("google_host must be a hostname")
    host = value.strip().lower()
    if len(host) > 255 or _HOSTNAME.fullmatch(host) is None:
        raise ValueError("google_host must be a hostname of at most 255 characters")
    return host.rstrip(".")


def charge_search_in_transaction(
    connection: Connection,
    *,
    worker_id: str,
    request_id: str,
    attempt_id: int,
    google_host: str,
    now: datetime | None = None,
) -> float:
    """Spend one query and return its configured pre-submit delay."""
    occurred_at = now or datetime.now(UTC)
    host = _validated_host(google_host)
    policy = connection.execute(text("""
        SELECT enabled, max_searches_hour, max_searches_24h,
               min_interval_seconds
        FROM worker_google_policy
        WHERE worker_id=:worker
        FOR UPDATE
    """), {"worker": worker_id}).one_or_none()
    if policy is None or not policy.enabled:
        raise SearchBudgetExhausted(
            f"Google search budget is disabled for worker {worker_id}")

    used = _counts(connection, worker_id, now=occurred_at)
    if (used.hour_used >= policy.max_searches_hour
            or used.day_used >= policy.max_searches_24h):
        raise SearchBudgetExhausted(
            f"Google search budget is exhausted for worker {worker_id}")

    connection.execute(text("""
        INSERT INTO worker_google_event
            (worker_id, request_id, attempt_id, occurred_at, google_host)
        VALUES (:worker, :request, :attempt, :occurred_at, :google_host)
    """), {
        "worker": worker_id, "request": request_id,
        "attempt": attempt_id, "occurred_at": occurred_at,
        "google_host": host,
    })
    return float(policy.min_interval_seconds)


def charge_search(
        database_url: str | None, *, worker_id: str, request_id: str,
        attempt_id: int, google_host: str,
        now: datetime | None = None, wait: bool = False) -> None:
    """Atomically spend one query navigation from a worker's Google budget."""
    from ..store.db import transaction

    with transaction(database_url) as connection:
        delay = charge_search_in_transaction(
            connection,
            worker_id=worker_id,
            request_id=request_id,
            attempt_id=attempt_id,
            google_host=google_host,
            now=now,
        )
    if wait and delay > 0:
        time.sleep(delay)
