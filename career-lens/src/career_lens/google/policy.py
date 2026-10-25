"""A browser worker's Google search budget: what it may do, and what it has done.

Kept out of the HTTP layer because the budget is a fleet fact rather than a
request one, and both the operator API and the command line have to read and
change it. `projection` takes a row rather than a worker id so the supervisors
listing, which already selects these columns for every worker in one query, can
render the same shape without a second round trip per row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, text

__all__ = [
    "UnknownGooglePolicy", "projection", "read", "runtime_capable", "update",
]


class UnknownGooglePolicy(LookupError):
    """That worker has no Google policy row."""


def runtime_capable(capabilities) -> bool:
    """Whether this worker advertises the mechanics used for Google work."""
    return "google_search" in frozenset(capabilities or ())


def projection(row) -> dict:
    """Budget, usage and what is left, from a row carrying the google_ columns.

    `remaining` collapses to zero when the worker cannot search or is switched
    off, so a caller deciding whether to dispatch reads one number instead of
    re-deriving the rule.
    """
    capable = runtime_capable(row.capabilities)
    enabled = bool(row.google_enabled)
    used_hour = int(row.google_searches_hour)
    used_24h = int(row.google_searches_24h)
    remaining_hour = max(0, row.google_max_searches_hour - used_hour)
    remaining_24h = max(0, row.google_max_searches_24h - used_24h)
    if not capable or not enabled:
        remaining_hour = remaining_24h = 0
    return {
        "capable": capable,
        "policy": {
            "enabled": enabled,
            "max_searches_hour": row.google_max_searches_hour,
            "max_searches_24h": row.google_max_searches_24h,
            "min_interval_seconds": row.google_min_interval_seconds,
        },
        "usage": {
            "searches_hour": used_hour,
            "searches_24h": used_24h,
        },
        "remaining": {
            "searches_hour": remaining_hour,
            "searches_24h": remaining_24h,
            "dispatch": min(remaining_hour, remaining_24h),
        },
    }


def read(connection: Connection, worker_id: str, *,
         now: datetime | None = None):
    """The worker's policy row with its rolling hour and day usage counted on."""
    moment = now or datetime.now(UTC)
    row = connection.execute(text("""
        SELECT worker.capabilities,
               policy.enabled AS google_enabled,
               policy.max_searches_hour AS google_max_searches_hour,
               policy.max_searches_24h AS google_max_searches_24h,
               policy.min_interval_seconds AS google_min_interval_seconds,
               count(event.id) FILTER (
                   WHERE event.occurred_at >= :since_hour)::integer
                   AS google_searches_hour,
               count(event.id) FILTER (
                   WHERE event.occurred_at >= :since_24h)::integer
                   AS google_searches_24h
        FROM worker
        JOIN worker_google_policy policy ON policy.worker_id=worker.id
        LEFT JOIN worker_google_event event ON event.worker_id=worker.id
          AND event.occurred_at >= :since_24h
        WHERE worker.id=:worker
        GROUP BY worker.id, policy.worker_id
    """), {
        "worker": worker_id,
        "since_hour": moment - timedelta(hours=1),
        "since_24h": moment - timedelta(hours=24),
    }).one_or_none()
    if row is None:
        raise UnknownGooglePolicy(f"no Google policy for worker {worker_id}")
    return row


def update(connection: Connection, worker_id: str, values: dict) -> None:
    """Apply one or more budget changes, or refuse to touch an absent worker."""
    if not values:
        raise ValueError("at least one Google policy field is required")
    columns = {
        "enabled": "enabled",
        "max_searches_hour": "max_searches_hour",
        "max_searches_24h": "max_searches_24h",
        "min_interval_seconds": "min_interval_seconds",
    }
    unknown = sorted(set(values) - set(columns))
    if unknown:
        raise ValueError(f"unknown Google policy fields: {', '.join(unknown)}")
    sets = ", ".join(f"{columns[name]}=:{name}" for name in values)
    result = connection.execute(text(f"""
        UPDATE worker_google_policy SET {sets}
        WHERE worker_id=:worker
    """), {**values, "worker": worker_id})
    if result.rowcount != 1:
        raise UnknownGooglePolicy(f"no Google policy for worker {worker_id}")
