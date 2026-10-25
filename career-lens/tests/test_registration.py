"""Worker registration survives restarts without stale heartbeat writes."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.fleet.config import WorkerConfig, SupervisorConfig

T0 = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=1)
CONFIG = SupervisorConfig(
    "worker-a", "Research laptop",
    (WorkerConfig("worker-a", "research-a"),
     WorkerConfig("worker-b", "research-b", enabled=False)),
)


def test_registration_upserts_stable_worker_and_declared_slots(app_url, app):
    from career_lens.fleet.registration import register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                        hostname="host", version="0.1", now=T0)
    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-two",
                        hostname="host", version="0.2", now=T1)

    worker = app.execute(text("SELECT * FROM supervisor")).one()
    workers = app.execute(text("""
        SELECT id, local_name, desired_state, capabilities
        FROM worker ORDER BY id
    """)).all()
    assert worker.id == "worker-a" and worker.instance_id == "boot-two"
    assert worker.software_version == "0.2" and worker.started_at == T1
    assert [(r.id, r.local_name, r.desired_state, r.capabilities)
            for r in workers] == [
        ("worker-a", "research-a", "enabled", ["google_search"]),
        ("worker-b", "research-b", "disabled", ["google_search"]),
    ]
    policies = app.execute(text("""
        SELECT worker_id, enabled, max_searches_hour, max_searches_24h
        FROM worker_google_policy ORDER BY worker_id
    """)).all()
    assert [tuple(row) for row in policies] == [
        ("worker-a", True, 120, 1000),
        ("worker-b", True, 120, 1000),
    ]


def test_registration_restores_capability_without_overwriting_policy(
        app_url, app):
    from career_lens.fleet.registration import register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="old", hostname="h",
                        version="old", now=T0)
    app.execute(text("""
        UPDATE worker SET capabilities='[]'::jsonb WHERE id='worker-a'
    """))
    app.execute(text("""
        UPDATE worker_google_policy
        SET max_searches_hour=7, max_searches_24h=19 WHERE worker_id='worker-a'
    """))

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="current", hostname="h",
                        version="current", now=T1)

    row = app.execute(text("""
        SELECT s.capabilities, p.max_searches_hour, p.max_searches_24h
        FROM worker s
        JOIN worker_google_policy p ON p.worker_id=s.id
        WHERE s.id='worker-a'
    """)).one()
    assert row._mapping == {
        "capabilities": ["google_search"],
        "max_searches_hour": 7,
        "max_searches_24h": 19,
    }


def test_registration_refuses_a_slot_owned_by_another_worker(app_url, app):
    from career_lens.fleet.registration import SupervisorError, register_supervisor
    from career_lens.store.db import transaction

    owner = SupervisorConfig(
        "worker-a", "First laptop", (WorkerConfig("shared-worker", "research"),))
    contender = SupervisorConfig(
        "worker-b", "Second laptop", (WorkerConfig("shared-worker", "research"),))
    with transaction(app_url) as connection:
        register_supervisor(connection, owner, instance_id="first", hostname="a",
                        version="v", now=T0)

    with pytest.raises(SupervisorError, match="shared-worker.*worker-a"):
        with transaction(app_url) as connection:
            register_supervisor(
                connection, contender, instance_id="second", hostname="b",
                version="v", now=T1)

    row = app.execute(text("""
        SELECT supervisor_id, local_name FROM worker WHERE id='shared-worker'
    """)).one()
    assert row._mapping == {"supervisor_id": "worker-a", "local_name": "research"}
    assert app.execute(text("""
        SELECT count(*) FROM supervisor WHERE id='worker-b'
    """)).scalar_one() == 0


def test_missing_slot_is_disabled_not_deleted(app_url, app):
    from career_lens.fleet.registration import register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="one", hostname="h",
                        version="v", now=T0)
    reduced = SupervisorConfig("worker-a", "Research laptop", CONFIG.workers[:1])
    with transaction(app_url) as connection:
        register_supervisor(connection, reduced, instance_id="two", hostname="h",
                        version="v", now=T1)
    row = app.execute(text("""
        SELECT desired_state, health, health_detail, operator_attention_code
        FROM worker WHERE id='worker-b'
    """)).one()
    assert row._mapping == {
        "desired_state": "disabled", "health": "attention_required",
        "health_detail": "missing_from_local_declaration",
        "operator_attention_code": "missing_from_declaration",
    }


def test_old_instance_cannot_overwrite_new_heartbeat(app_url, app):
    from career_lens.fleet.registration import heartbeat_supervisor, register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="new", hostname="h",
                        version="v", now=T0)
    with transaction(app_url) as connection:
        assert heartbeat_supervisor(
            connection, "worker-a", "old", live_worker_ids={"worker-a"},
            now=T1) is False
    assert app.execute(text("""
        SELECT last_heartbeat_at FROM supervisor WHERE id='worker-a'
    """)).scalar_one() == T0


def test_current_instance_heartbeats_only_live_slots(app_url, app):
    from career_lens.fleet.registration import heartbeat_supervisor, register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot", hostname="h",
                        version="v", now=T0)
    with transaction(app_url) as connection:
        assert heartbeat_supervisor(
            connection, "worker-a", "boot", live_worker_ids={"worker-a"},
            now=T1) is True
    rows = app.execute(text("""
        SELECT id, last_heartbeat_at FROM worker ORDER BY id
    """)).all()
    assert rows[0].last_heartbeat_at == T1
    assert rows[1].last_heartbeat_at is None


def test_heartbeat_waits_for_workers_before_locking_supervisor(app_url, app):
    """A capture holding a worker can still finish locking its supervisor."""
    from career_lens.fleet.registration import heartbeat_supervisor, register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot", hostname="h",
                            version="v", now=T0)

    heartbeat_pid: list[int] = []

    def heartbeat() -> bool:
        with transaction(app_url) as connection:
            heartbeat_pid.append(
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            return heartbeat_supervisor(
                connection, "worker-a", "boot",
                live_worker_ids={"worker-a"}, now=T1,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(app_url) as capture:
            capture.execute(text(
                "SELECT id FROM worker WHERE id='worker-a' FOR UPDATE"
            ))
            pending = pool.submit(heartbeat)
            for _ in range(100):
                if heartbeat_pid and app.execute(text("""
                    SELECT wait_event_type
                    FROM pg_stat_activity
                    WHERE pid=:pid
                """), {"pid": heartbeat_pid[0]}).scalar_one() == "Lock":
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("heartbeat did not wait for the worker lock")

            capture.execute(text(
                "SELECT id FROM supervisor WHERE id='worker-a' FOR UPDATE"
            ))

        assert pending.result(timeout=2) is True


def test_restart_waits_for_workers_before_locking_supervisor(app_url, app):
    """A restart cannot invert capture's worker-to-supervisor lock order."""
    from career_lens.fleet.registration import register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="h", version="v", now=T0)

    restart_pid: list[int] = []

    def restart() -> None:
        with transaction(app_url) as connection:
            restart_pid.append(
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
            register_supervisor(
                connection, CONFIG, instance_id="boot-two",
                hostname="h", version="v", now=T1,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction(app_url) as capture:
            capture.execute(text(
                "SELECT id FROM worker WHERE id='worker-a' FOR UPDATE"
            ))
            pending = pool.submit(restart)
            for _ in range(100):
                if restart_pid and app.execute(text("""
                    SELECT wait_event_type
                    FROM pg_stat_activity
                    WHERE pid=:pid
                """), {"pid": restart_pid[0]}).scalar_one() == "Lock":
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("restart did not wait for the worker lock")

            capture.execute(text(
                "SELECT id FROM supervisor WHERE id='worker-a' FOR UPDATE"
            ))

        assert pending.result(timeout=2) is None


def test_a_worker_that_stopped_is_told_apart_from_one_that_crashed(
        app_url, app):
    """`desired_state` stayed 'active' and the heartbeat simply aged, so the
    console showed a worker that was still meant to be running."""
    from career_lens.fleet.registration import register_supervisor, stop_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="wi-1",
                        hostname="host", version="1.0")
    with transaction(app_url) as connection:
        stop_supervisor(connection, CONFIG.id, "wi-1")

    stopped = app.execute(text(
        "SELECT stopped_at FROM supervisor WHERE id=:i"),
        {"i": CONFIG.id}).scalar_one()
    assert stopped is not None
    # The operator's own intent is not what a process exiting gets to change.
    state = app.execute(text(
        "SELECT desired_state FROM supervisor WHERE id=:i"),
        {"i": CONFIG.id}).scalar_one()
    assert state == "active"


def test_starting_again_clears_the_stop(app_url, app):
    from career_lens.fleet.registration import register_supervisor, stop_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="wi-1",
                        hostname="host", version="1.0")
    with transaction(app_url) as connection:
        stop_supervisor(connection, CONFIG.id, "wi-1")
    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="wi-2",
                        hostname="host", version="1.0")

    stopped = app.execute(text(
        "SELECT stopped_at FROM supervisor WHERE id=:i"),
        {"i": CONFIG.id}).scalar_one()
    assert stopped is None


