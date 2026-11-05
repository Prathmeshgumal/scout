"""Fleet-dispatched attempts own budget, captures, and extractions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text

from career_lens.store.ids import REQUEST, new_id

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def _assigned(connection) -> tuple[str, int]:
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:request, 'https://example.com/a', 'https://example.com/a',
                'generic', 'assigned', :now)
    """), {"request": request_id, "now": NOW})
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('supervisor-a', 'Laptop', 'active', 'instance-a', 'host-a',
                'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health)
        VALUES ('worker-a', 'supervisor-a', 'research', 'enabled', 'healthy')
    """))
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-a', 'Research', 'a@example.com', 'enabled', 20, 60,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at)
        VALUES ('as-a', 'la-a', 'worker-a', 'ready', :now, :now)
    """), {"now": NOW})
    attempt_id = connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, claim_deadline_at,
             supervisor_id, worker_id, account_id, assignment_id)
        VALUES (:request, 1, :now, :now, 'supervisor-a', 'worker-a', 'la-a', 'as-a')
        RETURNING id
    """), {"request": request_id, "now": NOW}).scalar_one()
    return request_id, attempt_id


def test_start_claims_an_assigned_attempt_and_consumes_budget_once(app_url):
    from career_lens.journal import Journal
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned(connection)
        connection.execute(text("""
            UPDATE scrape_request SET kind='linkedin_person',
                url_requested='https://www.linkedin.com/in/alice/',
                url_normalised='https://www.linkedin.com/in/alice/'
            WHERE id=:id
        """), {"id": request_id})
        first = Journal.start(connection, attempt_id, url=app_url, now=NOW)
    with transaction(app_url) as connection:
        second = Journal.start(connection, attempt_id, url=app_url, now=NOW)
        state = connection.execute(text("""
            SELECT r.status, a.started_at
            FROM scrape_request r JOIN scrape_attempt a ON a.request_id = r.id
            WHERE r.id=:request
        """), {"request": request_id}).one()
        events = connection.execute(text("""
            SELECT count(*) FROM linkedin_account_event
            WHERE attempt_id=:attempt AND kind='attempt_started'
        """), {"attempt": attempt_id}).scalar_one()
        first.close(connection)
        second.close(connection)

    assert state.status == "running"
    assert state.started_at == NOW
    assert events == 1


def test_capture_accepts_attempt_attribution(app_url):
    from career_lens.capture.writer import Document, write_capture
    from career_lens.store.db import transaction

    class Store:
        def __init__(self):
            self.objects = {}

        def put(self, key, data, content_type):
            self.objects[key] = data

        def head(self, key):
            return len(self.objects[key])

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned(connection)
    capture_id = write_capture(
        app_url, request_id=request_id, attempt_id=attempt_id,
        document=Document(
            section="profile", url_requested="https://example.com/a",
            url_final="https://example.com/a", host="example.com",
            http_status=200, captured_at=NOW, title="A", html=b"<p>A</p>"),
        store=Store())
    from career_lens.store.db import connect
    connection = connect(app_url)
    try:
        assert connection.execute(text(
            "SELECT attempt_id FROM capture WHERE id=:id"),
            {"id": capture_id}).scalar_one() == attempt_id
    finally:
        connection.close()


def test_the_public_projection_still_names_no_worker_at_all(app_url):
    from career_lens.api.serialise import scrape_request
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, _ = _assigned(connection)
        projected = scrape_request(connection, request_id)

    attempt = projected["attempts"][0]
    assert "fleet" not in attempt
    assert "worker" not in attempt
