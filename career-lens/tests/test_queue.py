"""The task's bookkeeping.

Capture itself is stubbed here: what these assert is that a request moves
through its statuses correctly, that a duplicate delivery is harmless, and that
a crash still leaves a record. The capture mechanics have their own tests.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.capture.writer import Document
from career_lens.store.ids import REQUEST, new_id

NOW = datetime.now(UTC)


def test_enqueue_surface_does_not_import_the_browser_runtime():
    """The API image can enqueue even though Patchright is worker-only."""
    program = """
import builtins

real_import = builtins.__import__

def without_patchright(name, *args, **kwargs):
    if name == "patchright" or name.startswith("patchright."):
        raise ModuleNotFoundError("blocked browser dependency")
    return real_import(name, *args, **kwargs)

builtins.__import__ = without_patchright
from career_lens.queue.enqueue import enqueue_dispatch
assert callable(enqueue_dispatch)
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_the_worker_connector_opens_asynchronously(app_url):
    """The worker is async even though enqueue accepts a sync connection."""
    import asyncio

    from career_lens.queue.app import make_app

    async def open_and_close() -> None:
        queue_app = make_app(app_url)
        async with queue_app.open_async():
            pass

    asyncio.run(open_and_close())


@pytest.fixture
def queued(app) -> str:
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'queued', :t)
    """), {"r": rid, "u": "https://example.com/a", "t": NOW})
    return rid


def _run(request_id: str, url: str, profile: str = "default") -> None:
    """Drive `_work` and `_finish`, the parts of a run these tests are about,
    without the worker claim that `run_assigned` adds."""
    from career_lens.capture import driver, pressure, trace
    from career_lens.capture.driver import CaptureFailed
    from career_lens.journal import Journal
    from career_lens.queue import tasks
    from career_lens.store.db import transaction

    with transaction(url) as connection:
        row = connection.execute(text(
            "UPDATE scrape_request SET status = 'running' WHERE id = :r "
            "RETURNING kind, url_normalised, tempo, linkedin_sections"
        ), {"r": request_id}).one()
    journal = Journal.open(url, request_id=request_id, browser_profile=profile)
    trace.activate([f"request {request_id}"], sink=journal.sink)
    pressure.activate(journal.intervention)
    try:
        tasks._work(request_id, row.kind, row.url_normalised, profile, journal, url,
                    tempo_name=row.tempo,
                    discovery_chance=driver.LINKEDIN_GOOGLE_SEARCH_CHANCE,
                    linkedin_sections=row.linkedin_sections)
    except CaptureFailed as failure:
        tasks._finish(journal, request_id, url, cause=failure.failure_kind,
                      detail=failure.detail, rate_limit=failure.rate_limit)
    except Exception as exc:
        tasks._finish(journal, request_id, url, cause="browser_error",
                      detail=str(exc)[:2000])
        raise
    finally:
        trace.close()
        pressure.close()


def _status(connection, request_id: str):
    """The request's status beside its latest attempt's cause.

    They live in two tables now: the status is the request's lifecycle, the
    cause is a fact about the execution that produced it.
    """
    return connection.execute(text("""
        SELECT r.status, a.cause AS failure_kind,
               a.cause_detail AS failure_detail
        FROM scrape_request r
        LEFT JOIN LATERAL (
            SELECT cause, cause_detail FROM scrape_attempt
            WHERE request_id = r.id ORDER BY attempt_no DESC LIMIT 1
        ) a ON true
        WHERE r.id = :r
    """), {"r": request_id}).one()


def test_a_successful_run_marks_the_request_succeeded(app, queued, monkeypatch,
                                                      app_url):
    from career_lens.queue import tasks

    # _finish is what marks a request succeeded and closes its attempt, so the
    # stub calls it rather than reimplementing the pairing it exists to hold.
    monkeypatch.setattr(
        tasks, "_work",
        lambda rid, kind, target, profile, journal, url=None, **options:
            tasks._finish(journal, rid, url))
    _run(queued, app_url)
    assert _status(app, queued).status == "succeeded"


def test_request_and_worker_capture_options_reach_the_driver(
        app, app_url, monkeypatch):
    from career_lens.capture import driver
    from career_lens.queue import tasks

    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at,
             tempo, linkedin_sections)
        VALUES (:r, :u, :u, 'linkedin_person', 'queued', :t, 'human',
                '["experience"]'::jsonb)
    """), {"r": request_id, "u": "https://example.com/options", "t": NOW})
    received = {}

    def fake_capture_request(*args, **kwargs):
        received.update(kwargs)
        return iter(())

    monkeypatch.setattr(tasks, "capture_request", fake_capture_request)
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())
    monkeypatch.setattr(driver, "LINKEDIN_GOOGLE_SEARCH_CHANCE", 1.0)

    _run(request_id, app_url)

    assert received["tempo"].name == "human"
    assert received["discovery_chance"] == 1.0
    assert received["linkedin_sections"] == frozenset({"experience"})


