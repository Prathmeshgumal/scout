"""Assigned workers execute query-shaped Google requests on shared profiles."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.store.ids import REQUEST, new_id

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def _assigned_google(connection):
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-google', 'Google', 'active', 'instance-google',
                'host-google', 'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at, capabilities)
        VALUES ('worker-google', 'worker-google', 'shared-profile', 'enabled',
                'healthy', :now, '["google_search"]'::jsonb)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker_google_policy (worker_id)
        VALUES ('worker-google')
    """))
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, state, max_attempts_24h,
             min_interval_seconds, created_at, updated_at)
        VALUES ('la-google', 'Shared', 'shared@example.com',
                'https://www.linkedin.com/in/shared/', 'enabled', 20, 60,
                :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO account_worker_assignment
            (id, account_id, worker_id, state, assigned_at, verified_at,
             verified_host)
        VALUES ('as-google', 'la-google', 'worker-google', 'ready', :now,
                :now, 'www.linkedin.com')
    """), {"now": NOW})
    request_id = new_id(REQUEST)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:request, 'Enterprise AI', 'enterprise ai', 2,
                'google_search', 'assigned', :now)
    """), {"request": request_id, "now": NOW})
    attempt_id = connection.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
             account_id, assignment_id)
        VALUES (:request, 1, :now, 'worker-google', 'worker-google',
                'la-google', 'as-google')
        RETURNING id
    """), {"request": request_id, "now": NOW}).scalar_one()
    return request_id, attempt_id


@contextmanager
def _page_session(*, profile, **options):
    assert profile == "shared-profile"
    yield object()


class FakeStore:
    def __init__(self, *, fail_extraction=False):
        self.objects: dict[str, bytes] = {}
        self.fail_extraction = fail_extraction

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        if self.fail_extraction and key.startswith("extractions/"):
            return None
        return len(self.objects[key]) if key in self.objects else None

    def get(self, key):
        return self.objects[key]


def _scripted_recipe(connection, visible, *, second_failure=None,
                     request_failure=None, crash_after_serp=False):
    from career_lens.capture.writer import Document
    from career_lens.google.browser import BrowserEnvironment, SearchResult
    from career_lens.google.recipe import CapturedPage, ObservedResult, RecipeResult

    def durable(document, capture_id, *, mode, serp=False):
        return CapturedPage(
            capture_id=capture_id, mode=mode, status="succeeded",
            url_requested=document.url_requested,
            url_final=document.url_final, host=document.host,
            http_status=document.http_status, title=document.title,
            captured_at=document.captured_at.isoformat(), text=document.text,
            text_characters=len(document.text or ""),
            completeness={
                "scroll_stopped": "not_scrolled" if serp else "bottom",
                "scroll_growing": False,
            },
        )

    def run(page, *, query, top_n, before_search, linkedin_host,
            persist, **options):
        before_search("www.google.com")
        visible.append(("charged", connection.execute(text(
            "SELECT count(*) FROM worker_google_event"
        )).scalar_one()))
        visible.append("navigation")
        serp_document = Document(
            section="serp", url_requested="https://www.google.com/search?q=ai",
            url_final="https://www.google.com/search?q=ai",
            host="www.google.com", http_status=200, captured_at=NOW,
            title="AI - Google Search", html=b"<html>serp</html>",
            text="complete rendered SERP text", scroll_stopped="not_scrolled",
        )
        serp_id = persist(serp_document)
        visible.append(connection.execute(text(
            "SELECT count(*) FROM capture")).scalar_one())
        if crash_after_serp:
            raise RuntimeError("browser process stopped")
        serp = durable(serp_document, serp_id, mode="generic", serp=True)
        if request_failure is not None:
            return RecipeResult(
                serp=serp, results=(), google_host="www.google.com",
                serp_url=serp_document.url_final,
                browser_environment=BrowserEnvironment("en-IN", "Asia/Kolkata"),
                searched_at=NOW.isoformat(), failure={
                    "kind": request_failure,
                    "detail": "Google challenge evidence",
                })

        observations = []
        urls = (
            "https://example.com/article",
            "https://www.linkedin.com/in/jane/",
        )
        for rank, target in enumerate(urls, 1):
            mode = "linkedin_shallow" if "linkedin.com" in target else "generic"
            result = SearchResult(
                rank, target, target, target.split("/")[2],
                f"Result {rank}", f"Snippet {rank}")
            if rank == 2 and second_failure is not None:
                failed = CapturedPage(
                    capture_id=None, mode=mode, status="failed",
                    url_requested=target, url_final=None, host=result.host,
                    http_status=None, title=None, captured_at=None, text=None,
                    text_characters=0,
                    completeness={
                        "scroll_stopped": "not_started",
                        "scroll_growing": False,
                    },
                    error={"kind": second_failure, "detail": "could not open"},
                )
                observations.append(ObservedResult(
                    result, True, "top_n", failed))
                continue
            if mode == "linkedin_shallow":
                assert linkedin_host() == "www.linkedin.com"
            document = Document(
                section=f"result-{rank:03d}", url_requested=target,
                url_final=target, host=result.host, http_status=200,
                captured_at=NOW, title=f"Result {rank}",
                html=f"<html>{rank}</html>".encode(),
                text=f"complete rendered destination {rank}",
                scroll_stopped="bottom",
            )
            capture_id = persist(document)
            visible.append(connection.execute(text(
                "SELECT count(*) FROM capture")).scalar_one())
            observations.append(ObservedResult(
                result, True, "top_n",
                durable(document, capture_id, mode=mode)))
        return RecipeResult(
            serp=serp, results=tuple(observations),
            google_host="www.google.com", serp_url=serp_document.url_final,
            browser_environment=BrowserEnvironment("en-IN", "Asia/Kolkata"),
            searched_at=NOW.isoformat())

    return run


def test_assigned_google_request_uses_query_worker_branch(
        app_url, app, monkeypatch):
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    calls = []

    def work_google(row, **options):
        calls.append((row.query_requested, row.query_normalised,
                      row.google_top_n, options))
        return "ex-google"

    monkeypatch.setattr(tasks, "_work_google", work_google, raising=False)
    monkeypatch.setattr(
        tasks, "_work",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Google work must not enter the URL recipe")),
    )

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=_page_session,
        observer=lambda page: (_ for _ in ()).throw(
            AssertionError("fresh identity must not be reverified")))

    assert len(calls) == 1
    query, normalised, top_n, options = calls[0]
    assert (query, normalised, top_n) == (
        "Enterprise AI", "enterprise ai", 2)
    assert options["browser_profile"] == "shared-profile"
    assert options["database_url"] == app_url
    assert options["attempt_id"] == attempt_id
    assert app.execute(text("""
        SELECT status FROM scrape_request WHERE id=:request
    """), {"request": request_id}).scalar_one() == "succeeded"
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event WHERE attempt_id=:attempt
    """), {"attempt": attempt_id}).scalar_one() == 0


