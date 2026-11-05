"""Durable session operations trust browser observations, not DB intent."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import text

from career_lens.linkedin.session import SessionObservation

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
ALICE = "https://www.linkedin.com/in/alice/"
BOB = "https://www.linkedin.com/in/bob/"


def _pending_login(app_url, profile_url=ALICE):
    from career_lens.fleet.assignments import assign_account
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        connection.execute(text("""
            INSERT INTO supervisor
                (id, label, desired_state, instance_id, hostname,
                 software_version, started_at, last_heartbeat_at)
            VALUES ('worker-a', 'Laptop', 'active', 'boot', 'host', 'test',
                    :now, :now)
        """), {"now": NOW})
        connection.execute(text("""
            INSERT INTO worker
                (id, supervisor_id, local_name, desired_state, health)
            VALUES ('worker-a', 'worker-a', 'research', 'enabled', 'healthy')
        """))
        connection.execute(text("""
            INSERT INTO linkedin_account
                (id, label, username, profile_url, state, max_attempts_24h,
                 min_interval_seconds, created_at, updated_at)
            VALUES ('la-a', 'Alice', 'alice@example.com', :profile, 'enabled',
                    20, 0, :now, :now)
        """), {"profile": profile_url, "now": NOW})
        connection.execute(text("""
            INSERT INTO linkedin_account_credential
                (account_id, ciphertext, nonce, key_version, updated_at)
            VALUES ('la-a', :ciphertext, :nonce, 1, :now)
        """), {"ciphertext": b"opaque", "nonce": b"n" * 12, "now": NOW})
        return assign_account(
            connection, account_id="la-a", worker_id="worker-a", now=NOW)


class ClosedPage:
    def is_closed(self):
        return True


def _factory(page=None):
    page = page if page is not None else ClosedPage()
    @contextmanager
    def factory(*, profile):
        assert profile == "research"
        yield page
    return factory


def _counted_factory(page=None):
    """A session factory that records how many browsers a run opened.

    Clearing is only believable from a browser started after the sign-out, so
    the count is the behaviour under test rather than an implementation detail.
    """
    opened = []
    page = page if page is not None else ClosedPage()

    @contextmanager
    def factory(*, profile):
        assert profile == "research"
        opened.append(profile)
        yield page
    factory.opened = opened
    return factory


def _observations(*results):
    """Answer each observation in turn, repeating the last one afterwards."""
    remaining = list(results)

    def observer(page):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]
    return observer


def test_manual_verify_uses_the_waiting_browser_and_releases_it(app_url, app):
    from career_lens.fleet.assignments import request_verify
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, operation_id = _pending_login(app_url, profile_url=None)

    class WaitingPage:
        def wait_for_timeout(self, milliseconds):
            pass

        def is_closed(self):
            with transaction(app_url) as connection:
                state = connection.execute(text("""
                    SELECT o.status, o.manual_intervention_at, a.state,
                           a.recovery_phase
                    FROM worker_operation o
                    JOIN account_worker_assignment ass ON ass.id=o.assignment_id
                    JOIN linkedin_account a ON a.id=ass.account_id
                    WHERE o.id=:id
                """), {"id": operation_id}).one()
                assert state.status == "running"
                assert state.manual_intervention_at is not None
                assert state.state == "attention_required"
                assert state.recovery_phase is None
                assert request_verify(connection, assignment_id) == operation_id
            return False

    factory = _counted_factory(WaitingPage())
    run_worker_operation(
        operation_id, database=app_url, session_factory=factory,
        observer=_observations(SessionObservation("challenge", detail="OTP required"),
                               SessionObservation("signed_out", detail="Still signed out"),
                               SessionObservation("signed_in", ALICE)))
    row = app.execute(text("""
        SELECT ass.state, a.profile_url, o.status, o.manual_intervention_at
        FROM worker_operation o
        JOIN account_worker_assignment ass ON ass.id=o.assignment_id
        JOIN linkedin_account a ON a.id=ass.account_id
        WHERE o.id=:id
    """), {"id": operation_id}).one()
    assert row.state == "ready"
    assert row.profile_url == ALICE
    assert row.status == "succeeded"
    assert row.manual_intervention_at is None
    assert factory.opened == ["research"]


def test_disabling_account_releases_manual_window_without_reenabling(app_url, app):
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    _, operation_id = _pending_login(app_url)

    class WaitingPage:
        def is_closed(self):
            with transaction(app_url) as connection:
                connection.execute(text("""
                    UPDATE linkedin_account SET state='disabled',
                        operator_attention_code=NULL, attention_detail=NULL
                """))
            return False

    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(WaitingPage()),
        observer=lambda page: SessionObservation("challenge", detail="OTP required"))
    row = app.execute(text("SELECT status,manual_intervention_at FROM worker_operation")).one()
    assert app.execute(text("SELECT state FROM linkedin_account")).scalar_one() == "disabled"
    assert row.status == "failed"
    assert row.manual_intervention_at is None


def test_matching_existing_identity_becomes_ready(app_url, app):
    from career_lens.fleet.operations import run_worker_operation

    assignment_id, operation_id = _pending_login(app_url)
    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation(
            "signed_in", ALICE, host="in.linkedin.com"))
    row = app.execute(text("""
        SELECT ass.state, ass.verified_host, operation.status, worker.health
        FROM account_worker_assignment ass
        JOIN worker_operation operation ON operation.assignment_id=ass.id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row._mapping == {
        "state": "ready", "verified_host": "in.linkedin.com",
        "status": "succeeded", "health": "healthy",
    }