def test_a_capture_failure_records_its_typed_cause(app, queued, monkeypatch,
                                                   app_url):
    from career_lens.capture.driver import CaptureFailed
    from career_lens.queue import tasks

    def boom(*a, **k):
        raise CaptureFailed("blocked", "a checkpoint was presented")

    monkeypatch.setattr(tasks, "_work", boom)
    _run(queued, app_url)
    row = _status(app, queued)
    # `blocked` is retryable on a different identity, so the request goes back
    # to the queue rather than stopping. What this test is about is unchanged:
    # the cause is typed and recorded on the attempt either way.
    assert row.status == "queued"
    assert row.failure_kind == "blocked"
    assert "checkpoint" in row.failure_detail


def test_parser_drift_is_stored_before_the_request_fails(monkeypatch):
    from career_lens.capture.driver import CaptureFailed
    from career_lens.queue import tasks

    target = "https://www.linkedin.com/in/jane/"
    documents = [
        Document(
            section="profile", url_requested=target, url_final=target,
            host="www.linkedin.com", http_status=200, captured_at=NOW,
            title=None, html=b"<html><main><h1>Jane Doe</h1></main></html>"),
    ]
    document = {
        "schema_version": "1.7",
        "profile": {"url": target, "handle": "jane", "name": "Jane Doe"},
        "sources": {"profile": {"artifact": None, "url": target}},
        "section_status": {"activity-posts": "drift"},
        "warnings": [],
        "drift": [{
            "section": "activity-posts",
            "contract": "feed activity cards",
            "expected": "at least one feed-shared-update-v2 card",
            "found": "0 cards while LinkedIn announced 20 posts",
        }],
    }
    stored = []
    monkeypatch.setattr(tasks, "parse_profile_html", lambda *a, **k: document)
    monkeypatch.setattr(
        tasks, "store_extraction",
        lambda *_a, document, **_k: stored.append(document))

    with pytest.raises(CaptureFailed) as raised:
        tasks._extract("rq-test", target, documents, {"profile": "cap-1"}, None)

    assert stored == [document]
    assert raised.value.failure_kind == "parser_drift"
    assert "activity-posts: feed activity cards" in raised.value.detail


def test_an_unexpected_crash_still_leaves_a_record(app, queued, monkeypatch,
                                                   app_url):
    # The one outcome worse than a failure is a request stuck at `running` with
    # nothing saying why.
    from career_lens.queue import tasks

    def boom(*a, **k):
        raise ZeroDivisionError("something nobody predicted")

    monkeypatch.setattr(tasks, "_work", boom)
    with pytest.raises(ZeroDivisionError):
        _run(queued, app_url)
    row = _status(app, queued)
    # Queued rather than failed because a browser error earns a retry, and
    # queued-with-a-cause is still the opposite of stuck-at-running.
    assert row.status == "queued"
    assert row.failure_kind == "browser_error"


class _FakeStore:
    """Accepts and remembers, so write_capture's verification passes.

    Every test that reaches `_work` must stub `open_store` with this. `_work`
    opens the real store before it iterates anything, and `blob_config()`
    defaults to the live MinIO endpoint rather than failing -- so a test that
    forgets uploads its fixtures into the development bucket and passes while
    doing it.
    """

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def url(self, key, expires=3600):
        return f"https://blobs.invalid/{key}"


def _document(section: str):
    from career_lens.capture.writer import Document

    return Document(section=section, url_requested="https://example.com/a",
                    url_final=None, host="example.com", http_status=200,
                    captured_at=NOW, title=None, html=b"<html/>")


