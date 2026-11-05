"""Registration and liveness writes for a supervisor and the workers it declares."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from .config import SupervisorConfig
from .attention import (
    CAPTURE_WORKER_LOST, MISSING_FROM_DECLARATION, WORKER_CONFIGURATION_INVALID,
    WORKER_EXITED, WORKER_OPERATION_ABANDONED, WORKER_OPERATION_FAILED,
    guidance,
)

HEALTH = {"unknown", "healthy", "attention_required", "quarantined"}
DETAIL_LIMIT = 1_000
WORKER_CAPABILITIES = ("google_search",)


class SupervisorError(RuntimeError):
    """A supervisor state write is invalid or not transactional."""


def _require_transaction(connection: Connection) -> None:
    isolation = connection.get_execution_options().get("isolation_level")
    if isolation == "AUTOCOMMIT" or not connection.in_transaction():
        raise SupervisorError(
            "supervisor writes require a real database transaction")


def _detail(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if len(cleaned) > DETAIL_LIMIT:
        raise SupervisorError(f"health detail must not exceed {DETAIL_LIMIT} characters")
    return cleaned or None


def stop_supervisor(connection: Connection, supervisor_id: str,
                    instance_id: str, *,
                now: datetime | None = None) -> bool:
    """Record that `instance_id` stopped on purpose. True if it was the one.

    Without this a stopped supervisor cannot be told from a crashed one: the
    console reads `desired_state` alongside the heartbeat age, and a supervisor
    that exited cleanly left `active` behind with a timestamp that merely got
    older. `register_supervisor` sets `active` again on the next start.

    Scoped to the instance because two processes race on a restart: the
    outgoing one's shutdown must not mark the instance that has already taken
    the registration over as stopped.

    `desired_state` is deliberately untouched -- that is the operator's intent,
    and a process exiting is not an instruction to keep it off.
    """
    _require_transaction(connection)
    return bool(connection.execute(text("""
        UPDATE supervisor SET stopped_at=:now
        WHERE id=:id AND instance_id=:instance
        RETURNING id
    """), {"id": supervisor_id, "instance": instance_id,
           "now": now or datetime.now(UTC)}).scalar_one_or_none())


def register_supervisor(connection: Connection, config: SupervisorConfig, *,
                        instance_id: str, hostname: str, version: str,
                        now: datetime | None = None) -> None:
    _require_transaction(connection)
    started_at = now or datetime.now(UTC)
    declared = [worker.id for worker in config.workers]
    # Dispatch and capture claim workers before their supervisor. A restart
    # updates both rows too, so claim every existing worker it can touch first
    # rather than letting the supervisor upsert establish the reverse order.
    connection.execute(text("""
        SELECT id FROM worker
        WHERE supervisor_id=:supervisor OR id = ANY(:declared)
        ORDER BY id
        FOR UPDATE
    """), {
        "supervisor": config.id,
        "declared": declared,
    }).scalars().all()
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES (:id, :label, 'active', :instance, :hostname, :version,
                :now, :now)
        ON CONFLICT (id) DO UPDATE SET
            label=EXCLUDED.label, instance_id=EXCLUDED.instance_id,
            hostname=EXCLUDED.hostname, software_version=EXCLUDED.software_version,
            started_at=EXCLUDED.started_at,
            last_heartbeat_at=EXCLUDED.last_heartbeat_at,
            stopped_at=NULL
    """), {
        "id": config.id, "label": config.label, "instance": instance_id,
        "hostname": hostname, "version": version, "now": started_at,
    })
    for worker in config.workers:
        registered = connection.execute(text("""
            INSERT INTO worker
                (id, supervisor_id, local_name, capabilities,
                 desired_state, health)
            VALUES (:id, :supervisor, :name, CAST(:capabilities AS jsonb),
                    :state, 'unknown')
            ON CONFLICT (id) DO UPDATE SET
                local_name=EXCLUDED.local_name,
                capabilities=EXCLUDED.capabilities,
                desired_state=EXCLUDED.desired_state,
                health_detail=CASE
                    WHEN worker.health_detail='missing_from_local_declaration'
                    THEN NULL ELSE worker.health_detail END,
                operator_attention_code=CASE
                    WHEN worker.health_detail='missing_from_local_declaration'
                    THEN NULL ELSE worker.operator_attention_code END,
                attention_recorded_at=CASE
                    WHEN worker.health_detail='missing_from_local_declaration'
                    THEN NULL ELSE worker.attention_recorded_at END,
                health=CASE
                    WHEN worker.health_detail='missing_from_local_declaration'
                    THEN 'unknown' ELSE worker.health END
            WHERE worker.supervisor_id=EXCLUDED.supervisor_id
            RETURNING supervisor_id
        """), {
            "id": worker.id, "supervisor": config.id, "name": worker.profile,
            "state": "enabled" if worker.enabled else "disabled",
            "capabilities": json.dumps(WORKER_CAPABILITIES),
        }).one_or_none()
        if registered is None:
            owner = connection.execute(text(
                "SELECT supervisor_id FROM worker WHERE id=:worker"),
                {"worker": worker.id}).scalar_one()
            raise SupervisorError(
                f"worker {worker.id} is already owned by supervisor {owner}")
        connection.execute(text("""
            INSERT INTO worker_google_policy (worker_id)
            VALUES (:worker) ON CONFLICT (worker_id) DO NOTHING
        """), {"worker": worker.id})
    connection.execute(text("""
        UPDATE worker
        SET desired_state='disabled', health='attention_required',
            health_detail='missing_from_local_declaration',
            operator_attention_code=:attention_code,
            attention_recorded_at=:now
        WHERE supervisor_id=:supervisor AND NOT (id = ANY(:declared))
    """), {
        "supervisor": config.id, "declared": declared, "now": started_at,
        "attention_code": MISSING_FROM_DECLARATION,
    })
    from ..queue.enqueue import enqueue_dispatch

    enqueue_dispatch(connection)


