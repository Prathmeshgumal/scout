"""A queued public request with a live anonymous worker, shared by several test files."""

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import text

from career_lens.store.ids import REQUEST, new_id

NOW = datetime.now(UTC)
URL = "https://example.org/page"
PAGE = object()


class _FakeStore:
    """Accepts and remembers, so write_capture's verification passes."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def url(self, key, expires=3600):
        return f"https://blobs.invalid/{key}"


@contextmanager
def _page_session(**kwargs):
    yield PAGE


def refuse_linkedin_observation(page):
    raise AssertionError("Public pages must not inspect LinkedIn identity")


def _public_fleet(connection):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('supervisor-public', 'Public host', 'active',
                'instance-public', 'host-public', 'test', now(), now())
    """))
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES ('worker-public', 'supervisor-public', 'public-profile',
                'enabled', 'unknown', now())
    """))


def _linkedin_fleet(connection):
    """A ready assignment on a second worker, to prove it is left alone."""
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES ('worker-linkedin', 'supervisor-public', 'linkedin-profile',
                'enabled', 'healthy', now())
    """))
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-a', 'Research', 'a@example.com',
                'https://www.linkedin.com/in/alice/', 'enabled', 20, 0,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-a', 'la-a', 'worker-linkedin', 'ready', :now, :now,
                'www.linkedin.com')
    """), {"now": NOW})


def _anonymous_attempt(app_url, *, cancelled=False, max_attempts=3,
                       linkedin=False, tempo="research",
                       kind="generic"):
    from career_lens.store.db import transaction

    request_id = new_id(REQUEST)
    with transaction(app_url) as connection:
        _public_fleet(connection)
        if linkedin:
            _linkedin_fleet(connection)
        connection.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status,
                 requested_at, max_attempts, cancel_requested_at)
            VALUES (:id, :u, :u, :kind, 'assigned', :t, :m,
                    CASE WHEN :cancelled THEN :t ELSE NULL END)
        """), {"id": request_id,
               "u": URL,
               "kind": kind, "t": NOW, "m": max_attempts,
               "cancelled": cancelled})
        connection.execute(text(
            "UPDATE scrape_request SET tempo=:tempo WHERE id=:id"),
            {"tempo": tempo, "id": request_id})
        attempt_id = connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            VALUES (:r, 1, :t, 'supervisor-public', 'worker-public',
                    NULL, NULL)
            RETURNING id
        """), {"r": request_id, "t": NOW}).scalar_one()
    return request_id, attempt_id