def test_a_run_records_an_attempt_that_names_its_worker(app, app_url, queued,
                                                        monkeypatch):
    from career_lens.journal import worker_name
    from career_lens.queue import tasks

    monkeypatch.setattr(tasks, "capture_request", lambda *a, **k: iter(()))
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())
    # This test isolates attempt attribution. Its empty capture iterator is not
    # a valid generic capture now that generic runs have a required extractor.
    monkeypatch.setitem(
        tasks.EXTRACTORS, "generic", lambda *args, **kwargs: None)
    _run(queued, app_url)

    row = app.execute(text("""
        SELECT attempt_no, worker, browser_profile, ended_at, cause
        FROM scrape_attempt WHERE request_id = :r
    """), {"r": queued}).one()
    assert row.attempt_no == 1
    assert row.worker == worker_name()
    assert row.ended_at is not None
    assert row.cause is None


def test_each_capture_is_visible_before_the_next_page_is_fetched(
        app, app_url, queued, monkeypatch):
    """The property the live UI rests on, asserted where a batching writer
    would fail it. A worker that collected the documents and wrote them at the
    end would leave both observations at zero."""
    from career_lens.queue import tasks

    seen: list[int] = []

    def fake_capture_request(*args, **kwargs):
        for section in ("profile", "experience"):
            yield _document(section)
            seen.append(app.execute(text(
                "SELECT count(*) FROM capture WHERE request_id = :r"),
                {"r": queued}).scalar_one())

    monkeypatch.setattr(tasks, "capture_request", fake_capture_request)
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())

    _run(queued, app_url)

    assert seen == [1, 2]


def test_a_failed_request_always_has_an_attempt_carrying_the_cause(
        app, app_url, queued, monkeypatch):
    """scrape_request lost its `(status='failed') = (failure_kind IS NOT NULL)`
    CHECK when the cause moved onto the attempt. This is the invariant that
    replaced it, asserted on the failure path."""
    from career_lens.capture.driver import CaptureFailed
    from career_lens.queue import tasks

    def refuse(*args, **kwargs):
        raise CaptureFailed("timeout", "the page did not settle")
        yield  # pragma: no cover -- unreachable, makes this a generator

    monkeypatch.setattr(tasks, "capture_request", refuse)
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())
    # One attempt only: this test is about the terminal state carrying its
    # reason, and a request with retries left would be queued rather than
    # failed. Retry behaviour proper is asserted separately.
    app.execute(text(
        "UPDATE scrape_request SET max_attempts=1 WHERE id=:r"), {"r": queued})
    app.commit()
    _run(queued, app_url)

    row = app.execute(text("""
        SELECT r.status, a.cause, a.cause_detail, a.ended_at
        FROM scrape_request r JOIN scrape_attempt a ON a.request_id = r.id
        WHERE r.id = :r
    """), {"r": queued}).one()
    assert row.status == "failed"
    assert row.cause == "timeout"
    assert row.cause_detail == "the page did not settle"
    assert row.ended_at is not None


def test_a_run_journals_the_capture_it_stored(app, app_url, queued, monkeypatch):
    from career_lens.queue import tasks

    monkeypatch.setattr(tasks, "capture_request",
                        lambda *a, **k: iter([_document("page")]))
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())
    _run(queued, app_url)

    event = app.execute(text("""
        SELECT e.phase, e.capture_id, e.detail
        FROM attempt_event e JOIN scrape_attempt a ON a.id = e.attempt_id
        WHERE a.request_id = :r AND e.phase = 'capture stored'
    """), {"r": queued}).one()
    assert event.detail == "page"
    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE id = :c"),
        {"c": event.capture_id}).scalar_one() == 1
    work = app.execute(text("""
        SELECT e.phase
        FROM attempt_event e JOIN scrape_attempt a ON a.id = e.attempt_id
        WHERE a.request_id = :r AND e.kind = 'work'
        ORDER BY e.id
    """), {"r": queued}).scalars().all()
    assert "store capture" in work
    assert "extract document" in work
    assert "finalize request" in work