def heartbeat_supervisor(connection: Connection, supervisor_id: str,
                         instance_id: str, *,
                         live_worker_ids: Collection[str],
                         now: datetime | None = None) -> bool:
    _require_transaction(connection)
    # The database's clock unless a caller pins one, for the reason given in
    # `mark_worker_health`: this is the value a hold is compared against.
    heartbeat_at = now
    live = list(live_worker_ids)
    if live:
        # Fleet claims already hold worker when they reach supervisor. Wait at
        # that same boundary so neither side can hold one row while requesting
        # the other in reverse order.
        connection.execute(text("""
            SELECT worker.id
            FROM worker
            JOIN supervisor ON supervisor.id=worker.supervisor_id
            WHERE worker.supervisor_id=:supervisor
              AND worker.id = ANY(:live)
              AND supervisor.instance_id=:instance
            ORDER BY worker.id
            FOR UPDATE OF worker
        """), {
            "supervisor": supervisor_id,
            "instance": instance_id,
            "live": live,
        }).scalars().all()
    current = connection.execute(text("""
        UPDATE supervisor
        SET last_heartbeat_at=COALESCE(CAST(:now AS timestamptz), now())
        WHERE id=:supervisor AND instance_id=:instance
        RETURNING id
    """), {
        "supervisor": supervisor_id, "instance": instance_id, "now": heartbeat_at,
    }).one_or_none()
    if current is None:
        return False
    if live:
        connection.execute(text("""
            UPDATE worker
            SET last_heartbeat_at=COALESCE(CAST(:now AS timestamptz), now())
            WHERE supervisor_id=:supervisor AND id = ANY(:live)
              AND desired_state='enabled'
        """), {"supervisor": supervisor_id, "live": live, "now": heartbeat_at})
        # Ordered after the stamp above, and reading it: the beat just written
        # is the evidence that the runner is back. A worker that is not in
        # `live` was not confirmed alive, keeps its older stamp, and so stays
        # held -- which is the whole point of checking the two against
        # each other rather than trusting the restart itself.
        clear_recovered_worker_attention(connection, live)
    return True


def clear_worker_configuration_attention(
        connection: Connection, worker_ids: Collection[str]) -> int:
    """Release workers held for a host misconfiguration that is now fixed.

    Only this one reason is cleared. Every other attention code describes
    something a person still has to look at -- a browser signed out, a
    challenge, a runner that exited -- and a supervisor restart is no evidence
    at all about those. A configuration hold is different: the supervisor's own
    preflight is the same check that set it, so passing that check is the
    evidence, and without this a fixed host would stay out of dispatch until
    someone edited the database by hand.

    Health returns to `unknown` rather than `healthy`: nothing here has
    observed a browser, and the first heartbeat and operation say the rest.
    """
    _require_transaction(connection)
    workers = list(worker_ids)
    if not workers:
        return 0
    result = connection.execute(text("""
        UPDATE worker
        SET health='unknown', health_detail=NULL,
            operator_attention_code=NULL, attention_recorded_at=NULL
        WHERE id = ANY(:workers) AND health='attention_required'
          AND operator_attention_code=:code
    """), {"workers": workers, "code": WORKER_CONFIGURATION_INVALID})
    return result.rowcount


#: The holds that say nothing except "the process serving this worker is
#: gone" -- one because it vanished mid-capture, one because the runner
#: exited. A heartbeat is the exact negation of both, and of nothing else: a
#: live process is no evidence about a LinkedIn session, a declaration file or
#: an environment variable, so no other code is released this way.
RUNNER_LIVENESS_CODES = (CAPTURE_WORKER_LOST, WORKER_EXITED)


