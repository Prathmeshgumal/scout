"""Queue submission without importing the browser-only worker runtime."""

from __future__ import annotations

from procrastinate.tasks import configure_task
from sqlalchemy import Connection, text

from .app import SCHEMA, connector_url


def _driver_connection(connection: Connection):
    """Return the psycopg connection beneath SQLAlchemy's transaction."""
    return connection.connection.driver_connection


def enqueue_dispatch(connection: Connection) -> int:
    """Coalesce request bursts into one transactional control-plane kick."""
    connection.execute(text(f"SET LOCAL search_path TO {SCHEMA}, public"))
    # Checking first without serialising is racy: two API transactions can see
    # no kick, and the loser of Procrastinate's unique queueing-lock insert has
    # its whole transaction aborted. The transaction-scoped advisory lock
    # makes the check-and-defer sequence atomic without depending on a caught
    # uniqueness error.
    connection.execute(text(
        "SELECT pg_advisory_xact_lock(hashtextextended("
        "'career_lens:dispatch-pending', 0))"))
    existing = connection.execute(text(f"""
        SELECT id FROM {SCHEMA}.procrastinate_jobs
        WHERE queueing_lock='dispatch:pending' AND status='todo'
        ORDER BY id LIMIT 1
    """)).scalar_one_or_none()
    if existing is not None:
        return existing
    return configure_task(
        name="dispatch_pending_requests",
        # This path also runs inside browser/control workers, where ``app`` has
        # an open async pool. Deferral is deliberately performed on the
        # caller's synchronous SQLAlchemy transaction, so it must use the
        # dedicated synchronous manager.
        job_manager=sync_job_manager(),
        queue="control",
        lock="dispatch:pending",
        queueing_lock="dispatch:pending",
        connection=_driver_connection(connection),
    ).defer()


def enqueue_capture(connection: Connection, *, attempt_id: int,
                    worker_id: str) -> int:
    """Target an already-attributed attempt to its physical browser worker."""
    connection.execute(text(f"SET LOCAL search_path TO {SCHEMA}, public"))
    return configure_task(
        name="scrape_assigned",
        # Dispatch runs in the control worker while its async pool is open,
        # but this enqueue belongs to the caller's synchronous transaction.
        job_manager=sync_job_manager(),
        queue=f"capture:{worker_id}",
        lock=f"worker:{worker_id}",
        queueing_lock=f"attempt:{attempt_id}",
        connection=_driver_connection(connection),
    ).defer(attempt_id=attempt_id)


def enqueue_worker_operation(connection: Connection, *, operation_id: str,
                           worker_id: str) -> int:
    """Queue one durable browser-session command to its owning profile."""
    connection.execute(text(f"SET LOCAL search_path TO {SCHEMA}, public"))
    return configure_task(
        name="worker_operation",
        job_manager=sync_job_manager(),
        queue=f"capture:{worker_id}",
        lock=f"worker:{worker_id}",
        queueing_lock=f"operation:{operation_id}",
        connection=_driver_connection(connection),
    ).defer(operation_id=operation_id)


_sync_manager = None


def sync_job_manager():
    """A job manager that always defers synchronously, on our connection.

    `app.job_manager`'s connector decides how to run a deferral by asking
    `get_sync_connector()`, and that method returns the ASYNC connector as soon
    as a pool is open:

        def get_sync_connector(self):
            if self._async_pool:
                return self

    The API never opens one, so deferring there quietly gets a
    SyncPsycopgConnector and works. Inside the worker the pool IS open, so the
    same call returns the async connector, which then does
    `async with conn.cursor(...)` on the synchronous connection SQLAlchemy
    handed it and raises TypeError. Measured the hard way: a completed capture
    reached _finish and could not close.

    Building our own sync connector removes the branch. It is lazy because
    constructing one reads the database URL, which a test may not have set at
    import time.
    """
    global _sync_manager
    if _sync_manager is None:
        import procrastinate

        _sync_manager = procrastinate.App(
            connector=procrastinate.SyncPsycopgConnector(
                conninfo=connector_url(),
                kwargs={"options": f"-csearch_path={SCHEMA},public"},
            ),
        ).job_manager
    return _sync_manager