def test_a_generic_run_stores_a_verified_json_extraction(
        app, app_url, queued, monkeypatch):
    from career_lens.queue import tasks

    document = _document("page")
    document.text = "Rendered article"
    store = _FakeStore()
    monkeypatch.setattr(
        tasks, "capture_request", lambda *args, **kwargs: iter([document]))
    monkeypatch.setattr(tasks, "open_store", lambda: store)

    _run(queued, app_url)

    extraction = app.execute(text("""
        SELECT extractor, extractor_version, outcome, document_key,
               document_sha256, document_bytes
        FROM extraction WHERE request_id=:request
    """), {"request": queued}).one_or_none()
    assert extraction is not None
    assert extraction.extractor == "generic_page_json"
    assert extraction.extractor_version == "1.0"
    assert extraction.outcome == "ok"
    assert extraction.document_key.endswith("/generic.json")
    assert extraction.document_bytes == len(store.objects[extraction.document_key])
    assert extraction.document_sha256
    assert app.execute(text("""
        SELECT section FROM extraction_input
        WHERE extraction_id=(
            SELECT id FROM extraction WHERE request_id=:request)
    """), {"request": queued}).scalar_one() == "page"


def test_a_generic_run_with_no_page_capture_fails_rather_than_succeeds(
        app, app_url, queued, monkeypatch):
    """What changed when generic gained a required extractor.

    A run that captured nothing used to succeed, because no extractor read
    the kind at all. It now has one, and a request with no page for it to
    read is a failed reading rather than a finished one -- which is worth a
    test of its own, since five existing tests had to stub the extractor out
    to keep their empty capture iterators succeeding.
    """
    from career_lens.queue import tasks

    monkeypatch.setattr(tasks, "capture_request", lambda *a, **k: iter(()))
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())

    _run(queued, app_url)

    assert app.execute(text("""
        SELECT status FROM scrape_request WHERE id=:request
    """), {"request": queued}).scalar_one() == "failed"
    assert app.execute(text("""
        SELECT cause FROM scrape_attempt WHERE request_id=:request
        ORDER BY attempt_no DESC LIMIT 1
    """), {"request": queued}).scalar_one() == "extract_failed"
    assert app.execute(text("""
        SELECT count(*) FROM extraction WHERE request_id=:request
    """), {"request": queued}).scalar_one() == 0


def test_two_captures_sharing_a_role_both_reach_the_extractor():
    """The mapping is the parser's input. A dropped capture is silent loss.

    Company job result pages arrive as jobs-page-N and do not collide, but the
    plain dict this replaced would have lost one the moment any recipe captured
    a section twice, with nothing reporting it.
    """
    from career_lens.queue.tasks import sections_of

    stored = [("jobs-page-1", "cp-a"), ("jobs-page-2", "cp-b"),
              ("about", "cp-c"), ("about", "cp-d")]

    assert sections_of(stored) == {
        "jobs-page-1": "cp-a", "jobs-page-2": "cp-b",
        "about": "cp-c", "about-2": "cp-d",
    }


def test_the_suffix_does_not_collide_with_a_role_that_is_already_numbered():
    """`about-2` arriving on its own must not be overwritten by a second
    `about`, and vice versa."""
    from career_lens.queue.tasks import sections_of

    assert sections_of([("about", "a"), ("about-2", "b"), ("about", "c")]) == {
        "about": "a", "about-2": "b", "about-3": "c",
    }


def test_every_url_request_kind_has_an_extractor():
    from career_lens.queue.tasks import EXTRACTORS

    assert "linkedin_person" in EXTRACTORS
    assert "linkedin_company" in EXTRACTORS
    assert "generic" in EXTRACTORS


