"""Opening a write transaction from a read-scoped request connection.

The routers depend on a plain connection for reads. A route that writes needs a
transaction, and it has to be on the same database the dependency handed it --
which the connection itself is the only honest source of, since every test
session points at its own.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection

from ..store.db import connect, transaction


def database() -> Iterator[Connection]:
    """The read connection that routes depend on. Tests override it by this
    function, which `v1` and `security` both import from here: kept out of
    `v1` because `security` needs it and `v1` needs `security`."""
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def database_url(connection: Connection) -> str:
    return connection.engine.url.render_as_string(hide_password=False)


@contextmanager
def write(connection: Connection):
    with transaction(database_url(connection)) as writable:
        yield writable
