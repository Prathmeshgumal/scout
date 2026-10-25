"""One supervisor per machine, with one isolated queue process per worker."""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import socket
import sys
import threading
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Callable

from procrastinate.exceptions import ConnectorException
from sqlalchemy.exc import OperationalError

from ..capture import captcha_assist
from ..capture.options import DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE
from ..queue.app import app as queue_app, make_app
from ..store.db import (
    DatabaseUnavailable, database_error_is_retryable, transaction,
)
from ..store.ids import SUPERVISOR, new_id
from .config import SupervisorConfig, WorkerConfig
from .attention import WORKER_EXITED
from .registration import (
    clear_worker_configuration_attention, heartbeat_supervisor,
    mark_worker_health, mark_worker_runner_exit, register_supervisor,
    stop_supervisor,
)
from .virtual_display import (
    DEFAULT_VIRTUAL_DISPLAY_SIZE, VirtualDisplay, require_graphical_display,
)

log = logging.getLogger("career_lens.fleet")

#: How long a worker is given to finish the capture it is already running when
#: a stop is asked for. A browser killed part way through a scrape is how
#: a request comes to record `succeeded` over sections it never reached, so the
#: default is generous: a whole profile, sections and activity included, runs
#: in about two minutes.
DRAIN_TIMEOUT_S = 180.0

#: How often an idle supervisor says it is still there. Long enough not to
#: fill a log with nothing, short enough that a silent terminal means something wrong
#: rather than something quiet.
IDLE_REPORT_INTERVAL_S = 300.0