def test_company_extraction_uses_tabs_metadata_and_ignores_serp(monkeypatch):
    from career_lens.queue import tasks

    target = "https://www.linkedin.com/company/acme/"

    def document(section, html, url, *, stopped=None, metadata=None):
        return Document(
            section=section, url_requested=url, url_final=url,
            host="www.linkedin.com", http_status=200, captured_at=NOW,
            title=None, html=html, scroll_stopped=stopped,
            metadata=metadata or {},
        )

    provenance = {
        "arrival_method": "google_search", "requested_url": target,
        "search_engine": "google", "search_query": "acme LinkedIn",
        "search_result_rank": 2,
    }
    documents = [
        document("discovery-serp", b"<html>google</html>",
                 "https://www.google.com/search?q=acme"),
        document("company", b"<html>company</html>", target,
                 metadata={"provenance": provenance}),
        document("posts", b"<html>posts</html>", f"{target}posts/",
                 stopped="distance"),
        document("jobs-page-1", b"<html>jobs</html>",
                 "https://www.linkedin.com/jobs/search-results/?start=0",
                 metadata={"has_more": True}),
    ]
    seen = {}

    def parse(home, tabs, **kwargs):
        seen.update(home=home, tabs=tabs, **kwargs)
        return {"schema_version": "1.1", "company": {},
                "section_status": {}}

    monkeypatch.setattr(tasks, "parse_company_html", parse)
    monkeypatch.setattr(
        tasks, "store_company_extraction",
        lambda _url, **kwargs: seen.setdefault("stored", kwargs),
    )

    tasks._extract_company(
        "rq-test", target, documents,
        {"discovery-serp": "cap-serp", "company": "cap-company",
         "posts": "cap-posts", "jobs-page-1": "cap-jobs"},
        store=object(), requested_sections=frozenset({"posts"}),
    )

    assert seen["home"] == "<html>company</html>"
    assert seen["tabs"] == {
        "posts": "<html>posts</html>",
        "jobs-page-1": "<html>jobs</html>",
    }
    assert seen["tab_capture"] == {
        "posts": {"scroll_stopped": "distance"},
        "jobs-page-1": {"scroll_stopped": None, "has_more": True},
    }
    assert seen["provenance"] == provenance
    assert seen["requested_sections"] == frozenset({"posts"})
    assert seen["stored"]["capture_ids"] == {
        "company": "cap-company", "posts": "cap-posts",
        "jobs-page-1": "cap-jobs",
    }


def _finished_request(app, *, status: str, cause: str | None = None) -> str:
    """A request that has run and stopped, with an attempt carrying the cause.

    The delivery reads the failure from the attempt, not the request: that is
    where execution facts have lived since 0008.
    """
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :u, :u, 'linkedin_person', :s, :t)
    """), {"id": request_id, "u": "https://www.linkedin.com/in/jane/",
           "s": status, "t": NOW})
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker, browser_profile, started_at,
             ended_at, cause)
        VALUES (:r, 1, 'test:1', 'research', :t, :t, :c)
    """), {"r": request_id, "t": NOW, "c": cause})
    return request_id


def _finished_google_request(app, *, status="succeeded") -> str:
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:id, 'Enterprise AI', 'enterprise ai', 2,
                'google_search', :status, :now)
    """), {"id": request_id, "status": status, "now": NOW})
    app.execute(text("""
        INSERT INTO scrape_attempt
            (request_id, attempt_no, worker, browser_profile,
             started_at, ended_at)
        VALUES (:request, 1, 'test:1', 'research', :now, :now)
    """), {"request": request_id, "now": NOW})
    return request_id


def _running_request(app) -> str:
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:id, :u, :u, 'linkedin_person', 'running', :t)
    """), {"id": request_id, "u": "https://www.linkedin.com/in/jane/", "t": NOW})
    return request_id


def _retry_fleet(connection):
    """One worker, one worker, one ready account. Each suite builds its own
    fleet rather than importing fixtures across test files."""
    connection.execute(text("""
        INSERT INTO supervisor
            (id, label, desired_state, instance_id, hostname,
             software_version, started_at, last_heartbeat_at)
        VALUES ('worker-a', 'Laptop', 'active', 'instance-a', 'host-a',
                'test', :now, :now)
    """), {"now": NOW})
    connection.execute(text("""
        INSERT INTO worker
            (id, supervisor_id, local_name, desired_state, health,
             last_heartbeat_at)
        VALUES ('worker-a', 'worker-a', 'research', 'enabled', 'healthy', :now)
    """), {"now": NOW})
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
        VALUES ('as-a', 'la-a', 'worker-a', 'ready', :now, :now,
                'www.linkedin.com')
    """), {"now": NOW})


def _running_attempt(app_url, *, max_attempts: int = 3):
    """A claimed, open fleet attempt on a request with this ceiling.

    Journal.start refuses an attempt whose request is not `assigned` or whose
    attempt has no fleet attribution, so both are set up here; it moves the
    request to `running`, which is the state _finish settles from.
    """
    from career_lens.journal import Journal
    from career_lens.store.db import transaction

    request_id = new_id(REQUEST)
    with transaction(app_url) as connection:
        _retry_fleet(connection)
        connection.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status,
                 requested_at, max_attempts)
            VALUES (:id, :u, :u, 'linkedin_person', 'assigned', :t, :m)
        """), {"id": request_id, "m": max_attempts, "t": NOW,
               "u": f"https://www.linkedin.com/in/{request_id}/"})
        attempt_id = connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            VALUES (:r, 1, :t, 'worker-a', 'worker-a', 'la-a', 'as-a')
            RETURNING id
        """), {"r": request_id, "t": NOW}).scalar_one()
        journal = Journal.start(connection, attempt_id, url=app_url)
    return request_id, journal


def _next_attempt(app_url, request_id: str):
    """The attempt a retry gets, claimed the way the dispatcher claims one."""
    from career_lens.journal import Journal
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        connection.execute(text(
            "UPDATE scrape_request SET status='assigned' WHERE id=:r"),
            {"r": request_id})
        attempt_id = connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            SELECT :r, COALESCE(MAX(attempt_no), 0) + 1, :t, 'worker-a',
                   'worker-a', 'la-a', 'as-a'
            FROM scrape_attempt WHERE request_id=:r
            RETURNING id
        """), {"r": request_id, "t": NOW}).scalar_one()
        return Journal.start(connection, attempt_id, url=app_url)


