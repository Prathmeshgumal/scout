"""The Procrastinate app.

Scope is deliberately narrow (spec 2.4). What is used: transactional enqueue
and completion alongside the data write, one worker, and a lock keyed on the
browser profile -- because Chrome locks user_data_dir and two jobs on one
profile is a physical impossibility, not a policy.

What is NOT used: Procrastinate's retry mechanism. Retry policy here is domain
logic -- a different identity after a challenge, a wait after a rate limit,
never after not_found -- and that does not fit "retry this task with backoff".
v1 does not retry at all; v2 owns the policy.

Its tables live in their own Postgres schema so nothing here shares a namespace
with ours, and so `include_name` in migrations/env.py can exclude them from
autogenerate.
"""

from __future__ import annotations

import procrastinate

from ..store.config import database_url

SCHEMA = "procrastinate"

# Procrastinate takes a libpq connection string, not a SQLAlchemy URL. The
# dialect prefix that store.config insists on is exactly what libpq will not
# accept, so it is stripped here rather than a second variable being invented.
DIALECT = "postgresql+psycopg://"


def connector_url(url: str | None = None) -> str:
    """Translate the application's SQLAlchemy URL to libpq's URL form."""
    url = url or database_url()
    return "postgresql://" + url[len(DIALECT):]


def make_app(url: str | None = None) -> procrastinate.App:
    """Build an app whose worker can open an async connector.

    ``PsycopgConnector`` also supplies a synchronous adapter when
    ``Task.configure(connection=...)`` defers on the API's caller-managed
    psycopg connection. ``SyncPsycopgConnector`` supports that enqueue path but
    cannot run Procrastinate's asynchronous worker loop.
    """
    return procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=connector_url(url),
            kwargs={"options": f"-csearch_path={SCHEMA},public"},
        ),
        import_paths=["career_lens.queue.tasks"],
    )


app = make_app()
