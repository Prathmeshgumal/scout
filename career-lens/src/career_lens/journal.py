"""A run's attempt row, and the journal of what that run did.

An attempt is one execution of a scrape request: which worker, which browser
profile, when it started, when it ended and why. The journal is every phase
that execution passed through, taken from the timing instrumentation
capture/trace.py already produces -- no new instrumentation, just a sink.

Two write disciplines, deliberately different:

  events   buffered, flushed from a background thread. A round trip to Postgres
           inside a timed phase would distort the very number the phase exists
           to measure, and an event that lands half a second late costs
           nothing.

  close    synchronous, on the caller's connection. `ended_at` and `cause` have
           to commit in the same transaction as the request's status: that
           pairing is what replaced scrape_request's
           `(status = 'failed') = (failure_kind IS NOT NULL)` CHECK when the
           cause moved onto the attempt.

Nothing here may fail a capture. A dropped batch costs a gap in the journal; a
raised exception would cost the run.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from .store.db import transaction

FLUSH_SECONDS = 0.5
FLUSH_BATCH = 50

_log = logging.getLogger("career_lens.journal")


def worker_name() -> str:
    """Which process this is. Recorded on the attempt so a stale one names the
    worker that abandoned it."""
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class Event:
    """One row of the journal, before it is written."""

    kind: str
    level: str
    phase: str
    detail: str
    at: datetime
    elapsed_ms: int
    duration_ms: int | None
    failed: bool
    capture_id: str | None = None


@dataclass(frozen=True)
class Intervention:
    """One thing that stood in the way, before it is written."""

    kind: str
    outcome: str
    host: str
    evidence: str
    at: datetime
    elapsed_ms: int


#: Request kinds that run on a browser without a LinkedIn identity. Their
#: attempts carry a supervisor and a worker and leave account and assignment
#: NULL, which is attribution rather than a gap in it.
ANONYMOUS_KINDS = ("generic", "google_search")


class Journal:
    """An open attempt and its buffered event stream."""

    def __init__(self, attempt_id: int, started: float,
                 url: str | None = None, *, batch: int = FLUSH_BATCH):
        self.attempt_id = attempt_id
        self.url = url
        self._started = started
        self._batch = batch
        self._pending: list[Event] = []
        self._interventions: list[Intervention] = []
        # What has already been noted this attempt. `is_blocking` is a
        # predicate several code paths ask independently about the same
        # challenge -- linkedin/company.py asks three times, referral.py twice
        # -- so without this the counts would report how many code paths cared
        # rather than what the page did.
        self._noted: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=f"journal-{attempt_id}")
        self._thread.start()

    @classmethod
    def start(cls, connection: Connection, attempt_id: int, *,
              url: str | None = None, batch: int = FLUSH_BATCH,
              now: datetime | None = None) -> "Journal":
        """Claim a dispatcher-created attempt and begin its event stream.

        Duplicate queue delivery is harmless: only the first start changes
        the attempt. LinkedIn work also consumes account budget in
        that transaction; Google and generic work deliberately do not. The caller owns
        the real transaction so request state and any LinkedIn budget event
        commit together.
        """
        started_at = now or datetime.now(UTC)
        row = connection.execute(text("""
            SELECT a.request_id, a.account_id, a.worker_id, a.started_at,
                   r.status, r.kind
            FROM scrape_attempt a
            JOIN scrape_request r ON r.id = a.request_id
            WHERE a.id = :attempt
            FOR UPDATE OF a, r
        """), {"attempt": attempt_id}).one_or_none()
        if row is None:
            raise RuntimeError(f"no scrape_attempt {attempt_id}")
        if row.started_at is None:
            if row.status != "assigned":
                raise RuntimeError(
                    f"attempt {attempt_id} cannot start request in {row.status}")
            if row.worker_id is None:
                raise RuntimeError(
                    f"attempt {attempt_id} has no fleet attribution")
            if row.account_id is None and row.kind not in ANONYMOUS_KINDS:
                raise RuntimeError(
                    f"attempt {attempt_id} has no fleet attribution")
            connection.execute(text("""
                UPDATE scrape_attempt SET started_at = :now
                WHERE id = :attempt AND started_at IS NULL
            """), {"attempt": attempt_id, "now": started_at})
            connection.execute(text("""
                UPDATE scrape_request SET status = 'running'
                WHERE id = :request AND status = 'assigned'
            """), {"request": row.request_id})
            # An account fact, so it needs an account. Google deliberately
            # spends its own per-worker budget instead, and an anonymous
            # public capture spends nobody's identity at all.
            if row.kind not in {"google_search", "generic"} and row.account_id is not None:
                connection.execute(text("""
                    INSERT INTO linkedin_account_event
                        (account_id, worker_id, attempt_id, kind, occurred_at,
                         detail)
                    VALUES (:account, :worker, :attempt, 'attempt_started', :now,
                            '{}'::jsonb)
                    ON CONFLICT (attempt_id, kind)
                        WHERE attempt_id IS NOT NULL
                          AND kind = 'attempt_started'
                    DO NOTHING
                """), {
                    "account": row.account_id, "worker": row.worker_id,
                    "attempt": attempt_id, "now": started_at,
                })
        return cls(attempt_id, time.monotonic(), url, batch=batch)

    @classmethod
    def open(cls, url: str | None, *, request_id: str, browser_profile: str,
             batch: int = FLUSH_BATCH) -> "Journal":
        """Insert the attempt and start buffering.

        `attempt_no` is derived inside the INSERT rather than read first: there
        is one worker per browser profile by physical constraint (Chrome locks
        user_data_dir), so there is nothing to race with, and the unique index
        is there for the case where that stops being true.
        """
        with transaction(url) as connection:
            attempt_id = connection.execute(text("""
                INSERT INTO scrape_attempt
                    (request_id, attempt_no, worker, browser_profile, started_at)
                SELECT :r, COALESCE(MAX(attempt_no), 0) + 1, :w, :p, :t
                FROM scrape_attempt WHERE request_id = :r
                RETURNING id
            """), {"r": request_id, "w": worker_name(),
                   "p": browser_profile, "t": datetime.now(UTC)}).scalar_one()
        return cls(attempt_id, time.monotonic(), url, batch=batch)

    def sink(self, *, kind: str, phase: str, detail: str,
             seconds: float | None, failed: bool, level: str,
             elapsed: float) -> None:
        """The callable handed to `trace.activate`."""
        self._append(Event(
            kind=kind, level=level, phase=phase, detail=detail or "",
            at=datetime.now(UTC), elapsed_ms=int(elapsed * 1000),
            duration_ms=None if seconds is None else int(seconds * 1000),
            failed=failed))

    def capture_stored(self, capture_id: str, section: str) -> None:
        """The event the UI appends capture cards from.

        Emitted after the capture's transaction has committed: the id does not
        exist before then, and the foreign key would refuse it.
        """
        self._append(Event(
            kind="note", level="info", phase="capture stored", detail=section,
            at=datetime.now(UTC), elapsed_ms=self._elapsed_ms(),
            duration_ms=None, failed=False, capture_id=capture_id))

    def intervention(self, *, kind: str, outcome: str, host: str,
                     evidence: str = "") -> None:
        """Note an overlay dismissed or a challenge met.

        The callable handed to `pressure.activate`. Deduplicated on
        (kind, host, evidence), keeping the first occurrence and its timing:
        two genuinely separate CAPTCHAs on one host with identical evidence
        collapse into one, which is the lesser error.
        """
        signature = (kind, host, evidence)
        with self._lock:
            if signature in self._noted:
                return
            self._noted.add(signature)
            self._interventions.append(Intervention(
                kind=kind, outcome=outcome, host=host, evidence=evidence,
                at=datetime.now(UTC), elapsed_ms=self._elapsed_ms()))

    def close(self, connection: Connection, *, cause: str | None = None,
              detail: str | None = None) -> None:
        """Stop buffering and end the attempt on the given connection."""
        self._stop.set()
        self._thread.join(timeout=5)
        self._flush(connection)
        connection.execute(text("""
            UPDATE scrape_attempt
            SET ended_at = :t, cause = :cause, cause_detail = :detail
            WHERE id = :a
        """), {"a": self.attempt_id, "t": datetime.now(UTC),
               "cause": cause, "detail": detail})

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def _append(self, event: Event) -> None:
        with self._lock:
            self._pending.append(event)
            full = len(self._pending) >= self._batch
        if full:
            self._flush()

    def _run(self) -> None:
        while not self._stop.wait(FLUSH_SECONDS):
            self._flush()

    def _flush(self, connection: Connection | None = None) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
            noted, self._interventions = self._interventions, []
        if not batch and not noted:
            return

        def insert(target: Connection) -> None:
            if batch:
                target.execute(text("""
                    INSERT INTO attempt_event
                        (attempt_id, at, elapsed_ms, duration_ms, kind, level,
                         phase, detail, failed, capture_id)
                    VALUES (:a, :at, :elapsed, :duration, :kind, :level,
                            :phase, :detail, :failed, :capture)
                """), [{
                    "a": self.attempt_id, "at": event.at,
                    "elapsed": event.elapsed_ms, "duration": event.duration_ms,
                    "kind": event.kind, "level": event.level,
                    "phase": event.phase, "detail": event.detail,
                    "failed": event.failed, "capture": event.capture_id,
                } for event in batch])
            if noted:
                target.execute(text("""
                    INSERT INTO attempt_intervention
                        (attempt_id, at, elapsed_ms, kind, outcome, host,
                         evidence)
                    VALUES (:a, :at, :elapsed, :kind, :outcome, :host,
                            :evidence)
                """), [{
                    "a": self.attempt_id, "at": one.at,
                    "elapsed": one.elapsed_ms, "kind": one.kind,
                    "outcome": one.outcome, "host": one.host,
                    "evidence": one.evidence or None,
                } for one in noted])

        try:
            if connection is None:
                with transaction(self.url) as target:
                    insert(target)
            else:
                # A savepoint preserves the journal's central guarantee: even
                # a malformed event cannot poison the attempt transaction.
                with connection.begin_nested():
                    insert(connection)
        except Exception as exc:  # noqa: BLE001 -- see the module docstring
            # Never into the capture. A gap in the journal is a cost worth
            # paying; a lost run is not.
            _log.warning("career_lens: journal dropped %d events and %d "
                         "interventions: %s", len(batch), len(noted), exc)


STALE_SECONDS = 30


def _procrastinate_stalled(url: str | None):
    """Ask Procrastinate which jobs a vanished worker left behind.

    Detection is the library's, not ours: it keeps worker heartbeats in
    `procrastinate_workers` and answers this from them, which is part of why it
    was chosen (v2 D9 -- "not writing claim, heartbeat and stale-recovery logic
    again").

    Its job manager is asynchronous. `get_stalled_jobs` is a coroutine
    function, so this opens the app's async connector for the length of the
    query rather than calling it from synchronous code -- calling it directly
    returns a coroutine object and fails with "'coroutine' object is not
    iterable".
    """
    import asyncio

    from .queue.app import make_app

    async def ask():
        queue_app = make_app(url)
        async with queue_app.open_async():
            return list(await queue_app.job_manager.get_stalled_jobs(
                seconds_since_heartbeat=STALE_SECONDS))

    return asyncio.run(ask())


def _procrastinate_finish(url: str | None, job) -> None:
    """Mark the abandoned job failed so it is never claimed again."""
    import asyncio

    from procrastinate import jobs as procrastinate_jobs

    from .queue.app import make_app

    async def finish():
        queue_app = make_app(url)
        async with queue_app.open_async():
            await queue_app.job_manager.finish_job(
                job, status=procrastinate_jobs.Status.FAILED, delete_job=False)

    asyncio.run(finish())


def sweep_stalled(url: str | None = None, *, stalled=None,
                  finish=None) -> list[str]:
    """Close attempts a vanished worker left open. Returns the request ids.

    Runs from the control-plane reconciliation task. Legacy single-profile
    workers also call it at startup, which remains safe because already closed
    attempts are ignored and Procrastinate's stalled-job query is authoritative.

    Do NOT call `prune_stalled_workers` before this. `procrastinate_jobs.supervisor_id`
    is ON DELETE SET NULL, so pruning the worker rows first can erase the link
    the heartbeat-based detection depends on.

    `stalled` and `finish` are injected so the sweep can be tested without a
    worker that really died.
    """
    stalled = stalled or (lambda: _procrastinate_stalled(url))
    finish = finish or (lambda job: _procrastinate_finish(url, job))

    closed: list[str] = []
    from .fleet.attention import CAPTURE_WORKER_LOST, WORKER_OPERATION_ABANDONED
    from .fleet.recovery import fail_worker_operation

    for job in stalled():
        arguments = getattr(job, "task_kwargs", None) or {}
        request_id = arguments.get("request_id")
        attempt_id = arguments.get("attempt_id")
        operation_id = arguments.get("operation_id")
        if operation_id:
            with transaction(url) as connection:
                fail_worker_operation(
                    connection, operation_id,
                    code=WORKER_OPERATION_ABANDONED,
                    detail="the worker stopped during this operation",
                    now=datetime.now(UTC),
                )
            finish(job)
            continue
        if not request_id and attempt_id is None:
            continue
        with transaction(url) as connection:
            # Only attempts still open. A job can stall after its attempt
            # closed -- the worker died between finishing the work and telling
            # the queue -- and overwriting a real cause with `worker_lost`
            # would lose the reason the run actually failed.
            worker_id = None
            if attempt_id is not None:
                attempt = connection.execute(text("""
                    SELECT attempt.request_id, attempt.worker_id,
                           attempt.attempt_no, request.max_attempts,
                           request.status AS request_status,
                           request.cancel_requested_at IS NOT NULL
                               AS cancel_requested
                    FROM scrape_attempt attempt
                    JOIN scrape_request request
                      ON request.id=attempt.request_id
                    WHERE attempt.id=:attempt
                """), {"attempt": attempt_id}).one_or_none()
                request_id = attempt.request_id if attempt else None
                worker_id = attempt.worker_id if attempt else None
            else:
                attempt = connection.execute(text("""
                    SELECT attempt.request_id, attempt.worker_id,
                           attempt.attempt_no, request.max_attempts,
                           request.status AS request_status,
                           request.cancel_requested_at IS NOT NULL
                               AS cancel_requested
                    FROM scrape_attempt attempt
                    JOIN scrape_request request
                      ON request.id=attempt.request_id
                    WHERE attempt.request_id=:request
                      AND attempt.ended_at IS NULL
                    ORDER BY attempt.attempt_no DESC
                    LIMIT 1
                """), {"request": request_id}).one_or_none()
                worker_id = attempt.worker_id if attempt else None
            clause = " AND id=:attempt" if attempt_id is not None else ""
            updated = connection.execute(text(f"""
                UPDATE scrape_attempt
                SET ended_at = :t, cause = 'worker_lost',
                    cause_detail = 'the worker stopped without closing this attempt'
                WHERE request_id = :r AND ended_at IS NULL{clause}
            """), {
                "r": request_id, "attempt": attempt_id,
                "t": datetime.now(UTC),
            }).rowcount
            if updated:
                from .fleet import retry

                decision = retry.decide(
                    cause="worker_lost",
                    attempt_no=retry.attempts_spent(connection, request_id),
                    max_attempts=attempt.max_attempts,
                    cancel_requested=attempt.cancel_requested,
                )
                if isinstance(decision, retry.Retry):
                    retry.requeue(
                        connection, request_id, now=datetime.now(UTC),
                        delay=decision.delay, reason="worker_lost")
                elif attempt.request_status != "cancelled":
                    retry.fail(
                        connection, request_id,
                        status=retry.terminal_status(
                            "worker_lost", attempt.cancel_requested))
                if worker_id is not None:
                    connection.execute(text("""
                        UPDATE worker
                        SET health='attention_required',
                            health_detail='the worker stopped during capture',
                            operator_attention_code=:attention_code,
                            -- The database's clock: the supervisor writes the
                            -- heartbeat this is compared against, and it is
                            -- not always on this machine.
                            attention_recorded_at=now()
                        WHERE id=:worker
                    """), {
                        "worker": worker_id,
                        "attention_code": CAPTURE_WORKER_LOST,
                    })
        if updated:
            closed.append(request_id)
        finish(job)
    return closed