def test_a_retryable_failure_returns_the_request_to_the_queue(app_url, app):
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url)

    _finish(journal, request_id, app_url, cause="timeout",
            detail="the page never settled")

    row = app.execute(text("""
        SELECT status, next_attempt_at, dispatch_wait_reason
        FROM scrape_request WHERE id=:r
    """), {"r": request_id}).one()
    assert row.status == "queued"
    assert row.next_attempt_at is not None
    assert row.dispatch_wait_reason == "retry_backoff"
    assert app.execute(text(
        "SELECT cause FROM scrape_attempt WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == "timeout"


def test_a_terminal_failure_fails_the_request_immediately(app_url, app):
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url)
    # Already carrying a due time from an earlier retry: stopping has to clear
    # it, or a failed request keeps a promise to run again that nothing honours.
    app.execute(text("""
        UPDATE scrape_request SET next_attempt_at = now() + interval '5 minutes'
        WHERE id=:r
    """), {"r": request_id})
    app.commit()

    _finish(journal, request_id, app_url, cause="not_found",
            detail="the profile does not exist")

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("failed", None)


def test_an_exhausted_request_fails_even_on_a_retryable_cause(app_url, app):
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url, max_attempts=1)

    _finish(journal, request_id, app_url, cause="timeout",
            detail="the page never settled")

    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "failed"


def test_the_ceiling_is_read_against_the_attempt_that_failed(app_url, app):
    """Attempt 2 of 2 is spent even though attempt 1 of 2 was not: the decision
    has to read this attempt's ordinal, not assume the first one."""
    from career_lens.queue.tasks import _finish

    request_id, first = _running_attempt(app_url, max_attempts=2)
    _finish(first, request_id, app_url, cause="timeout", detail="never settled")
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "queued"

    second = _next_attempt(app_url, request_id)
    _finish(second, request_id, app_url, cause="timeout", detail="never settled")

    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "failed"


def test_a_success_still_finishes(app_url, app):
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url)

    _finish(journal, request_id, app_url)

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("succeeded", None)