class _QueueDisconnectFilter(logging.Filter):
    """Leave expected queue disconnect reporting to the supervisor wrapper."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("procrastinate"):
            return True
        if record.getMessage() == "Stop requested":
            return False
        if not record.exc_info:
            return True
        return not isinstance(record.exc_info[1], ConnectorException)


def configure_logging(level: str = "info") -> None:
    """Send the supervisor's and Procrastinate's records to the console.

    Nothing called `basicConfig`, so both loggers fell back to the last-resort
    handler at WARNING and every INFO line was dropped -- which is why an idle
    supervisor printed nothing at all and a job being claimed was never
    announced.

    Formatted to sit beside `trace`'s own table rather than fight it: a short
    timestamp, the level, and the message.
    """
    resolved = getattr(logging, str(level).upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S"))
    handler.addFilter(_QueueDisconnectFilter())
    for name in ("career_lens.fleet", "procrastinate"):
        logger = logging.getLogger(name)
        logger.handlers = [
            existing for existing in logger.handlers
            if not isinstance(existing, logging.StreamHandler)]
        logger.addHandler(handler)
        logger.setLevel(resolved)
        # These format for a person reading a terminal. Handing the records to
        # the root logger as well would print each line twice under anything
        # that configures logging itself.
        logger.propagate = False
    # An unavailable database makes psycopg's background pool retry several
    # times before Procrastinate returns one ConnectorException. Printing each
    # internal retry obscures the supervisor's single outage/recovery message.
    pool_logger = logging.getLogger("psycopg.pool")
    pool_logger.handlers = [handler]
    pool_logger.setLevel(logging.ERROR)
    pool_logger.propagate = False


def preflight(database: str | None = None) -> None:
    """Prove this host's configuration before any worker can claim a job.

    A supervisor used to start happily with a keyring it could not parse and
    only found out several minutes later, inside a login, with Chrome already
    open on the account: the operator saw an assignment parked in
    `attention_required` and nothing that named the environment. Every check
    here reads configuration only -- no browser, no network -- so the cost is
    a few milliseconds at startup for a failure that is otherwise discovered
    by a job.

    The keyring is checked only when one of its variables is set. A fleet that
    captures public pages and signs nothing in has no keyring to give, and
    refusing to start would take that fleet down for a facility it never uses.
    Set but unreadable is the real error, and it is the one that fired.
    """
    from ..store.config import (
        artifact_url_ttl_seconds, blob_config, database_url,
    )
    from .credentials import ACTIVE_VARIABLE, KEYS_VARIABLE, credential_keyring

    if database is None:
        database_url()
    blob_config()
    artifact_url_ttl_seconds()
    if os.environ.get(KEYS_VARIABLE) or os.environ.get(ACTIVE_VARIABLE):
        keyring = credential_keyring()
        log.info("configuration checked · credential keyring version %d of %d",
                 keyring.active_version, len(keyring.keys))
        return
    log.warning(
        "no %s is set: this supervisor cannot sign an account in, and any "
        "Assign sent to it will need attention", KEYS_VARIABLE)


def _version() -> str:
    try:
        return version("career-lens")
    except PackageNotFoundError:
        return "development"


def run_worker(worker: WorkerConfig, supervisor: SupervisorConfig, *,
               log_level: str, company_tabs: str,
               company_job_pages: int, show_cursor: bool = False,
               window_size: tuple[int, int] | None = None,
               har: str = "off",
               linkedin_google_search_chance: float = (
                   DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE),
               database: str | None = None) -> None:
    """Configure capture behavior and serve exactly one browser profile."""
    from ..capture import browser, driver, trace
    from ..linkedin.dom import wanted_tabs

    trace.configure(log_level)
    browser.configure(headless=False, window_size=window_size)
    driver.configure(tabs=wanted_tabs(company_tabs),
                     job_pages=company_job_pages,
                     show_cursor=show_cursor,
                     har=har,
                     linkedin_google_search_chance=(
                         linkedin_google_search_chance))
    # Tasks are registered on the module-level app when Procrastinate imports
    # ``career_lens.queue.tasks``. A newly constructed app has the same import
    # path but is not the object those decorators target, so it starts without
    # any runnable tasks. Worker processes are isolated, which makes temporarily
    # replacing the registered app's connector safe when a database override
    # is supplied (the normal CLI path uses its existing connector).
    # `shutdown_graceful_timeout` is what turns a stop into a drain.
    # Procrastinate installs its own SIGINT/SIGTERM handlers and, left at the
    # default of None, waits for a running job forever -- which the supervisor
    # then abandons after its own join. Bounding it here means an in-flight
    # capture finishes if it can and is given up on if it cannot, and either
    # way the supervisor's wait and the runner's agree on the number.
    options = {
        "queues": [f"capture:{worker.id}"],
        "name": f"{supervisor.id}:{worker.id}",
        "shutdown_graceful_timeout": DRAIN_TIMEOUT_S,
    }
    try:
        if database is None:
            queue_app.run_worker(**options)
            return
        with queue_app.replace_connector(make_app(database).connector):
            queue_app.run_worker(**options)
    except ConnectorException:
        # The parent owns reconnect/restart policy. Exit with a temporary
        # failure code but suppress multiprocessing's full child traceback;
        # the parent reports and replaces this runner once the DB returns.
        log.warning(
            "worker %s lost its queue database connection · exiting for "
            "supervisor restart", worker.id)
        raise SystemExit(os.EX_TEMPFAIL) from None


class Supervisor:
    def __init__(
            self, config: SupervisorConfig, *, database: str | None = None,
            process_factory=multiprocessing.Process,
            heartbeat_interval: float = 10.0,
            drain_timeout: float = DRAIN_TIMEOUT_S,
            idle_report_interval: float = IDLE_REPORT_INTERVAL_S,
            log_level: str = "info", company_tabs: str = "all",
            company_job_pages: int = 3, show_cursor: bool = False,
            use_virtual_display: bool = False,
            window_size: tuple[int, int] | None = None,
            har: str = "off",
            linkedin_google_search_chance: float = (
                DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE),
            health_reporter: Callable[
                [str, str, str | None, str | None], None] | None = None,
            virtual_display_factory: Callable[
                [tuple[int, int]], VirtualDisplay] = VirtualDisplay,
    ):
        self.config = config
        self.database = database
        self.process_factory = process_factory
        self.heartbeat_interval = heartbeat_interval
        self.drain_timeout = drain_timeout
        self.idle_report_interval = idle_report_interval
        self.log_level = log_level
        self.company_tabs = company_tabs
        self.company_job_pages = company_job_pages
        self.show_cursor = show_cursor
        self.use_virtual_display = use_virtual_display
        self.window_size = (
            window_size or DEFAULT_VIRTUAL_DISPLAY_SIZE
            if use_virtual_display else window_size)
        self.virtual_display_factory = virtual_display_factory
        self.virtual_display: VirtualDisplay | None = None
        self.har = har
        self.linkedin_google_search_chance = linkedin_google_search_chance
        self.instance_id = new_id(SUPERVISOR)
        self.children = {}
        self._reported_exits: set[str] = set()
        self._pending_start_failures: dict[str, str] = {}
        self._stopping = False
        # An Event rather than a bool: PEP 475 makes `time.sleep` resume after
        # a handler that does not raise, so a flag plus sleep(heartbeat) waited
        # out the rest of the interval before noticing. Measured on this
        # supervisor: the handler ran at 1.00s and the sleep returned at
        # 10.00s. `Event.wait` returns the moment it is set.
        self._stop = threading.Event()
        self._health_reporter = health_reporter
        self._idle_reported_at = 0.0
        self._assist_missing: tuple[str, ...] = ()

    def preflight(self) -> None:
        """Refuse to start on configuration a worker would fail on later."""
        preflight(self.database)
        # A profile read, so it belongs with the other configuration checks.
        # Missing assist is a warning, not a refusal: Google discovery falls
        # back to the requested URL, so the fleet still works without it.
        self._assist_missing = captcha_assist.missing(
            worker.profile for worker in self.config.workers if worker.enabled)
        for profile in self._assist_missing:
            log.warning(
                "captcha assist is not installed in profile %r: a Google "
                "challenge will block a search rather than be answered",
                profile)

    def prepare_display(self) -> None:
        """Select the desktop display or start one owned by this supervisor."""
        if not self.use_virtual_display:
            require_graphical_display()
            return
        display = self.virtual_display_factory(self.window_size)
        self.virtual_display = display
        selected = display.start()
        width, height = self.window_size
        log.info("virtual display %s ready · %dx%dx24", selected, width, height)

    def check_display(self) -> None:
        """Make loss of the supervisor-owned X server a fatal host error."""
        if self.virtual_display is not None:
            self.virtual_display.check()

    def stop_display(self) -> None:
        """Stop the virtual display after every Chrome worker is gone."""
        if self.virtual_display is None:
            return
        self.virtual_display.stop()
        self.virtual_display = None

    def install_captcha_assist(self) -> None:
        """Run the one-time install while no worker owns a profile yet.

        Between `register` and `start_children` is the only window for it: a
        child holds its profile's Chrome lock from then until the stop, and the
        install needs a headed browser and a person to accept Chrome's dialog.
        """
        if not self._assist_missing:
            return
        if self.use_virtual_display:
            log.warning(
                "skipping the captcha assist install in virtual-display mode: "
                "install it through Chrome policy before starting the "
                "supervisor")
            return
        if not sys.stdin.isatty():
            log.warning(
                "skipping the captcha assist install: it needs a terminal and "
                "a headed browser")
            return
        for profile in self._assist_missing:
            log.info("opening the Chrome Web Store in profile %r · accept "
                     "Chrome's dialog to finish", profile)
            try:
                done = captcha_assist.install(profile)
            except Exception as exc:  # an optional facility, never fatal
                log.warning("could not install captcha assist in %r: %s: %s",
                            profile, type(exc).__name__, exc)
                continue
            log.log(
                logging.INFO if done else logging.WARNING,
                "captcha assist %s in profile %r",
                "installed" if done else "was not installed", profile)

    def register(self) -> None:
        with transaction(self.database) as connection:
            register_supervisor(
                connection, self.config, instance_id=self.instance_id,
                hostname=socket.gethostname(), version=_version())
            # The preflight above passed, so a worker parked for bad
            # configuration is describing an environment that no longer
            # exists. Nothing else clears it, and leaving it set would keep a
            # fixed host out of dispatch until someone edited the database.
            cleared = clear_worker_configuration_attention(
                connection, [worker.id for worker in self.config.workers])
        if cleared:
            log.info("cleared a configuration hold on %d worker(s)", cleared)

    def announce_start(self) -> None:
        """What this supervisor is, before it starts doing anything.

        An idle supervisor used to print nothing whatsoever, so a healthy one
        waiting for work and a hung one looked identical from the terminal.
        """
        enabled = [worker for worker in self.config.workers if worker.enabled]
        disabled = [worker for worker in self.config.workers if not worker.enabled]
        log.info("supervisor %s (%s) on %s, career_lens %s, instance %s",
                 self.config.id, self.config.label, socket.gethostname(),
                 _version(), self.instance_id)
        for worker in enabled:
            log.info("  worker %s -> browser profile %r, queue capture:%s",
                     worker.id, worker.profile, worker.id)
        for worker in disabled:
            log.info("  worker %s -> disabled in the local declaration", worker.id)
        if not enabled:
            log.warning("no worker is enabled: this supervisor will take no work")

    def report_idle(self) -> None:
        """Say we are alive, occasionally, when nothing else is being said."""
        now = time.monotonic()
        if now - self._idle_reported_at < self.idle_report_interval:
            return
        self._idle_reported_at = now
        live = sorted(worker_id for worker_id, process in self.children.items()
                      if process.is_alive())
        if live:
            log.info("waiting for work · %d worker(s) live: %s",
                     len(live), ", ".join(live))
        else:
            log.warning("waiting for work · no worker runner is alive")

    def _start_child(self, worker: WorkerConfig) -> None:
        process = self.process_factory(
            target=run_worker,
            kwargs={
                "worker": worker, "supervisor": self.config,
                "log_level": self.log_level,
                "company_tabs": self.company_tabs,
                "company_job_pages": self.company_job_pages,
                "show_cursor": self.show_cursor,
                "window_size": self.window_size,
                "har": self.har,
                "linkedin_google_search_chance": (
                    self.linkedin_google_search_chance),
                "database": self.database,
            },
            name=f"career-lens-{self.config.id}-{worker.id}",
        )
        process.start()
        self.children[worker.id] = process

    def _try_start_child(self, worker: WorkerConfig) -> bool:
        try:
            self._start_child(worker)
        except Exception as exc:  # process creation is an OS boundary
            detail = f"worker runner could not start: {type(exc).__name__}: {exc}"
            self._pending_start_failures[worker.id] = detail
            log.error("%s · will retry", detail)
            return False
        self._pending_start_failures.pop(worker.id, None)
        return True

    def start_children(self) -> None:
        for worker in self.config.workers:
            if worker.enabled:
                self._try_start_child(worker)

    def _report_health(self, worker_id: str, health: str,
                       detail: str | None,
                       operator_attention_code: str | None = None) -> None:
        if self._health_reporter is not None:
            self._health_reporter(
                worker_id, health, detail, operator_attention_code)
            return
        with transaction(self.database) as connection:
            if operator_attention_code == WORKER_EXITED:
                mark_worker_runner_exit(connection, worker_id, detail or health)
            else:
                mark_worker_health(
                    connection, worker_id, health, detail,
                    operator_attention_code=operator_attention_code)

    def reconcile_children(self) -> None:
        for worker in self.config.workers:
            if not worker.enabled or self._stopping:
                continue
            worker_id = worker.id
            process = self.children.get(worker_id)
            if process is not None and process.is_alive():
                continue
            detail = self._pending_start_failures.get(worker_id)
            if detail is None:
                detail = (f"worker runner exited with code {process.exitcode}"
                          if process is not None
                          else "worker runner is missing")
            if worker_id not in self._reported_exits:
                self._report_health(
                    worker_id, "attention_required", detail, WORKER_EXITED)
                # Only suppress a repeat after the durable report succeeds.
                self._reported_exits.add(worker_id)
            if process is not None:
                process.join(timeout=0)
            if self._try_start_child(worker):
                self._reported_exits.discard(worker_id)
                log.warning("worker %s runner restarted", worker_id)

    def heartbeat(self) -> bool:
        live = {worker_id for worker_id, process in self.children.items()
                if process.is_alive()}
        with transaction(self.database) as connection:
            return heartbeat_supervisor(
                connection, self.config.id, self.instance_id,
                live_worker_ids=live)

    def ask_stop(self) -> None:
        """Request a stop. Safe from a signal handler -- it only sets flags."""
        self._stopping = True
        self._stop.set()

    def wait_between_heartbeats(self) -> None:
        """Sleep until the next heartbeat, or until a stop is asked for."""
        self._stop.wait(self.heartbeat_interval)

    def stop(self) -> None:
        """Drain the worker runners, then make sure they are actually gone."""
        self._stopping = True
        self._stop.set()
        exited = [(worker_id, process)
                  for worker_id, process in self.children.items()
                  if not process.is_alive()]
        for _, process in exited:
            process.join(timeout=0)
        running = [(worker_id, process)
                   for worker_id, process in self.children.items()
                   if process.is_alive()]
        if not running:
            log.info("stopping · no worker runner was running · %d reaped",
                     len(exited))
            return
        log.info("stopping · asking %d worker runner(s) to finish the capture "
                 "they are on, up to %.0fs: %s",
                 len(running), self.drain_timeout,
                 ", ".join(worker_id for worker_id, _ in running))
        for _, process in running:
            process.terminate()
        # The same budget the runner was given, so the two agree rather than
        # the supervisor giving up first and orphaning a live browser.
        for worker_id, process in running:
            process.join(timeout=self.drain_timeout)
        stubborn = [(worker_id, process) for worker_id, process in running
                    if process.is_alive()]
        for worker_id, process in stubborn:
            log.warning("worker %s did not stop within %.0fs · kill",
                        worker_id, self.drain_timeout)
            process.kill()
            process.join(timeout=5)
        log.info("stopped · %d worker runner(s) drained, %d killed",
                 len(running) - len(stubborn), len(stubborn))

    def deregister(self) -> None:
        """Record that this supervisor stopped on purpose.

        Without it a stopped supervisor is indistinguishable from a crashed one:
        `desired_state` stays `active` and the heartbeat simply ages, so the
        console shows a supervisor that is still meant to be running. Registering
        again on the next start puts it back to `active`.
        """
        try:
            with transaction(self.database) as connection:
                stop_supervisor(connection, self.config.id, self.instance_id)
        except Exception as exc:  # a stop must not fail on its own bookkeeping
            log.warning("could not record the stop: %s: %s",
                        type(exc).__name__, exc)

    def run(self) -> None:
        self.announce_start()
        self.preflight()
        previous = {}
        registered = False

        def on_signal(signum, frame):
            log.info("received %s", signal.Signals(signum).name)
            self.ask_stop()

        for name in (signal.SIGINT, signal.SIGTERM):
            previous[name] = signal.signal(name, on_signal)
        try:
            self.prepare_display()
            database_unavailable = False
            while not self._stopping:
                try:
                    self.register()
                except (DatabaseUnavailable, OperationalError) as exc:
                    if not database_error_is_retryable(exc):
                        raise
                    if not database_unavailable:
                        log.warning(
                            "central database is unavailable during startup · "
                            "waiting to register: %s",
                            str(exc).splitlines()[0])
                    database_unavailable = True
                    self.wait_between_heartbeats()
                    continue
                registered = True
                if database_unavailable:
                    log.info("central database connection restored")
                break
            if self._stopping:
                return

            self.install_captcha_assist()
            self.start_children()
            self._idle_reported_at = time.monotonic()
            database_unavailable = False
            while not self._stopping:
                try:
                    self.check_display()
                    self.reconcile_children()
                    if not self.heartbeat():
                        raise RuntimeError(
                            "supervisor registration was replaced by another "
                            "instance")
                except (DatabaseUnavailable, OperationalError) as exc:
                    if not database_error_is_retryable(exc):
                        raise
                    if not database_unavailable:
                        log.warning(
                            "central database is unavailable · supervisor "
                            "remains online; stopped worker runners will "
                            "restart after recovery: %s",
                            str(exc).splitlines()[0])
                    database_unavailable = True
                    self.wait_between_heartbeats()
                    continue
                if database_unavailable:
                    log.info("central database connection restored")
                    database_unavailable = False
                self.report_idle()
                self.wait_between_heartbeats()
        finally:
            try:
                self.stop()
            finally:
                try:
                    if registered:
                        self.deregister()
                finally:
                    try:
                        self.stop_display()
                    finally:
                        for name, handler in previous.items():
                            signal.signal(name, handler)