def test_a_replaced_worker_does_not_deregister_its_successor(app_url, app):
    """Two processes race on restart: the old one's shutdown must not disable
    the instance that has already taken over."""
    from career_lens.fleet.registration import register_supervisor, stop_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="wi-old",
                        hostname="host", version="1.0")
    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="wi-new",
                        hostname="host", version="1.0")
    with transaction(app_url) as connection:
        stop_supervisor(connection, CONFIG.id, "wi-old")

    stopped = app.execute(text(
        "SELECT stopped_at FROM supervisor WHERE id=:i"),
        {"i": CONFIG.id}).scalar_one()
    assert stopped is None


def test_flagging_a_worker_records_when_the_flag_was_raised(app_url, app):
    """Nothing can tell a stale hold from a fresh one without this."""
    from career_lens.fleet.registration import (
        mark_worker_health, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
        mark_worker_health(
            connection, "worker-a", "attention_required",
            "worker runner exited with code 1",
            operator_attention_code="worker_exited", now=T1)

    recorded = app.execute(text(
        "SELECT attention_recorded_at FROM worker WHERE id='worker-a'"
    )).scalar_one()
    assert recorded == T1


def _hold(connection, worker_id: str, code: str, *, raised: datetime,
          beat: datetime | None) -> None:
    """Put one worker on hold, and say when its runner was last seen alive."""
    from career_lens.fleet.registration import mark_worker_health

    mark_worker_health(connection, worker_id, "attention_required",
                       f"held by {code}", operator_attention_code=code,
                       now=raised)
    connection.execute(text(
        "UPDATE worker SET last_heartbeat_at=:beat WHERE id=:worker"
    ), {"worker": worker_id, "beat": beat})


def test_a_worker_seen_alive_after_it_was_lost_stops_being_held(app_url, app):
    """The runner is demonstrably back, so `it stopped` is no longer true."""
    from career_lens.fleet.registration import (
        clear_recovered_worker_attention, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
        _hold(connection, "worker-a", "capture_worker_lost",
              raised=T0, beat=T1)

        assert clear_recovered_worker_attention(connection, ["worker-a"]) == 1

    row = app.execute(text(
        "SELECT health, health_detail, operator_attention_code,"
        "       attention_recorded_at"
        " FROM worker WHERE id='worker-a'")).one()
    # `unknown`, not `healthy`: nothing here has observed a browser.
    assert row.health == "unknown"
    assert row.health_detail is None
    assert row.operator_attention_code is None
    assert row.attention_recorded_at is None


def test_a_hold_raised_after_the_last_heartbeat_survives(app_url, app):
    """A runner that died and stayed dead sends nothing after being flagged."""
    from career_lens.fleet.registration import (
        clear_recovered_worker_attention, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
        _hold(connection, "worker-a", "worker_exited", raised=T1, beat=T0)

        assert clear_recovered_worker_attention(connection, ["worker-a"]) == 0

    row = app.execute(text(
        "SELECT health, operator_attention_code FROM worker WHERE id='worker-a'"
    )).one()
    assert row.health == "attention_required"
    assert row.operator_attention_code == "worker_exited"


def test_a_worker_that_never_heartbeat_stays_held(app_url, app):
    """An unknown time cannot be shown to precede anything."""
    from career_lens.fleet.registration import (
        clear_recovered_worker_attention, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
        _hold(connection, "worker-a", "capture_worker_lost",
              raised=T0, beat=None)

        assert clear_recovered_worker_attention(connection, ["worker-a"]) == 0

    assert app.execute(text(
        "SELECT operator_attention_code FROM worker WHERE id='worker-a'"
    )).scalar_one() == "capture_worker_lost"


@pytest.mark.parametrize("code", [
    "linkedin_signed_out",
    "linkedin_challenge",
    "worker_operation_failed",
    "missing_from_declaration",
    "worker_configuration_invalid",
])
def test_only_a_hold_about_the_runner_dying_is_released_by_liveness(
        app_url, app, code):
    """A live process is no evidence about a session, a file or an env var."""
    from career_lens.fleet.registration import (
        clear_recovered_worker_attention, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
        _hold(connection, "worker-a", code, raised=T0, beat=T1)

        assert clear_recovered_worker_attention(connection, ["worker-a"]) == 0

    assert app.execute(text(
        "SELECT operator_attention_code FROM worker WHERE id='worker-a'"
    )).scalar_one() == code


def _linkedin_hold(connection, worker_id: str, code: str) -> None:
    """An open LinkedIn assignment on `worker_id` that still needs a human."""
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, operator_attention_code,
             max_attempts_24h, min_interval_seconds, created_at, updated_at)
        VALUES ('la-a', 'Research', 'research@example.com',
                'attention_required', :code, 20, 60, :now, :now)
    """), {"code": code, "now": T0})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, session_mode, state, state_detail,
             operator_attention_code, assigned_at)
        VALUES ('aw-a', 'la-a', :worker, 'managed_login',
                'attention_required', 'LinkedIn session is signed out',
                :code, :now)
    """), {"worker": worker_id, "code": code, "now": T0})


def test_a_hold_without_a_time_is_refused_by_the_database(app_url, app):
    """Every writer must stamp it, or recovery silently never fires."""
    from sqlalchemy.exc import IntegrityError

    from career_lens.fleet.registration import register_supervisor
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)

    with pytest.raises(IntegrityError, match="attention_recorded"):
        with transaction(app_url) as connection:
            connection.execute(text("""
                UPDATE worker
                SET health='attention_required', health_detail='held',
                    operator_attention_code='worker_exited'
                WHERE id='worker-a'
            """))


class _SkewedClock:
    """A host whose clock is wrong, which is the case the fix is for."""

    @staticmethod
    def now(tz=None):
        return datetime(2020, 1, 1, tzinfo=UTC)


def test_the_hold_time_comes_from_the_database_not_the_writing_host(
        app_url, app, monkeypatch):
    """Two hosts write the two timestamps that recovery compares."""
    import career_lens.fleet.registration as registration
    from career_lens.fleet.registration import (
        mark_worker_health, register_supervisor,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        register_supervisor(connection, CONFIG, instance_id="boot-one",
                            hostname="host", version="0.1", now=T0)
    monkeypatch.setattr(registration, "datetime", _SkewedClock)
    with transaction(app_url) as connection:
        mark_worker_health(
            connection, "worker-a", "attention_required", "held",
            operator_attention_code="worker_exited")

    recorded, database_now = app.execute(text(
        "SELECT attention_recorded_at, now() FROM worker WHERE id='worker-a'"
    )).one()
    assert recorded.year != 2020
    assert abs((recorded - database_now).total_seconds()) < 60


def _held_worker(connection, code: str) -> None:
    """One registered worker, held by `code`."""
    from career_lens.fleet.registration import register_supervisor

    register_supervisor(connection, CONFIG, instance_id="boot",
                        hostname="host", version="0.1", now=T0)
    connection.execute(text("""
        UPDATE worker
        SET health='attention_required', health_detail='held',
            operator_attention_code=:code, attention_recorded_at=:now
        WHERE id='worker-a'
    """), {"code": code, "now": T0})


@pytest.mark.parametrize("code", [
    "worker_operation_failed", "worker_operation_abandoned"])
def test_a_successful_operation_releases_a_machinery_hold(app_url, app, code):
    from career_lens.fleet.registration import (
        clear_completed_operation_attention,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _held_worker(connection, code)
        released = clear_completed_operation_attention(connection, "worker-a")

    row = app.execute(text("SELECT * FROM worker WHERE id='worker-a'")).one()
    assert released == 1
    assert row.health == "unknown"
    assert row.health_detail is None
    assert row.operator_attention_code is None
    assert row.attention_recorded_at is None


@pytest.mark.parametrize("code", [
    "worker_configuration_invalid", "worker_exited", "capture_worker_lost",
    "missing_from_declaration", "linkedin_challenge"])
def test_a_successful_operation_releases_nothing_else(app_url, app, code):
    """Driving one session says nothing about a config file or a dead runner."""
    from career_lens.fleet.registration import (
        clear_completed_operation_attention,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _held_worker(connection, code)
        released = clear_completed_operation_attention(connection, "worker-a")

    row = app.execute(text("""
        SELECT health, operator_attention_code FROM worker WHERE id='worker-a'
    """)).one()
    assert released == 0
    assert row.health == "attention_required"
    assert row.operator_attention_code == code


def test_a_quarantined_worker_is_never_released_by_success(app_url, app):
    from career_lens.fleet.registration import (
        clear_completed_operation_attention,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _held_worker(connection, "worker_operation_failed")
        connection.execute(text(
            "UPDATE worker SET health='quarantined' WHERE id='worker-a'"))
        released = clear_completed_operation_attention(connection, "worker-a")

    health = app.execute(text(
        "SELECT health FROM worker WHERE id='worker-a'")).scalar_one()
    assert released == 0
    assert health == "quarantined"