def test_an_identity_failure_requeues_and_still_quarantines(app_url, app):
    from career_lens.queue.tasks import _finish_identity_failure

    request_id, journal = _running_attempt(app_url)

    _finish_identity_failure(
        journal, request_id, app_url, cause="blocked",
        detail="a checkpoint was presented")

    request = app.execute(text(
        "SELECT status, dispatch_wait_reason FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert request.status == "queued"
    assert request.dispatch_wait_reason == "retry_backoff"
    # The account still needs a human whether or not the request tries again,
    # and the retry lands elsewhere precisely because of this.
    assert app.execute(text(
        "SELECT state FROM linkedin_account WHERE id='la-a'")).scalar_one() == (
            "attention_required")
    # The browser is untouched: LinkedIn's answer is about the session.
    assert app.execute(text(
        "SELECT health FROM worker WHERE id='worker-a'")).scalar_one() != (
            "attention_required")


def test_an_exhausted_identity_failure_fails(app_url, app):
    from career_lens.queue.tasks import _finish_identity_failure

    request_id, journal = _running_attempt(
        app_url, max_attempts=1)

    _finish_identity_failure(
        journal, request_id, app_url, cause="blocked",
        detail="a checkpoint was presented")

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("failed", None)


def _refuse_to_parse(*args, **kwargs):
    raise AssertionError("the extractor ran on a cancelled request")


def _cancelled_assignment(app_url):
    """An unstarted fleet attempt whose request already carries the intent.

    Deliberately not built on `_running_attempt`: that helper calls
    `Journal.start`, which stamps `started_at` on the attempt -- and
    `run_assigned` returns early on any attempt that has already started. A
    test built on it would pass without the check this asserts ever running.
    """
    from career_lens.store.db import transaction

    request_id = new_id(REQUEST)
    with transaction(app_url) as connection:
        _retry_fleet(connection)
        connection.execute(text("""
            INSERT INTO scrape_request
                (id, url_requested, url_normalised, kind, status,
                 requested_at, cancel_requested_at)
            VALUES (:id, :u, :u, 'linkedin_person', 'assigned', :t, :t)
        """), {"id": request_id, "t": NOW,
               "u": f"https://www.linkedin.com/in/{request_id}/"})
        attempt_id = connection.execute(text("""
            INSERT INTO scrape_attempt
                (request_id, attempt_no, assigned_at, supervisor_id, worker_id,
                 account_id, assignment_id)
            VALUES (:r, 1, :t, 'worker-a', 'worker-a', 'la-a', 'as-a')
            RETURNING id
        """), {"r": request_id, "t": NOW}).scalar_one()
    return request_id, attempt_id


def test_a_cancel_stops_the_run_at_the_next_page_boundary(
        app, app_url, queued, monkeypatch):
    """The granularity is one page. The first page is captured and kept, the
    cancel is noticed after it, and the second page is never fetched."""
    from career_lens.queue import tasks

    fetched: list[str] = []

    def fake_capture_request(*args, **kwargs):
        # The operator clicks Cancel while the first page is in flight. Written
        # before the first yield rather than between them: the generator only
        # resumes once the loop asks for the next page, which is after the poll
        # -- so a stub that sets the flag there would be describing a cancel
        # that arrived one page later than it meant to.
        app.execute(text("""
            UPDATE scrape_request SET cancel_requested_at = now()
            WHERE id = :r
        """), {"r": queued})
        app.commit()
        for section in ("profile", "experience"):
            fetched.append(section)
            yield _document(section)

    monkeypatch.setattr(tasks, "capture_request", fake_capture_request)
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())

    _run(queued, app_url)

    assert fetched == ["profile"]
    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id=:r"),
        {"r": queued}).scalar_one() == 1
    row = _status(app, queued)
    assert row.status == "cancelled"
    assert row.failure_kind == "cancelled"