def test_linkedin_assigned_work_keeps_url_recipe_and_gets_budget_callback(
        app_url, monkeypatch):
    """The shared worker still runs the existing URL path unchanged."""
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
        connection.execute(text("""
            UPDATE scrape_request
            SET query_requested=NULL, query_normalised=NULL, google_top_n=NULL,
                url_requested='https://www.linkedin.com/in/jane/',
                url_normalised='https://www.linkedin.com/in/jane/',
                kind='linkedin_person'
            WHERE id=:request
        """), {"request": request_id})
    calls = []

    def work(*args, **kwargs):
        calls.append((args, kwargs))
        tasks._finish(args[4], args[0], args[5])

    monkeypatch.setattr(tasks, "_work", work)
    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=_page_session,
        observer=lambda page: None)

    assert calls[0][0][2] == "https://www.linkedin.com/in/jane/"
    assert callable(calls[0][1]["before_google_search"])


def test_google_work_charges_then_persists_each_capture_and_terminal_json(
        app_url, app, monkeypatch):
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    store = FakeStore()
    visible = []
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(recipe, "run", _scripted_recipe(app, visible))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    assert visible == [("charged", 1), "navigation", 1, 2, 3]
    assert app.execute(text("""
        SELECT count(*) FROM worker_google_event
        WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 1
    row = app.execute(text("""
        SELECT request.status, extraction.outcome, extraction.document_key
        FROM scrape_request request
        JOIN extraction ON extraction.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    document = json.loads(store.objects[row.document_key])
    assert row.status == "succeeded" and row.outcome == "ok"
    assert document["serp"]["text"] == "complete rendered SERP text"
    assert [item["capture"]["mode"] for item in document["results"]] == [
        "generic", "linkedin_shallow"]
    assert [item["capture"]["text"] for item in document["results"]] == [
        "complete rendered destination 1",
        "complete rendered destination 2",
    ]
    assert app.execute(text("""
        SELECT section FROM extraction_input input
        JOIN extraction ON extraction.id=input.extraction_id
        WHERE extraction.request_id=:request ORDER BY section
    """), {"request": request_id}).scalars().all() == [
        "result-001", "result-002", "serp"]
    phases = set(app.execute(text("""
        SELECT phase FROM attempt_event WHERE attempt_id=:attempt
    """), {"attempt": attempt_id}).scalars())
    assert {"google budget", "google json validate",
            "google extraction upload"} <= phases


def test_destination_failure_stores_partial_document_but_request_succeeds(
        app_url, app, monkeypatch):
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    store = FakeStore()
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(
        recipe, "run", _scripted_recipe(app, [], second_failure="search_budget"))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, extraction.outcome, extraction.document_key
        FROM scrape_request request JOIN extraction
          ON extraction.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    document = json.loads(store.objects[row.document_key])
    assert (row.status, row.outcome) == ("succeeded", "partial")
    assert document["results"][1]["capture"]["error"]["kind"] == (
        "search_budget")


def test_readable_google_block_stores_evidence_and_earns_another_attempt(
        app_url, app, monkeypatch):
    """A Google block retries without blaming the assigned LinkedIn account.

    The partial extraction is kept either way: a readable SERP is evidence
    whether or not the next attempt succeeds.
    """
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    store = FakeStore()
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(
        recipe, "run", _scripted_recipe(app, [], request_failure="blocked"))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, attempt.cause, extraction.outcome,
               extraction.document_key, account.state, assignment.state
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN extraction ON extraction.request_id=request.id
        JOIN linkedin_account account ON account.id=attempt.account_id
        JOIN account_worker_assignment assignment
          ON assignment.id=attempt.assignment_id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause, row.outcome) == (
        "queued", "blocked", "partial")
    assert (row[4], row[5]) == ("enabled", "ready")
    assert json.loads(store.objects[row.document_key])["serp"]["capture_id"]


def test_public_google_block_requeues_without_touching_linkedin(
        app_url, app, monkeypatch):
    """A SERP-only Google wall belongs to the search worker, not to a
    LinkedIn identity the attempt does not carry."""
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    now = datetime.now(UTC)
    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
        connection.execute(text("""
            UPDATE scrape_request SET google_top_n=0 WHERE id=:request
        """), {"request": request_id})
        connection.execute(text("""
            UPDATE scrape_attempt SET account_id=NULL, assignment_id=NULL
            WHERE id=:attempt
        """), {"attempt": attempt_id})
        connection.execute(
            text("UPDATE supervisor SET last_heartbeat_at=:now"), {"now": now})
        connection.execute(
            text("UPDATE worker SET last_heartbeat_at=:now"), {"now": now})

    store = FakeStore()
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(
        recipe, "run", _scripted_recipe(app, [], request_failure="blocked"))

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, attempt.cause, account.state, assignment.state
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        CROSS JOIN linkedin_account account
        JOIN account_worker_assignment assignment
          ON assignment.account_id=account.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause) == ("queued", "blocked")
    assert (row[2], row[3]) == ("enabled", "ready")