def test_automatic_relogin_success_starts_account_cooldown(app_url, app):
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, operation_id = _pending_login(app_url)
    with transaction(app_url) as connection:
        connection.execute(text("""
            UPDATE linkedin_account
            SET recovery_phase='before_relogin', recovery_not_before=:now
            WHERE id='la-a'
        """), {"now": NOW})

    run_worker_operation(
        operation_id,
        database=app_url,
        session_factory=_factory(),
        observer=lambda page: SessionObservation(
            "signed_in", ALICE, host="in.linkedin.com"),
    )

    row = app.execute(text("""
        SELECT ass.state, account.state AS account_state,
               account.recovery_phase,
               account.recovery_not_before > :now AS cooling
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id, "now": NOW}).one()
    assert row == ("ready", "enabled", "after_relogin", True)


def test_matching_identity_ignores_linkedin_self_profile_query(app_url, app):
    from career_lens.fleet.operations import run_worker_operation

    assignment_id, operation_id = _pending_login(app_url)
    observed = "https://in.linkedin.com/in/alice/?isSelfProfile=true"
    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", observed))
    row = app.execute(text("""
        SELECT ass.state, operation.status, worker.health
        FROM account_worker_assignment ass
        JOIN worker_operation operation ON operation.assignment_id=ass.id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row._mapping == {
        "state": "ready", "status": "succeeded", "health": "healthy",
    }


def test_different_existing_identity_requires_operator_confirmation(
        app_url, app):
    from career_lens.fleet.operations import run_worker_operation

    assignment_id, operation_id = _pending_login(app_url)
    clears = []
    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", BOB),
        clear_action=lambda page: clears.append(True))
    row = app.execute(text("""
        SELECT ass.state, operation.status, ass.ended_at
        FROM account_worker_assignment ass
        JOIN worker_operation operation ON operation.assignment_id=ass.id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row.state == "account_mismatch"
    assert row.status == "failed" and row.ended_at is None
    assert clears == []


def test_unknown_existing_identity_is_not_adopted(app_url, app):
    from career_lens.fleet.operations import run_worker_operation

    assignment_id, operation_id = _pending_login(app_url, profile_url=None)
    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    state = app.execute(text("""
        SELECT state FROM account_worker_assignment WHERE id=:assignment
    """), {"assignment": assignment_id}).scalar_one()
    assert state == "account_mismatch"


def test_verify_after_manual_intervention_recovers_account_and_slot(
        app_url, app):
    from career_lens.fleet.assignments import request_verify
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation(
            "challenge", detail="manual intervention required"))
    with transaction(app_url) as connection:
        verify_id = request_verify(connection, assignment_id, now=NOW)

    run_worker_operation(
        verify_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))

    row = app.execute(text("""
        SELECT ass.state, account.state AS account_state, worker.health,
               operation.status
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker worker ON worker.id=ass.worker_id
        JOIN worker_operation operation ON operation.id=:operation
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id, "operation": verify_id}).one()
    assert row._mapping == {
        "state": "ready", "account_state": "enabled", "health": "healthy",
        "status": "succeeded",
    }


def test_duplicate_delivery_is_a_noop(app_url, app):
    from career_lens.fleet.operations import run_worker_operation

    _, operation_id = _pending_login(app_url)
    observations = []
    observer = lambda page: (
        observations.append(True) or SessionObservation("signed_in", ALICE))
    values = dict(database=app_url, session_factory=_factory(), observer=observer)
    run_worker_operation(operation_id, **values)
    run_worker_operation(operation_id, **values)
    assert observations == [True]


def test_operation_does_not_overwrite_a_concurrent_assignment_transition(
        app_url, app):
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, operation_id = _pending_login(app_url)

    def observe(page):
        with transaction(app_url) as connection:
            connection.execute(text("""
                UPDATE account_worker_assignment
                SET state='attention_required', state_detail='operator changed it',
                    operator_attention_code='manual_hold'
                WHERE id=:assignment
            """), {"assignment": assignment_id})
        return SessionObservation("signed_in", ALICE)

    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=observe)

    row = app.execute(text("""
        SELECT ass.state, ass.state_detail, operation.status
        FROM account_worker_assignment ass
        JOIN worker_operation operation ON operation.assignment_id=ass.id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row._mapping == {
        "state": "attention_required",
        "state_detail": "operator changed it",
        "status": "failed",
    }


def test_signed_out_session_decrypts_only_for_login(app_url, app):
    from career_lens.fleet.credentials import CredentialKeyring, encrypt_password
    from career_lens.fleet.operations import run_worker_operation

    keyring = CredentialKeyring(active_version=1, keys={1: b"k" * 32})
    _, operation_id = _pending_login(app_url)
    envelope = encrypt_password("la-a", "secret", keyring=keyring)
    app.execute(text("""
        UPDATE linkedin_account_credential
        SET ciphertext=:ciphertext, nonce=:nonce
        WHERE account_id='la-a'
    """), {"ciphertext": envelope.ciphertext, "nonce": envelope.nonce})
    received = []

    def login(page, *, username, password):
        received.append((username, password))
        return SessionObservation("signed_in", ALICE)

    run_worker_operation(
        operation_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_out"),
        login_action=login, keyring=keyring)
    assert received == [("alice@example.com", "secret")]


def test_failed_clear_quarantines_account_and_slot(app_url, app):
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    run_worker_operation(
        clear_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE),
        clear_action=lambda page: SessionObservation(
            "signed_in", ALICE, "session remained signed in"))

    row = app.execute(text("""
        SELECT ass.state, ass.ended_at, account.state AS account_state,
               account.attention_detail, worker.health,
               ass.operator_attention_code AS assignment_code,
               account.operator_attention_code AS account_code,
               worker.operator_attention_code AS worker_code
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row._mapping == {
        "state": "attention_required", "ended_at": None,
        "account_state": "attention_required",
        "attention_detail": "session remained signed in",
        # Untouched: a profile that would not sign out is a session fact,
        # not a claim about Chrome.
        "health": "healthy",
        "assignment_code": "linkedin_clear_failed",
        "account_code": "linkedin_clear_failed",
        "worker_code": None,
    }


def test_clear_is_not_believed_until_a_fresh_browser_confirms_it(app_url, app):
    """LinkedIn's `li_rm` outlives its own sign-out and silently restores the
    session on the next visit. Measured on a live profile: sign-out clicked,
    operation reported success, assignment marked ended -- and the next browser
    read the account back as signed in. Only an observation made after the
    sign-out, in a browser that reloaded cookies from disk, can settle this.
    """
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    factory = _counted_factory()
    run_worker_operation(
        clear_id, database=app_url, session_factory=factory,
        observer=_observations(
            SessionObservation("signed_in", ALICE),
            SessionObservation("signed_in", ALICE)),
        clear_action=lambda page: SessionObservation("signed_out"),
        reset_action=lambda page, **kwargs: None)

    assert len(factory.opened) == 2
    row = app.execute(text("""
        SELECT operation.status, ass.state, ass.ended_at, worker.health,
               ass.operator_attention_code
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
    """), {"operation": clear_id}).one()
    assert row.status == "failed"
    assert row.state == "attention_required" and row.ended_at is None
    assert row.operator_attention_code == "linkedin_clear_failed"
    # The browser is fine; it is the session that would not clear.
    assert row.health == "healthy"


def test_clear_removes_linkedin_browser_state_before_confirming(app_url, app):
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    resets = []
    run_worker_operation(
        clear_id, database=app_url, session_factory=_counted_factory(),
        observer=_observations(
            SessionObservation("signed_in", ALICE),
            SessionObservation("signed_out")),
        clear_action=lambda page: SessionObservation("signed_out"),
        reset_action=lambda page, **kwargs: resets.append(page))

    assert resets, "a cleared worker must not keep LinkedIn's site data"
    row = app.execute(text("""
        SELECT operation.status, ass.state FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        WHERE operation.id=:operation
    """), {"operation": clear_id}).one()
    assert row.status == "succeeded" and row.state == "ended"


def test_clear_already_signed_out_session_succeeds_without_logout_click(
        app_url, app):
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    def unexpected_logout(page):
        raise AssertionError("an already signed-out session needs no logout click")

    run_worker_operation(
        clear_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_out"),
        clear_action=unexpected_logout,
        reset_action=lambda page, **kwargs: None)

    row = app.execute(text("""
        SELECT operation.status, ass.state, ass.ended_at, worker.health
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
    """), {"operation": clear_id}).one()
    assert row.status == "succeeded"
    assert row.state == "ended" and row.ended_at is not None
    assert row.health == "healthy"


def test_browser_failure_during_clear_closes_the_operation(app_url, app):
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    @contextmanager
    def exploding(*, profile):
        assert profile == "research"
        raise RuntimeError("chrome was killed mid-clear")
        yield  # pragma: no cover

    run_worker_operation(
        clear_id, database=app_url, session_factory=exploding)

    row = app.execute(text("""
        SELECT operation.status, ass.state, worker.health,
               ass.operator_attention_code AS assignment_code,
               worker.operator_attention_code AS worker_code
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        JOIN worker worker ON worker.id=ass.worker_id
        WHERE operation.id=:operation
    """), {"operation": clear_id}).one()
    assert row._mapping == {
        "status": "failed", "state": "attention_required",
        "health": "attention_required",
        "assignment_code": "worker_operation_failed",
        "worker_code": "worker_operation_failed",
    }


def test_failure_handler_closes_operation_if_assignment_changed(
        app_url, app):
    from career_lens.fleet.assignments import request_clear
    from career_lens.fleet.operations import run_worker_operation
    from career_lens.store.db import transaction

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation("signed_in", ALICE))
    with transaction(app_url) as connection:
        clear_id = request_clear(connection, assignment_id, now=NOW)

    @contextmanager
    def changed_then_exploding(*, profile):
        assert profile == "research"
        with transaction(app_url) as connection:
            connection.execute(text("""
                UPDATE account_worker_assignment SET state='ready'
                WHERE id=:assignment
            """), {"assignment": assignment_id})
        raise RuntimeError("chrome was killed after assignment changed")
        yield  # pragma: no cover

    run_worker_operation(
        clear_id, database=app_url, session_factory=changed_then_exploding)

    row = app.execute(text("""
        SELECT operation.status, ass.state
        FROM worker_operation operation
        JOIN account_worker_assignment ass ON ass.id=operation.assignment_id
        WHERE operation.id=:operation
    """), {"operation": clear_id}).one()
    assert row._mapping == {"status": "failed", "state": "ready"}


def test_a_challenge_leaves_the_browser_usable(app_url, app):
    """LinkedIn's answer is about the session, not about Chrome."""
    from career_lens.fleet.operations import run_worker_operation

    assignment_id, login_id = _pending_login(app_url)
    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=lambda page: SessionObservation(
            "challenge", detail="manual intervention required"))

    row = app.execute(text("""
        SELECT ass.state, ass.operator_attention_code AS assignment_code,
               account.state AS account_state,
               account.operator_attention_code AS account_code,
               worker.health, worker.operator_attention_code AS worker_code
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker ON worker.id=ass.worker_id
        WHERE ass.id=:assignment
    """), {"assignment": assignment_id}).one()
    assert row._mapping == {
        "state": "attention_required",
        "assignment_code": "linkedin_challenge",
        "account_state": "attention_required",
        "account_code": "linkedin_challenge",
        # Untouched. `_pending_login` leaves the worker `healthy`.
        "health": "healthy", "worker_code": None,
    }


def test_an_exception_while_driving_still_benches_the_worker(app_url, app):
    """A driver that threw is evidence about this machine, and is kept."""
    from career_lens.fleet.operations import run_worker_operation

    _, login_id = _pending_login(app_url)

    def explode(page):
        raise RuntimeError("chrome went away")

    run_worker_operation(
        login_id, database=app_url, session_factory=_factory(),
        observer=explode)

    row = app.execute(text("""
        SELECT health, operator_attention_code, attention_recorded_at
        FROM worker
    """)).one()
    assert row.health == "attention_required"
    assert row.operator_attention_code == "worker_operation_failed"
    assert row.attention_recorded_at is not None
