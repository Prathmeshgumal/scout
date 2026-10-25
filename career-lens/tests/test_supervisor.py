"""One worker process owns each enabled browser profile worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from career_lens.fleet.config import WorkerConfig, SupervisorConfig

CONFIG = SupervisorConfig(
    "worker-a", "Laptop",
    (WorkerConfig("worker-a", "research-a"),
     WorkerConfig("worker-b", "research-b"),
     WorkerConfig("worker-off", "off", enabled=False)),
)


class FakeProcess:
    made = []

    def __init__(self, *, target, kwargs, name):
        self.target = target
        self.kwargs = kwargs
        self.name = name
        self.started = False
        self.exitcode = None
        self.terminated = False
        self.joined = False
        self.__class__.made.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started and self.exitcode is None and not self.terminated

    def terminate(self):
        self.terminated = True

    def join(self, timeout=None):
        self.joined = True
        self.joined_timeout = timeout

    def kill(self):
        self.killed = True
        self.terminated = True


class FakeVirtualDisplay:
    made = []

    def __init__(self, size):
        self.size = size
        self.started = False
        self.checked = 0
        self.stopped = False
        self.__class__.made.append(self)

    def start(self):
        self.started = True
        return ":83"

    def check(self):
        self.checked += 1

    def stop(self):
        self.stopped = True


def test_the_desktop_display_is_used_unless_virtual_display_is_requested(
        monkeypatch):
    from career_lens.fleet import supervisor as module

    checked = []
    monkeypatch.setattr(
        module, "require_graphical_display", lambda: checked.append(True))
    supervisor = module.Supervisor(CONFIG, process_factory=FakeProcess)

    supervisor.prepare_display()

    assert checked == [True]
    assert supervisor.window_size is None
    assert supervisor.virtual_display is None


def test_virtual_display_defaults_to_full_hd_for_xvfb_and_chrome():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    FakeVirtualDisplay.made = []
    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess, use_virtual_display=True,
        virtual_display_factory=FakeVirtualDisplay)

    supervisor.prepare_display()
    supervisor.start_children()

    display = FakeVirtualDisplay.made[0]
    assert display.size == (1920, 1080)
    assert display.started is True
    assert supervisor.window_size == (1920, 1080)
    assert {process.kwargs["window_size"] for process in FakeProcess.made} == {
        (1920, 1080)}


def test_an_explicit_window_size_is_shared_by_xvfb_and_chrome():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    FakeVirtualDisplay.made = []
    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess, use_virtual_display=True,
        window_size=(1440, 900),
        virtual_display_factory=FakeVirtualDisplay)

    supervisor.prepare_display()
    supervisor.start_children()

    assert FakeVirtualDisplay.made[0].size == (1440, 900)
    assert {process.kwargs["window_size"] for process in FakeProcess.made} == {
        (1440, 900)}


def test_xvfb_is_started_before_registration_and_checked_while_running(
        monkeypatch):
    import pytest

    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.config import ConfigError

    events = []

    class FailedVirtualDisplay:
        def __init__(self, size):
            events.append(("create display", size))

        def start(self):
            events.append("start display")
            return ":83"

        def check(self):
            events.append("check display")
            raise ConfigError("Virtual display stopped unexpectedly")

        def stop(self):
            events.append("stop display")

    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess, use_virtual_display=True,
        virtual_display_factory=FailedVirtualDisplay,
        heartbeat_interval=0.0)
    monkeypatch.setattr(
        supervisor, "preflight", lambda: events.append("preflight"))
    monkeypatch.setattr(
        supervisor, "register", lambda: events.append("register"))
    monkeypatch.setattr(
        supervisor, "install_captcha_assist",
        lambda: events.append("install assist"))
    monkeypatch.setattr(
        supervisor, "start_children", lambda: events.append("start workers"))
    monkeypatch.setattr(
        supervisor, "stop", lambda: events.append("stop workers"))
    monkeypatch.setattr(
        supervisor, "deregister", lambda: events.append("deregister"))

    with pytest.raises(ConfigError, match="stopped unexpectedly"):
        supervisor.run()

    assert events == [
        "preflight",
        ("create display", (1920, 1080)),
        "start display",
        "register",
        "install assist",
        "start workers",
        "check display",
        "stop workers",
        "deregister",
        "stop display",
    ]


def test_supervisor_starts_one_process_per_enabled_slot():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess)
    supervisor.start_children()
    assert list(supervisor.children) == ["worker-a", "worker-b"]
    assert [process.kwargs["worker"].id for process in FakeProcess.made] == [
        "worker-a", "worker-b"]
    assert all(process.started for process in FakeProcess.made)


def test_unexpected_exit_marks_only_that_slot_for_attention():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    reported = []
    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess,
        health_reporter=lambda worker, health, detail, code: reported.append(
            (worker, health, detail, code)))
    supervisor.start_children()
    supervisor.children["worker-a"].exitcode = 7
    supervisor.reconcile_children()
    supervisor.reconcile_children()
    assert reported == [("worker-a", "attention_required",
                         "worker runner exited with code 7",
                         "worker_exited")]
    assert supervisor.children["worker-b"].terminated is False


def test_restarted_slot_keeps_assignments_and_does_not_block_live_slot(
        app_url, app):
    from career_lens.fleet.eligibility import eligible_assignments
    from career_lens.fleet.supervisor import Supervisor
    from career_lens.fleet.registration import mark_worker_health
    from career_lens.store.db import transaction

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, database=app_url,
                            process_factory=FakeProcess)
    supervisor.register()
    now = datetime.now(UTC)
    with transaction(app_url) as connection:
        for worker_id in ("worker-a", "worker-b"):
            mark_worker_health(connection, worker_id, "healthy")
        connection.execute(text("""
            INSERT INTO linkedin_account
                (id, label, username, state, max_attempts_24h,
                 min_interval_seconds, created_at, updated_at)
            VALUES
                ('account-a', 'Account A', 'a@example.com', 'enabled',
                 20, 0, :now, :now),
                ('account-b', 'Account B', 'b@example.com', 'enabled',
                 20, 0, :now, :now)
        """), {"now": now})
        connection.execute(text("""
            INSERT INTO account_worker_assignment
                (id, account_id, worker_id, state, assigned_at, verified_at,
                 verified_host)
            VALUES
                ('assignment-a', 'account-a', 'worker-a', 'ready', :now,
                 :now, 'www.linkedin.com'),
                ('assignment-b', 'account-b', 'worker-b', 'ready', :now,
                 :now, 'www.linkedin.com')
        """), {"now": now})
        for account_id in ("account-a", "account-b"):
            connection.execute(text("""
                INSERT INTO account_schedule
                    (account_id, enabled, timezone, seed,
                     start_jitter_minutes, end_jitter_minutes,
                     created_at, updated_at)
                VALUES (:account, true, 'UTC', 1, 0, 0, :now, :now)
            """), {"account": account_id, "now": now})
            day_id = connection.execute(text("""
                INSERT INTO account_shift_day
                    (account_id, local_date, generated_at)
                VALUES (:account, :today, :now)
                RETURNING id
            """), {
                "account": account_id, "today": now.date(), "now": now,
            }).scalar_one()
            connection.execute(text("""
                INSERT INTO account_shift_block
                    (shift_day_id, account_id, ordinal, kind,
                     starts_at, ends_at, pace)
                VALUES (:day, :account, 0, 'work', :starts, :ends, 1.0)
            """), {
                "day": day_id, "account": account_id,
                "starts": now - timedelta(hours=1),
                "ends": now + timedelta(hours=1),
            })

    supervisor.start_children()
    supervisor.children["worker-a"].exitcode = 7
    supervisor.reconcile_children()
    assert supervisor.heartbeat() is True

    with transaction(app_url) as connection:
        candidates, reason = eligible_assignments(
            connection, now=datetime.now(UTC),
            heartbeat_after=datetime.now(UTC) - timedelta(seconds=30))
    assert reason is None
    assert [(candidate.worker_id, candidate.account_id)
            for candidate in candidates] == [
                ("worker-a", "account-a"), ("worker-b", "account-b")]
    rows = app.execute(text("""
        SELECT id, account_id, worker_id, ended_at
        FROM account_worker_assignment ORDER BY id
    """)).all()
    assert [(row.id, row.account_id, row.worker_id, row.ended_at)
            for row in rows] == [
                ("assignment-a", "account-a", "worker-a", None),
                ("assignment-b", "account-b", "worker-b", None),
            ]


def test_stop_terminates_and_joins_every_child():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess)
    supervisor.start_children()
    supervisor.stop()
    assert all(process.terminated and process.joined
               for process in FakeProcess.made)


def test_stop_reaps_a_child_that_already_exited():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess)
    supervisor.start_children()
    supervisor.children["worker-a"].exitcode = 7

    supervisor.stop()

    assert supervisor.children["worker-a"].joined is True


def test_slot_runner_listens_only_to_its_target_queue(monkeypatch):
    from career_lens.fleet import supervisor as module

    calls = []
    configured = []
    browsers = []

    class Queue:
        def run_worker(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(module, "queue_app", Queue())
    monkeypatch.setattr(
        "career_lens.capture.driver.configure",
        lambda **options: configured.append(options))
    monkeypatch.setattr(
        "career_lens.capture.browser.configure",
        lambda **options: browsers.append(options))
    module.run_worker(
        CONFIG.workers[0], CONFIG, log_level="info", company_tabs="none",
        company_job_pages=0, show_cursor=True, window_size=(1440, 900),
        linkedin_google_search_chance=0.75)
    assert configured == [{
        "tabs": None, "job_pages": 0, "show_cursor": True,
        "har": "off", "linkedin_google_search_chance": 0.75,
    }]
    assert browsers == [{"headless": False, "window_size": (1440, 900)}]
    assert calls == [{
        "queues": ["capture:worker-a"], "name": "worker-a:worker-a",
        "shutdown_graceful_timeout": module.DRAIN_TIMEOUT_S,
    }]


def test_slot_runner_reuses_registered_tasks_with_database_override(monkeypatch):
    from contextlib import contextmanager
    from career_lens.fleet import supervisor as module

    calls = []

    class Queue:
        @contextmanager
        def replace_connector(self, connector):
            calls.append(("connector", connector))
            yield self

        def run_worker(self, **kwargs):
            calls.append(("worker", kwargs))

    class FreshApp:
        connector = "alternate"

    monkeypatch.setattr(module, "queue_app", Queue())
    monkeypatch.setattr(module, "make_app", lambda database: FreshApp())
    module.run_worker(
        CONFIG.workers[0], CONFIG, log_level="info", company_tabs="none",
        company_job_pages=0, database="postgresql://ignored")
    assert calls == [
        ("connector", "alternate"),
        ("worker", {
            "queues": ["capture:worker-a"], "name": "worker-a:worker-a",
            "shutdown_graceful_timeout": module.DRAIN_TIMEOUT_S,
        }),
    ]


def _records(caplog):
    return [record.getMessage() for record in caplog.records]


def test_a_worker_says_what_it_registered_and_which_slots_it_owns(caplog):
    """An idle worker printed nothing at all, so there was no way to tell a
    healthy one waiting for work from a hung one."""
    import logging

    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess)
    with caplog.at_level(logging.INFO, logger="career_lens.fleet"):
        supervisor.announce_start()
        supervisor.start_children()

    logged = " | ".join(_records(caplog))
    assert "worker-a" in logged
    assert "worker-a" in logged and "worker-b" in logged
    # The worker nobody enabled is named too -- silence about it reads as a bug.
    assert "worker-off" in logged


def test_an_idle_worker_still_says_it_is_alive(caplog):
    import logging

    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            idle_report_interval=0.0)
    supervisor.start_children()
    with caplog.at_level(logging.INFO, logger="career_lens.fleet"):
        supervisor.report_idle()

    logged = " | ".join(_records(caplog))
    assert "2" in logged and "worker" in logged.lower()


def test_ctrl_c_is_acted_on_at_once_rather_than_at_the_next_heartbeat():
    """PEP 475 makes `time.sleep` resume after a handler that does not raise,
    so a flag plus sleep(10) waited out the rest of the interval. Measured:
    the handler ran at 1.00s and the sleep returned at 10.00s."""
    import threading
    import time

    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            heartbeat_interval=30.0)

    threading.Timer(0.2, supervisor.ask_stop).start()
    started = time.monotonic()
    supervisor.wait_between_heartbeats()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, elapsed


def test_temporary_database_outage_does_not_stop_the_supervisor(
        monkeypatch, caplog):
    import logging

    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import DatabaseUnavailable

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            heartbeat_interval=0.0)
    heartbeats = 0
    stopped = []

    monkeypatch.setattr(supervisor, "preflight", lambda: None)
    monkeypatch.setattr(supervisor, "prepare_display", lambda: None)
    monkeypatch.setattr(supervisor, "register", lambda: None)
    monkeypatch.setattr(supervisor, "install_captcha_assist", lambda: None)
    monkeypatch.setattr(supervisor, "start_children", lambda: None)
    monkeypatch.setattr(supervisor, "reconcile_children", lambda: None)
    monkeypatch.setattr(supervisor, "report_idle", lambda: None)
    monkeypatch.setattr(supervisor, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(supervisor, "deregister", lambda: None)

    def heartbeat():
        nonlocal heartbeats
        heartbeats += 1
        if heartbeats == 1:
            raise DatabaseUnavailable("central database is offline")
        supervisor.ask_stop()
        return True

    monkeypatch.setattr(supervisor, "heartbeat", heartbeat)

    with caplog.at_level(logging.INFO, logger="career_lens.fleet"):
        supervisor.run()

    assert heartbeats == 2
    assert stopped == [True]
    assert "central database is unavailable" in caplog.text
    assert "central database connection restored" in caplog.text


def test_database_outage_during_startup_waits_then_registers(monkeypatch):
    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import DatabaseUnavailable

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            heartbeat_interval=0.0)
    registrations = 0
    started = []

    monkeypatch.setattr(supervisor, "preflight", lambda: None)
    monkeypatch.setattr(supervisor, "prepare_display", lambda: None)
    monkeypatch.setattr(supervisor, "install_captcha_assist", lambda: None)
    monkeypatch.setattr(supervisor, "start_children", lambda: started.append(True))
    monkeypatch.setattr(supervisor, "reconcile_children", lambda: None)
    monkeypatch.setattr(supervisor, "stop", lambda: None)
    monkeypatch.setattr(supervisor, "deregister", lambda: None)

    def register():
        nonlocal registrations
        registrations += 1
        if registrations == 1:
            raise DatabaseUnavailable("central database is offline")

    def heartbeat():
        supervisor.ask_stop()
        return True

    monkeypatch.setattr(supervisor, "register", register)
    monkeypatch.setattr(supervisor, "heartbeat", heartbeat)

    supervisor.run()

    assert registrations == 2
    assert started == [True]


def test_nonretryable_database_error_stops_startup(monkeypatch):
    import pytest

    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import DatabaseUnavailable

    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            heartbeat_interval=0.0)
    registrations = 0
    monkeypatch.setattr(supervisor, "preflight", lambda: None)
    monkeypatch.setattr(supervisor, "prepare_display", lambda: None)
    monkeypatch.setattr(supervisor, "stop", lambda: None)

    def register():
        nonlocal registrations
        registrations += 1
        if registrations == 1:
            raise DatabaseUnavailable(
                "database schema does not match", retryable=False)
        supervisor.ask_stop()

    monkeypatch.setattr(supervisor, "register", register)

    with pytest.raises(DatabaseUnavailable):
        supervisor.run()

    assert registrations == 1


def test_exited_worker_is_replaced_after_its_exit_is_recorded():
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    reported = []
    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess,
        health_reporter=lambda worker, health, detail, code: reported.append(
            (worker, health, detail, code)))
    supervisor.start_children()
    first_process = supervisor.children["worker-a"]
    first_process.exitcode = 7

    supervisor.reconcile_children()

    replacement = supervisor.children["worker-a"]
    assert replacement is not first_process
    assert replacement.is_alive()
    assert reported == [(
        "worker-a", "attention_required", "worker runner exited with code 7",
        "worker_exited")]


def test_exit_reporting_is_retried_before_the_worker_is_replaced():
    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import DatabaseUnavailable

    FakeProcess.made = []
    reports = 0

    def report(worker, health, detail, code):
        nonlocal reports
        reports += 1
        if reports == 1:
            raise DatabaseUnavailable("central database is offline")

    supervisor = Supervisor(
        CONFIG, process_factory=FakeProcess, health_reporter=report)
    supervisor.start_children()
    first_process = supervisor.children["worker-a"]
    first_process.exitcode = 7

    try:
        supervisor.reconcile_children()
    except DatabaseUnavailable:
        pass
    assert supervisor.children["worker-a"] is first_process

    supervisor.reconcile_children()

    assert reports == 2
    assert supervisor.children["worker-a"] is not first_process
    assert supervisor.children["worker-a"].is_alive()


def test_initial_process_start_failure_does_not_block_other_workers():
    from career_lens.fleet.supervisor import Supervisor

    class FailsFirstStart(FakeProcess):
        attempts = 0

        def start(self):
            self.__class__.attempts += 1
            if self.__class__.attempts == 1:
                raise OSError("process table temporarily unavailable")
            super().start()

    FakeProcess.made = []
    FailsFirstStart.attempts = 0
    reported = []
    supervisor = Supervisor(
        CONFIG, process_factory=FailsFirstStart,
        health_reporter=lambda worker, health, detail, code: reported.append(
            (worker, health, detail, code)))

    supervisor.start_children()
    supervisor.reconcile_children()

    assert supervisor.children["worker-a"].is_alive()
    assert supervisor.children["worker-b"].is_alive()
    assert reported[0][0] == "worker-a"
    assert reported[0][1] == "attention_required"
    assert reported[0][3] == "worker_exited"


def test_stopping_is_announced_and_the_drain_is_bounded(caplog):
    import logging

    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess,
                            drain_timeout=7.0)
    supervisor.start_children()
    with caplog.at_level(logging.INFO, logger="career_lens.fleet"):
        supervisor.stop()

    logged = " | ".join(_records(caplog))
    assert "stopping" in logged.lower()
    # Each child is asked to finish its capture, then joined on the drain
    # budget rather than a hardcoded ten seconds.
    assert all(process.joined_timeout == 7.0 for process in FakeProcess.made)


def test_a_child_that_will_not_drain_is_killed_rather_than_abandoned(caplog):
    """`join(timeout=10)` returned and nothing checked whether the child had
    actually gone, leaving a browser alive with nobody supervising it."""
    import logging

    from career_lens.fleet.supervisor import Supervisor

    class Stubborn(FakeProcess):
        def terminate(self):
            pass  # ignores the polite request

        def kill(self):
            self.killed = True
            self.terminated = True

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=Stubborn,
                            drain_timeout=0.0)
    supervisor.start_children()
    with caplog.at_level(logging.WARNING, logger="career_lens.fleet"):
        supervisor.stop()

    assert all(getattr(process, "killed", False)
               for process in FakeProcess.made)
    assert any("kill" in message.lower() for message in _records(caplog))


def test_a_capture_in_flight_is_given_time_to_finish():
    """The worker runner must ask Procrastinate to drain rather than drop the
    job: a browser killed mid-scrape is how a request records Succeeded with
    sections it never reached."""
    from career_lens.fleet import supervisor as module

    calls = []

    class FakeApp:
        def run_worker(self, **kwargs):
            calls.append(kwargs)

    monkey = FakeApp()
    original = module.queue_app
    module.queue_app = monkey
    try:
        module.run_worker(
            CONFIG.workers[0], CONFIG, log_level="info", company_tabs="all",
            company_job_pages=1)
    finally:
        module.queue_app = original

    assert calls[0]["shutdown_graceful_timeout"] == module.DRAIN_TIMEOUT_S


def test_a_keyring_the_host_cannot_read_stops_the_start(monkeypatch):
    """Discovered at startup, not several minutes later inside a login."""
    import pytest

    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.config import ConfigError

    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", "{1:not-json}")
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", "1")
    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, process_factory=FakeProcess)

    with pytest.raises(ConfigError) as caught:
        supervisor.run()

    assert "CAREER_LENS_CREDENTIAL_KEYS must be a JSON object" in str(
        caught.value)
    assert FakeProcess.made == []


def test_a_readable_keyring_starts_and_says_which_version_is_active(
        monkeypatch, caplog):
    import base64
    import logging

    from career_lens.fleet.supervisor import preflight

    key = base64.b64encode(b"k" * 32).decode()
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", '{"3":"%s"}' % key)
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", "3")

    with caplog.at_level(logging.INFO, logger="career_lens.fleet"):
        preflight("postgresql+psycopg://user:pw@127.0.0.1:5432/career_lens")

    assert "credential keyring version 3 of 1" in caplog.text


def test_no_keyring_at_all_warns_rather_than_refusing_to_start(
        monkeypatch, caplog):
    """A fleet that captures public pages has no keyring to give."""
    import logging

    from career_lens.fleet.supervisor import preflight

    monkeypatch.delenv("CAREER_LENS_CREDENTIAL_KEYS", raising=False)
    monkeypatch.delenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", raising=False)

    with caplog.at_level(logging.WARNING, logger="career_lens.fleet"):
        preflight("postgresql+psycopg://user:pw@127.0.0.1:5432/career_lens")

    assert "cannot sign an account in" in caplog.text


def test_registering_releases_a_worker_held_for_bad_configuration(
        app_url, app):
    """The operator fixes the environment and restarts; nothing else clears it."""
    from career_lens.fleet.registration import mark_worker_health
    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import transaction

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, database=app_url,
                            process_factory=FakeProcess)
    supervisor.register()
    with transaction(app_url) as connection:
        mark_worker_health(
            connection, "worker-a", "attention_required",
            "CAREER_LENS_CREDENTIAL_KEYS must be a JSON object",
            operator_attention_code="worker_configuration_invalid")
        mark_worker_health(
            connection, "worker-b", "attention_required",
            "worker runner exited with code 1",
            operator_attention_code="worker_exited")

    supervisor.register()

    held = dict(app.execute(text(
        "SELECT id, operator_attention_code FROM worker")).all())
    assert held["worker-a"] is None
    # A runner that exited is not something a restart has any evidence about.
    assert held["worker-b"] == "worker_exited"


def test_a_heartbeat_releases_a_worker_whose_runner_is_back(app_url, app):
    """The child is alive again, so `it stopped mid-capture` has expired."""
    from career_lens.fleet.registration import mark_worker_health
    from career_lens.fleet.supervisor import Supervisor
    from career_lens.store.db import transaction

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, database=app_url,
                            process_factory=FakeProcess)
    supervisor.register()
    with transaction(app_url) as connection:
        mark_worker_health(
            connection, "worker-a", "attention_required",
            "the worker stopped during capture",
            operator_attention_code="capture_worker_lost",
            now=datetime.now(UTC) - timedelta(minutes=5))
        mark_worker_health(
            connection, "worker-b", "attention_required",
            "LinkedIn session is signed out",
            operator_attention_code="linkedin_signed_out",
            now=datetime.now(UTC) - timedelta(minutes=5))
    supervisor.start_children()

    assert supervisor.heartbeat() is True

    held = dict(app.execute(text(
        "SELECT id, operator_attention_code FROM worker")).all())
    assert held["worker-a"] is None
    # A live process says nothing about a LinkedIn session.
    assert held["worker-b"] == "linkedin_signed_out"


def test_a_runner_that_dies_again_is_held_again(app_url, app):
    """A crash loop must not be heartbeat away: the beat only covers the live."""
    from career_lens.fleet.supervisor import Supervisor

    FakeProcess.made = []
    supervisor = Supervisor(CONFIG, database=app_url,
                            process_factory=FakeProcess)
    supervisor.register()
    supervisor.start_children()
    supervisor.heartbeat()

    supervisor.children["worker-a"].exitcode = 1
    supervisor.reconcile_children()
    # The replacement also dies before it can produce a heartbeat.
    supervisor.children["worker-a"].exitcode = 1
    supervisor.heartbeat()

    held = dict(app.execute(text(
        "SELECT id, operator_attention_code FROM worker")).all())
    assert held["worker-a"] == "worker_exited"
    assert held["worker-b"] is None


def test_the_captcha_assist_install_runs_once_per_missing_profile(
        monkeypatch):
    """Only the enabled workers, and only the profiles without the solver."""
    from career_lens.fleet import supervisor as module

    monkeypatch.setattr(module.captcha_assist, "installed",
                        lambda profile: profile == "research-a")
    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    installed = []
    monkeypatch.setattr(module.captcha_assist, "install",
                        lambda profile: installed.append(profile) or True)

    supervisor = module.Supervisor(CONFIG, process_factory=FakeProcess)
    supervisor._assist_missing = module.captcha_assist.missing(
        worker.profile for worker in CONFIG.workers if worker.enabled)
    supervisor.install_captcha_assist()

    # "off" is a disabled worker and "research-a" already has it.
    assert installed == ["research-b"]


def test_a_virtual_display_supervisor_never_waits_for_a_dialog(monkeypatch):
    """Nobody can accept Chrome's prompt in an invisible display."""
    from career_lens.fleet import supervisor as module

    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)
    installed = []
    monkeypatch.setattr(module.captcha_assist, "install",
                        lambda profile: installed.append(profile) or True)

    supervisor = module.Supervisor(
        CONFIG, process_factory=FakeProcess, use_virtual_display=True)
    supervisor._assist_missing = ("research-b",)
    supervisor.install_captcha_assist()

    assert installed == []


def test_a_failed_install_does_not_stop_the_supervisor(monkeypatch):
    """An optional facility must never take the fleet down."""
    from career_lens.fleet import supervisor as module

    monkeypatch.setattr(module.sys.stdin, "isatty", lambda: True)

    attempted = []

    def explode(profile):
        attempted.append(profile)
        raise RuntimeError("Chrome would not start")

    monkeypatch.setattr(module.captcha_assist, "install", explode)

    supervisor = module.Supervisor(CONFIG, process_factory=FakeProcess)
    supervisor._assist_missing = ("research-a", "research-b")
    supervisor.install_captcha_assist()

    # The second profile is still offered after the first one fails.
    assert attempted == ["research-a", "research-b"]