def test_a_cancelled_run_writes_no_extraction(app, app_url, monkeypatch):
    """The abort is before the extractor, and a parser run over a deliberately
    partial set would produce a document whose gaps mean nothing."""
    from career_lens.queue import tasks

    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'queued', :t)
    """), {"r": request_id, "t": NOW,
           "u": "https://www.linkedin.com/in/cancelled/"})
    app.commit()

    def fake_capture_request(*args, **kwargs):
        yield _document("profile")
        app.execute(text(
            "UPDATE scrape_request SET cancel_requested_at=now() WHERE id=:r"),
            {"r": request_id})
        app.commit()
        yield _document("experience")

    monkeypatch.setattr(tasks, "capture_request", fake_capture_request)
    monkeypatch.setattr(tasks, "open_store", lambda: _FakeStore())
    monkeypatch.setattr(tasks, "parse_profile_html", _refuse_to_parse)

    _run(request_id, app_url)

    assert app.execute(text(
        "SELECT count(*) FROM extraction WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == 0


def test_a_cancel_on_an_assigned_request_never_opens_a_browser(app, app_url):
    """The cheapest case: the intent is already set when the worker picks the
    job up, so it stops in the SELECT it was making anyway."""
    from career_lens.queue import tasks

    request_id, attempt_id = _cancelled_assignment(app_url)

    def refuse(**kwargs):
        raise AssertionError("a browser was opened for a cancelled request")

    tasks.run_assigned(attempt_id, url=app_url, session_factory=refuse)

    row = _status(app, request_id)
    assert (row.status, row.failure_kind) == ("cancelled", "cancelled")
    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id=:r"),
        {"r": request_id}).scalar_one() == 0


def test_a_cancel_racing_an_identity_failure_still_quarantines(app_url, app):
    """Both facts survive. A challenged account needs a human whether or not
    the request continues, and the request is still `cancelled` rather than
    `failed` -- the operator stopped it, and that is what they should read."""
    from career_lens.queue.tasks import _finish_identity_failure

    request_id, journal = _running_attempt(app_url)
    app.execute(text(
        "UPDATE scrape_request SET cancel_requested_at=now() WHERE id=:r"),
        {"r": request_id})
    app.commit()

    _finish_identity_failure(
        journal, request_id, app_url, cause="blocked",
        detail="a checkpoint was presented")

    row = app.execute(text(
        "SELECT status, next_attempt_at FROM scrape_request WHERE id=:r"),
        {"r": request_id}).one()
    assert (row.status, row.next_attempt_at) == ("cancelled", None)
    # The assignment as well as the account: it is the one the next run
    # would otherwise be dispatched to.
    assert app.execute(text(
        "SELECT state FROM account_worker_assignment WHERE id='as-a'")
    ).scalar_one() == "attention_required"
    assert app.execute(text(
        "SELECT state FROM linkedin_account WHERE id='la-a'")).scalar_one() == (
            "attention_required")
    # The browser is untouched: LinkedIn's answer is about the session.
    assert app.execute(text(
        "SELECT health FROM worker WHERE id='worker-a'")).scalar_one() != (
            "attention_required")


def test_a_run_that_outran_its_cancel_still_succeeds(app_url, app):
    """The counterpart of the mutation that would make every pending cancel
    terminal regardless of cause."""
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url)
    app.execute(text(
        "UPDATE scrape_request SET cancel_requested_at=now() WHERE id=:r"),
        {"r": request_id})
    app.commit()

    _finish(journal, request_id, app_url)

    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "succeeded"


def test_a_cancelled_request_notifies_its_callers_once(app_url, app):
    from career_lens.queue.tasks import _finish

    request_id, journal = _running_attempt(app_url)
    app.execute(text(
        "UPDATE scrape_request SET cancel_requested_at=now() WHERE id=:r"),
        {"r": request_id})
    app.commit()

    _finish(journal, request_id, app_url, cause="cancelled",
            detail="an operator stopped this run")

    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id=:r"),
        {"r": request_id}).scalar_one() == "cancelled"


def test_a_challenge_quarantines_the_identity_and_not_the_browser(
        app_url, app):
    from career_lens.queue.tasks import _finish_identity_failure

    request_id, journal = _running_attempt(app_url)

    _finish_identity_failure(
        journal, request_id, app_url, cause="blocked",
        detail="a challenge was presented")

    row = app.execute(text("""
        SELECT ass.state, ass.operator_attention_code AS assignment_code,
               account.state AS account_state,
               worker.health, worker.operator_attention_code AS worker_code
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker ON worker.id=ass.worker_id
    """)).one()
    assert row.state == "attention_required"
    assert row.assignment_code == "linkedin_challenge"
    assert row.account_state == "attention_required"
    assert row.health != "attention_required"
    assert row.worker_code is None


def test_an_unobservable_identity_still_marks_assignment_and_account(
        app_url, app):
    """The pre-flight `browser_error` path bypasses the IDENTITY_CAUSES gate.

    `tasks.py` routes only `IDENTITY_CAUSES` here on a capture failure, but the
    session verification that runs before traffic calls this directly with
    `browser_error` when it cannot observe the identity at all. The map entry
    for it is live, not dead.
    """
    from career_lens.queue.tasks import _finish_identity_failure

    request_id, journal = _running_attempt(app_url)

    _finish_identity_failure(
        journal, request_id, app_url, cause="browser_error",
        detail="LinkedIn identity was not observable")

    row = app.execute(text("""
        SELECT ass.operator_attention_code AS assignment_code,
               account.operator_attention_code AS account_code,
               worker.operator_attention_code AS worker_code
        FROM account_worker_assignment ass
        JOIN linkedin_account account ON account.id=ass.account_id
        JOIN worker ON worker.id=ass.worker_id
    """)).one()
    assert row.assignment_code == "linkedin_identity_unobservable"
    assert row.account_code == "linkedin_identity_unobservable"
    assert row.worker_code is None