def clear_recovered_worker_attention(
        connection: Connection, worker_ids: Collection[str]) -> int:
    """Release workers whose runner has been seen alive since it was lost.

    `last_heartbeat_at` is only written for a worker the supervisor has just
    confirmed with `process.is_alive()`, so a heartbeat later than the hold is
    proof the runner was running after it was recorded as gone. That sorts the
    cases out without having to know which one it is: a runner that really
    died sends nothing and stays held, a job that merely stalled while the
    process was fine clears on the next beat, and a runner crashing in a loop
    re-raises the hold with a newer stamp each time and never clears.

    Health returns to `unknown` rather than `healthy`. Nothing here observed a
    browser; being dispatchable again is the whole claim.

    Nothing can be buried underneath the hold: every code a worker can carry
    is an observation of our own machinery, and no provider copies a session
    reason onto a worker any more. So this is a plain clear rather than a
    write-back of something hidden.
    """
    _require_transaction(connection)
    workers = list(worker_ids)
    if not workers:
        return 0
    result = connection.execute(text("""
        UPDATE worker
        SET health='unknown', health_detail=NULL,
            operator_attention_code=NULL, attention_recorded_at=NULL
        WHERE id = ANY(:workers) AND health='attention_required'
          AND operator_attention_code = ANY(:codes)
          AND last_heartbeat_at > attention_recorded_at
    """), {"workers": workers, "codes": list(RUNNER_LIVENESS_CODES)})
    return result.rowcount


#: The holds that a completed operation is evidence against. Driving a session
#: to completion proves this browser and this queue job work, and proves
#: nothing else -- not that a host's environment was fixed, not that a runner
#: which has since exited is back, not that a declaration file names this
#: worker again. Each of those has its own clearer above, keyed to its own
#: evidence.
OPERATION_FAILURE_CODES = (WORKER_OPERATION_FAILED, WORKER_OPERATION_ABANDONED)


def clear_completed_operation_attention(
        connection: Connection, worker_id: str) -> int:
    """Release a worker held by a failed operation, after one succeeds.

    Health returns to `unknown` rather than `healthy`, for the reason
    `clear_worker_configuration_attention` gives: being dispatchable again is
    the whole claim, and nothing native writes `healthy` any more.

    All four columns move together because
    `ck_worker_attention_recorded_with_code` requires the code and its
    timestamp to be null or non-null in step.

    `health='attention_required'` in the predicate is what keeps a
    `quarantined` worker held: an operator's hold on the whole browser is not
    answered by one session completing.
    """
    _require_transaction(connection)
    result = connection.execute(text("""
        UPDATE worker
        SET health='unknown', health_detail=NULL,
            operator_attention_code=NULL, attention_recorded_at=NULL
        WHERE id=:worker AND health='attention_required'
          AND operator_attention_code = ANY(:codes)
    """), {"worker": worker_id, "codes": list(OPERATION_FAILURE_CODES)})
    return result.rowcount


def mark_worker_health(connection: Connection, worker_id: str, health: str,
                       detail: str | None = None,
                       operator_attention_code: str | None = None,
                       now: datetime | None = None) -> None:
    _require_transaction(connection)
    if health not in HEALTH:
        raise SupervisorError(f"unknown worker health {health}")
    blocked = health in {"attention_required", "quarantined"}
    if blocked != (operator_attention_code is not None):
        raise SupervisorError(
            f"worker health {health} has invalid operator attention code")
    if operator_attention_code is not None:
        guidance(operator_attention_code)
    result = connection.execute(text("""
        UPDATE worker SET health=:health, health_detail=:detail,
            operator_attention_code=:attention_code,
            -- Cast because this use of the parameter is an IS NULL test,
            -- which on its own leaves the type undetermined.
            --
            -- The default is the database's clock, not this host's. Recovery
            -- compares this against `last_heartbeat_at`, and the two are
            -- written by different machines -- the control plane records a
            -- worker lost, the supervisor records the beat. One clock for
            -- both means the comparison never rests on them agreeing.
            attention_recorded_at=CASE
                WHEN CAST(:attention_code AS text) IS NULL THEN NULL
                ELSE COALESCE(CAST(:now AS timestamptz), now()) END
        WHERE id=:worker
    """), {
        "worker": worker_id, "health": health, "detail": _detail(detail),
        "attention_code": operator_attention_code, "now": now,
    })
    if result.rowcount != 1:
        raise SupervisorError(f"no worker {worker_id}")


def mark_worker_runner_exit(connection: Connection, worker_id: str,
                            detail: str) -> None:
    """Hold a dead runner and invalidate browser-session evidence it owned.

    A replacement process reopens the same persistent Chrome profile, but it
    is a new browser lifecycle. Clearing the timestamps makes the first later
    provider capture verify the actual session instead of trusting evidence
    observed by the process that died.
    """
    mark_worker_health(
        connection, worker_id, "attention_required", detail,
        operator_attention_code=WORKER_EXITED)
    # A ready assignment is required by schema to retain complete verification
    # evidence. Move its timestamp behind this supervisor lifecycle rather than
    # manufacturing a half-verified ready state; the ordinary first-capture
    # check will observe the session again.
    connection.execute(text("""
        UPDATE account_worker_assignment assignment
        SET verified_at=supervisor.started_at - interval '1 microsecond'
        FROM worker
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        WHERE assignment.worker_id=:worker AND assignment.ended_at IS NULL
          AND assignment.verified_at IS NOT NULL
          AND worker.id=assignment.worker_id
    """), {"worker": worker_id})
