"""Connections, and the gate that keeps a stale checkout off a shared database.

Opening a connection checks the migration revision rather than applying it. A
capture runs for minutes against a database several machines share; a client
that migrated on connect could alter the schema underneath one that was
half-way through writing a capture.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, create_engine, make_url
from sqlalchemy.pool import NullPool

from .config import database_url, migrate_database_url

__all__ = [
    "connect", "engine", "transaction", "migrate", "expected_revision",
    "DatabaseUnavailable", "database_error_is_retryable",
]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


class DatabaseUnavailable(RuntimeError):
    """Unreachable, or carrying a schema this checkout does not know.

    Most callers present both cases the same way. Long-running processes may
    retry a connectivity failure, but must stop on an incompatible schema.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


_NON_RETRYABLE_CONNECTION_MARKERS = (
    "password authentication failed",
    "no pg_hba.conf entry",
    "role does not exist",
    "database does not exist",
)


def database_error_is_retryable(error: Exception) -> bool:
    """Whether retrying can plausibly recover without configuration changes."""
    if isinstance(error, DatabaseUnavailable):
        return error.retryable
    current: BaseException | None = error
    while current is not None:
        sqlstate = (getattr(current, "sqlstate", None)
                    or getattr(getattr(current, "diag", None), "sqlstate", None))
        if sqlstate and sqlstate[:2] in {"28", "3D", "42"}:
            return False
        current = (getattr(current, "orig", None)
                   or getattr(current, "__cause__", None))
    message = str(error).casefold()
    return not any(marker in message
                   for marker in _NON_RETRYABLE_CONNECTION_MARKERS)


def _where(url: str) -> str:
    """The URL without its password. Every failure names what it tried; a
    password in a log is a failure of its own."""
    return make_url(url).render_as_string(hide_password=True)


def engine(url: str | None = None) -> Engine:
    """An engine with no pool, hardcoded to AUTOCOMMIT.

    NullPool because engines are made per caller and never disposed, and a pool
    behind each would exhaust Postgres's connection limit over a test run.

    AUTOCOMMIT is a hazard, not a convenience: on this connection `begin()`
    returns a transaction object whose ROLLBACK rolls nothing back, because
    every statement has already committed on its own by the time it runs. A
    caller that needs a real all-or-nothing unit of work -- Plan 2's capture
    writer is exactly this, on append-only tables where a partial write cannot
    be repaired afterwards -- must build its own connection without
    isolation_level="AUTOCOMMIT" rather than call `begin()` on one from here.
    """
    return create_engine(url or database_url(),
                         isolation_level="AUTOCOMMIT", poolclass=NullPool)


def expected_revision() -> str | None:
    """The migration head this checkout carries, or None if there are none."""
    return ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()


def connect(url: str | None = None) -> Connection:
    """An open connection, or DatabaseUnavailable naming the mismatch."""
    url = url or database_url()
    try:
        connection = engine(url).connect()
    except Exception as exc:
        raise DatabaseUnavailable(
            f"cannot reach {_where(url)}: {exc}",
            retryable=database_error_is_retryable(exc)) from exc

    head = expected_revision()
    try:
        found = MigrationContext.configure(connection).get_current_revision()
    except Exception as exc:
        # Reading the revision is itself a database operation and can fail on
        # its own -- most plausibly a missing grant on alembic_version. This
        # function exists to turn database problems into one legible error, so
        # it cannot let a driver traceback out through its own gate.
        connection.close()
        raise DatabaseUnavailable(
            f"cannot read the schema revision from {_where(url)}: {exc}",
            retryable=database_error_is_retryable(exc)) from exc

    if found != head:
        connection.close()
        raise DatabaseUnavailable(
            f"{_where(url)} is at revision {found or 'none'}, this checkout "
            f"expects {head or 'none'}. Run `career_lens migrate`.",
            retryable=False)
    return connection


@contextmanager
def transaction(url: str | None = None) -> Iterator[Connection]:
    """A connection with a real transaction, gated on the schema revision.

    `engine()` is AUTOCOMMIT, which makes `begin()` a lie there: every
    statement has already committed by the time a ROLLBACK runs. The capture
    writer needs all-or-nothing across append-only tables, where a partial
    write cannot be repaired afterwards -- there is no UPDATE to fix it with.

    The revision gate is the same one `connect()` applies, for the same
    reason: a worker running for minutes against a shared database must not
    discover mid-write that its schema is stale.
    """
    url = url or database_url()
    gate = connect(url)
    gate.close()

    plain = create_engine(url, poolclass=NullPool)
    connection = plain.connect()
    try:
        with connection.begin():
            yield connection
    finally:
        connection.close()
        plain.dispose()


def migrate(url: str | None = None) -> str | None:
    """Bring the schema to head as the owner. Returns the revision applied."""
    url = url or migrate_database_url()
    # Built in memory rather than read from alembic.ini: that file exists for
    # `alembic revision --autogenerate` at a shell, and locating it from an
    # installed package means guessing at a path that may not be there.
    config = AlembicConfig()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return expected_revision()