def test_linkedin_destination_signout_schedules_linkedin_recovery(
        app_url, app, monkeypatch):
    from career_lens.capture.failures import ProviderCaptureFailed
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)

    monkeypatch.setattr(tasks, "open_store", lambda: FakeStore())
    monkeypatch.setattr(
        recipe, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ProviderCaptureFailed(
                "linkedin", "login_required", "LinkedIn signed out")))

    tasks.run_assigned(
        attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, attempt.cause, account.recovery_phase,
               assignment.state, assignment.operator_attention_code
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN linkedin_account account ON account.id=attempt.account_id
        JOIN account_worker_assignment assignment
          ON assignment.id=attempt.assignment_id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause) == ("queued", "login_required")
    assert row.recovery_phase == "before_relogin"
    assert row.state == "attention_required"
    assert row.operator_attention_code == "linkedin_signed_out"


def test_a_google_block_on_the_last_attempt_fails_the_request(
        app_url, app, monkeypatch):
    """The same failure at the ceiling. Retrying is the policy's answer while
    attempts remain; once they are spent the request stops for good, and the
    evidence it accumulated on the way is still there."""
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
        connection.execute(text("""
            UPDATE scrape_request SET max_attempts=1 WHERE id=:request
        """), {"request": request_id})
    store = FakeStore()
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(
        recipe, "run", _scripted_recipe(app, [], request_failure="blocked"))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, request.next_attempt_at, attempt.cause,
               extraction.outcome
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        JOIN extraction ON extraction.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause, row.outcome) == (
        "failed", "blocked", "partial")
    assert row.next_attempt_at is None


