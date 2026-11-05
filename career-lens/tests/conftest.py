"""A real Postgres per test, cloned from one migrated template.

Triggers, grants, exclusion constraints and check constraints are the substance
of this plan, and none of them exist in a mock. So the harness migrates a
template database once per session and clones it per test -- a file copy of an
empty schema, which is fast enough to keep function scope and leaves every test
with a database nobody else has touched.

Tests connect as `career_lens_app` by default. That is the point: an accidental
UPDATE against an append-only table fails here rather than in production.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import os
import secrets
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from career_lens.store.config import APP_ROLE, MIGRATE_ROLE
from career_lens.store.db import migrate


MAINTENANCE = "postgres"

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def _template_name() -> str:
    """One template database per checkout and pytest worker.

    Two worktrees of this repository share a PostgreSQL server, and each is at
    its own migration head. A single fixed name meant the second session to
    start dropped the first's template mid-run and replaced it with a schema at
    a different revision -- which surfaces as an unrelated-looking
    `DatabaseUnavailable: ... is at revision X, this checkout expects Y`.

    Derived from the checkout path rather than configured, so parallel
    worktrees need no setup and no coordination. `CAREER_LENS_TEST_TEMPLATE`
    overrides that base name. An xdist worker suffix keeps concurrent session
    fixtures from recreating or dropping one another's template.
    """
    configured = os.environ.get("CAREER_LENS_TEST_TEMPLATE")
    if configured is None:
        root = str(Path(__file__).resolve().parent.parent)
        configured = (f"career_lens_test_template_"
                      f"{hashlib.blake2b(root.encode(), digest_size=4).hexdigest()}")
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    return f"{configured}_{worker}" if worker else configured


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--browser", action="store_true", default=False,
        help="run tests that open a real Chrome window",
    )


def pytest_collection_modifyitems(
        config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep headed-browser checks out of the routine test suite.

    They remain available for deliberate capture verification with
    ``uv run pytest --browser``.
    """
    if config.getoption("--browser"):
        return
    skip = pytest.mark.skip(reason="needs --browser (opens a real Chrome window)")
    for item in items:
        if "browser" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def site() -> str:
    """A local HTTP server over the fixture pages.

    Never against LinkedIn: a test that needs a signed-in session and a live
    site is not a test. Shared by every module that drives a real browser
    against fixtures, so there is one server per test session rather than one
    per module.
    """
    handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _url(database: str, section: str) -> str:
    host = os.environ.get("CAREER_LENS_TEST_HOST", "127.0.0.1:5433")
    secret = os.environ.get("CAREER_LENS_TEST_PASSWORD", "career-lens-dev-secret")
    return f"postgresql+psycopg://{section}:{secret}@{host}/{database}"


def _run(sql: str) -> None:
    """One statement against the maintenance database, then disconnect.

    CREATE DATABASE cannot run inside a transaction, and cloning a template
    fails while anyone is connected to it -- so this opens, runs, and closes
    rather than holding anything.
    """
    engine = create_engine(_url(MAINTENANCE, MIGRATE_ROLE),
                           isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with engine.connect() as connection:
        connection.execute(text(sql))
    engine.dispose()


def _drop_database(name: str) -> None:
    """Drop a test clone after its last client disconnect reaches Postgres.

    ``DROP DATABASE ... WITH (FORCE)`` is the wrong cleanup primitive here:
    the migrator owns the database but deliberately does not inherit the app
    section or ``pg_signal_backend``, so it cannot terminate an app connection.
    A connection close can reach the server a few milliseconds after the test
    fixture returns. Retry only PostgreSQL's object-in-use error and let every
    other failure surface immediately.
    """
    for attempt in range(20):
        try:
            _run(f'DROP DATABASE IF EXISTS "{name}"')
            return
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55006" or attempt == 19:
                raise
            time.sleep(0.05)


@pytest.fixture(scope="session")
def template() -> Iterator[str]:
    """A migrated, empty database that every test's database is copied from."""
    name = _template_name()
    _drop_database(name)
    _run(f'CREATE DATABASE "{name}" OWNER {MIGRATE_ROLE}')
    migrate(_url(name, MIGRATE_ROLE))
    yield name
    _drop_database(name)


@pytest.fixture
def database(template: str) -> Iterator[str]:
    """A throwaway clone, dropped when the test ends."""
    name = f"career_lens_test_{secrets.token_hex(6)}"
    _run(f'CREATE DATABASE "{name}" TEMPLATE "{template}" OWNER {MIGRATE_ROLE}')
    yield name
    _drop_database(name)


@pytest.fixture
def app(database: str) -> Iterator[Connection]:
    """Connected as career_lens_app -- restricted, like production."""
    engine = create_engine(_url(database, APP_ROLE),
                           isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


@pytest.fixture
def migrator(database: str) -> Iterator[Connection]:
    """Connected as the owner. For asserting on catalogue state, not for setup."""
    engine = create_engine(_url(database, MIGRATE_ROLE),
                           isolation_level="AUTOCOMMIT", poolclass=NullPool)
    with engine.connect() as connection:
        yield connection
    engine.dispose()


@pytest.fixture
def app_url(database: str) -> str:
    """The application's URL for this test's database.

    For code under test that opens its own connection -- the capture writer,
    the queue task, the API's dependency -- rather than accepting one.
    """
    return _url(database, APP_ROLE)


@pytest.fixture
def migrate_url(database: str) -> str:
    """The owner's URL. For asserting on behaviour the app section is not
    permitted to exercise -- never as a way around a grant in a test of
    application code."""
    return _url(database, MIGRATE_ROLE)