def test_google_extraction_verification_failure_keeps_captures_only(
        app_url, app, monkeypatch):
    """A store that would not take the document leaves the captures behind and
    no extraction at all. `storage_error` is transient, so the request waits
    out a backoff and tries again rather than stopping on a blob-store blip."""
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    store = FakeStore(fail_extraction=True)
    monkeypatch.setattr(tasks, "open_store", lambda: store)
    monkeypatch.setattr(recipe, "run", _scripted_recipe(app, []))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, request.next_attempt_at, attempt.cause
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause) == ("queued", "storage_error")
    assert row.next_attempt_at is not None
    assert app.execute(text("""
        SELECT count(*) FROM capture WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 3
    assert app.execute(text("""
        SELECT count(*) FROM extraction WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0


def test_policy_exhausted_after_dispatch_causes_no_navigation_or_evidence(
        app_url, app, monkeypatch):
    from career_lens.fleet import retry
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
        connection.execute(text("""
            UPDATE worker_google_policy SET enabled=false
            WHERE worker_id='worker-google'
        """))
    visible = []
    monkeypatch.setattr(tasks, "open_store", lambda: FakeStore())
    monkeypatch.setattr(recipe, "run", _scripted_recipe(app, visible))

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    assert visible == []
    assert app.execute(text("""
        SELECT status FROM scrape_request WHERE id=:request
    """), {"request": request_id}).scalar_one() == "queued"
    assert app.execute(text("""
        SELECT count(*) FROM capture WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0
    assert app.execute(text("""
        SELECT count(*) FROM extraction WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0
    assert retry.attempts_spent(app, request_id) == 0


def test_linkedin_destination_recheck_returns_verified_host_only_when_ready(
        app_url, app):
    from datetime import timedelta
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _, attempt_id = _assigned_google(connection)
    assert tasks._linkedin_destination_host(
        app_url, attempt_id) == "www.linkedin.com"

    # Offset from the real clock, not from the module's fixed NOW: this recheck
    # runs immediately before traffic and reads the wall clock rather than a
    # time the caller passes in. Against NOW the cooldown expires an hour after
    # the tests were written, and the assertion silently inverts.
    app.execute(text("""
        UPDATE linkedin_account SET cooldown_until=:future
        WHERE id='la-google'
    """), {"future": datetime.now(UTC) + timedelta(hours=1)})
    assert tasks._linkedin_destination_host(app_url, attempt_id) is None

    app.execute(text("""
        UPDATE linkedin_account SET cooldown_until=NULL,
            state='attention_required', operator_attention_code='manual_hold'
        WHERE id='la-google'
    """))
    assert tasks._linkedin_destination_host(app_url, attempt_id) is None


def test_optional_linkedin_discovery_charges_the_google_budget(
        app_url, app, monkeypatch):
    from career_lens.capture import driver
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
        connection.execute(text("""
            UPDATE scrape_request
            SET query_requested=NULL, query_normalised=NULL, google_top_n=NULL,
                url_requested='https://www.linkedin.com/in/jane/',
                url_normalised='https://www.linkedin.com/in/jane/',
                kind='linkedin_person'
            WHERE id=:request
        """), {"request": request_id})

    def capture_request(*args, **kwargs):
        kwargs["before_search"]("www.google.com")
        return iter(())

    monkeypatch.setattr(tasks, "capture_request", capture_request)
    monkeypatch.setattr(tasks, "open_store", lambda: FakeStore())
    monkeypatch.setattr(driver, "LINKEDIN_GOOGLE_SEARCH_CHANCE", 1.0)
    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    assert app.execute(text("""
        SELECT count(*) FROM worker_google_event
        WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 1
    assert app.execute(text("""
        SELECT count(*) FROM linkedin_account_event
        WHERE attempt_id=:attempt AND kind='attempt_started'
    """), {"attempt": attempt_id}).scalar_one() == 1


def test_browser_exception_keeps_verified_google_captures(
        app_url, app, monkeypatch):
    """A driver that died mid-run is machinery failing, not the page or the
    account, so the request backs off and runs again. The SERP capture it had
    already verified survives; no extraction is written from a partial run."""
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    monkeypatch.setattr(tasks, "open_store", lambda: FakeStore())
    monkeypatch.setattr(
        recipe, "run", _scripted_recipe(app, [], crash_after_serp=True))

    with pytest.raises(RuntimeError, match="browser process stopped"):
        tasks.run_assigned(
            attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, attempt.cause
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause) == ("queued", "browser_error")
    assert app.execute(text("""
        SELECT count(*) FROM capture WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 1
    assert app.execute(text("""
        SELECT count(*) FROM extraction WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0


def test_a_cancelled_google_search_stops_after_its_current_capture(
        app_url, app, monkeypatch):
    """Cancelling has to mean the same thing for every kind.

    A Google search runs through `_work_google` rather than `_work`, so the
    poll that stops a LinkedIn capture does not reach it. Without its own poll
    at the same boundary, a cancelled search would keep fetching destinations
    and only stop when the recipe ran out of them.
    """
    from career_lens.google import recipe
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        request_id, attempt_id = _assigned_google(connection)
    visible = []
    scripted = _scripted_recipe(app, visible)

    def run(page, **options):
        # Requested while the SERP is being fetched, not before the run: an
        # intent already set when the worker picks the job up is caught by
        # run_assigned before a browser opens, which is a different path.
        app.execute(text(
            "UPDATE scrape_request SET cancel_requested_at=now() WHERE id=:r"),
            {"r": request_id})
        app.commit()
        return scripted(page, **options)

    monkeypatch.setattr(tasks, "open_store", lambda: FakeStore())
    monkeypatch.setattr(recipe, "run", run)

    tasks.run_assigned(attempt_id, url=app_url, session_factory=_page_session)

    row = app.execute(text("""
        SELECT request.status, attempt.cause
        FROM scrape_request request
        JOIN scrape_attempt attempt ON attempt.request_id=request.id
        WHERE request.id=:request
    """), {"request": request_id}).one()
    assert (row.status, row.cause) == ("cancelled", "cancelled")
    # The SERP it had already fetched is kept; nothing after it was captured.
    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == 1
    assert app.execute(text(
        "SELECT count(*) FROM extraction WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == 0


def test_linkedin_destination_recheck_accepts_an_unknown_worker(app_url, app):
    """A native worker rests at `unknown`; traffic must not be vetoed for it."""
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _, attempt_id = _assigned_google(connection)
    app.execute(text("UPDATE worker SET health='unknown'"))

    assert tasks._linkedin_destination_host(
        app_url, attempt_id) == "www.linkedin.com"


def test_linkedin_destination_recheck_still_refuses_a_held_worker(
        app_url, app):
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        _, attempt_id = _assigned_google(connection)
    app.execute(text("""
        UPDATE worker
        SET health='attention_required',
            operator_attention_code='worker_exited',
            attention_recorded_at=now()
    """))

    assert tasks._linkedin_destination_host(app_url, attempt_id) is None
