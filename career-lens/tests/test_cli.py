"""The CLI's error path.

`store.db._where()` keeps passwords out of the errors this project raises.
`_safe()` is the same duty on the path that prints whatever the driver raised,
which is the one nobody controls.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from career_lens.api.requests import Submission
from career_lens.auth.service import ApiKeyRecord, IssuedApiKey
from career_lens.cli import _safe


def test_a_url_in_a_driver_message_loses_its_password():
    message = _safe(RuntimeError(
        "connection failed for postgresql+psycopg://career_lens_app:hunter2@127.0.0.1:5432/career_lens"))
    assert "hunter2" not in message
    assert "127.0.0.1:5432" in message   # still says what it tried to reach


def test_a_message_with_no_url_is_returned_unchanged():
    assert _safe(RuntimeError("relation does not exist")) == (
        "relation does not exist")


def test_an_unparseable_url_still_loses_its_secret():
    # The fallback branch: whatever could not be parsed must not be echoed.
    message = _safe(RuntimeError("bad url ://:@@@:hunter2@host"))
    assert "hunter2" not in message


@contextmanager
def _fake_transaction():
    yield object()


def test_api_key_create_prints_the_plaintext_once(monkeypatch, capsys):
    from career_lens.cli import main

    issued = IssuedApiKey(
        id="ak-test", name="Customer A", prefix="cl_v1.ak-test",
        created_at=datetime(2026, 9, 21, tzinfo=UTC), revoked_at=None,
        token="cl_v1.ak-test.customer-secret",
    )
    monkeypatch.setattr("career_lens.store.db.transaction", _fake_transaction)
    monkeypatch.setattr(
        "career_lens.auth.service.create_api_key",
        lambda connection, name, now: issued,
    )

    assert main(["api-key", "create", "Customer A"]) == 0
    output = capsys.readouterr().out
    assert output.count(issued.token) == 1
    assert issued.id in output


def test_api_key_list_prints_only_safe_metadata(monkeypatch, capsys):
    from career_lens.cli import main

    class Connection:
        def close(self):
            pass

    record = ApiKeyRecord(
        id="ak-test", name="Customer A", prefix="cl_v1.ak-test",
        created_at=datetime(2026, 9, 21, tzinfo=UTC), revoked_at=None,
    )
    monkeypatch.setattr("career_lens.store.db.connect", Connection)
    monkeypatch.setattr(
        "career_lens.auth.service.list_api_keys", lambda connection: [record],
    )

    assert main(["api-key", "list"]) == 0
    output = capsys.readouterr().out
    assert "ak-test" in output
    assert "cl_v1.ak-test" in output
    assert "secret" not in output


def test_api_key_revoke_reports_the_public_id(monkeypatch, capsys):
    from career_lens.cli import main

    record = ApiKeyRecord(
        id="ak-test", name="Customer A", prefix="cl_v1.ak-test",
        created_at=datetime(2026, 9, 21, tzinfo=UTC),
        revoked_at=datetime(2026, 9, 22, tzinfo=UTC),
    )
    monkeypatch.setattr("career_lens.store.db.transaction", _fake_transaction)
    monkeypatch.setattr(
        "career_lens.auth.service.revoke_api_key",
        lambda connection, key_id, now: record,
    )

    assert main(["api-key", "revoke", "ak-test"]) == 0
    assert capsys.readouterr().out.strip() == "career_lens: revoked ak-test"


def test_api_key_database_errors_are_safe(monkeypatch, capsys):
    from career_lens.cli import main
    from career_lens.store.db import DatabaseUnavailable

    def unavailable():
        raise DatabaseUnavailable(
            "cannot reach postgresql://user:hunter2@database/career_lens",
        )

    monkeypatch.setattr("career_lens.store.db.connect", unavailable)

    assert main(["api-key", "list"]) == 1
    error = capsys.readouterr().err
    assert "hunter2" not in error
    assert "database" in error


def test_api_key_requires_a_valid_subcommand(capsys):
    from career_lens.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["api-key", "unknown"])
    assert stopped.value.code == 2


def test_supervisor_requires_a_local_declaration(monkeypatch, tmp_path, capsys):
    from career_lens.cli import main

    monkeypatch.setenv("CAREER_LENS_SUPERVISOR_CONFIG", str(tmp_path / "missing.json"))
    assert main(["supervisor"]) == 1
    assert "supervisor declaration" in capsys.readouterr().err.lower()


def test_a_declared_supervisor_runs_one_process_per_worker(monkeypatch, tmp_path):
    import json
    from career_lens.cli import main

    path = tmp_path / "worker.json"
    path.write_text(json.dumps({
        "id": "worker-a", "label": "Laptop",
        "workers": [{"id": "worker-a", "profile": "research"}],
    }))
    received = []

    class FakeSupervisor:
        def __init__(self, config, **kwargs):
            received.append((config, kwargs))
        def run(self):
            received.append("run")

    monkeypatch.setattr(
        "career_lens.fleet.supervisor.Supervisor", FakeSupervisor)
    assert main([
        "supervisor", "--config", str(path), "--company-tabs", "none",
        "--company-job-pages", "1", "--show-cursor",
        "--use-virtual-display",
        "--window-size", "1440x900", "--har", "bodies",
        "--linkedin-google-search-chance", "0.75",
    ]) == 0
    assert received[0][0].id == "worker-a"
    assert received[0][1]["company_tabs"] == "none"
    assert received[0][1]["company_job_pages"] == 1
    assert received[0][1]["show_cursor"] is True
    assert received[0][1]["use_virtual_display"] is True
    assert received[0][1]["window_size"] == (1440, 900)
    assert received[0][1]["har"] == "bodies"
    assert received[0][1]["linkedin_google_search_chance"] == 0.75
    assert received[1] == "run"


def test_the_cursor_is_off_unless_the_run_asks_for_it(monkeypatch, tmp_path):
    """Opt-in: the overlay is a debugging aid, not the default window."""
    received = []

    class FakeSupervisor:
        def __init__(self, declaration, **options):
            received.append((declaration, options))

        def run(self):
            received.append("run")
            return 0

    import json

    path = tmp_path / "worker.json"
    path.write_text(json.dumps({
        "id": "worker-a", "label": "Laptop",
        "workers": [{"id": "worker-a", "profile": "research"}],
    }))
    monkeypatch.setattr(
        "career_lens.fleet.supervisor.Supervisor", FakeSupervisor)
    from career_lens.cli import main

    assert main(["supervisor", "--config", str(path)]) == 0

    assert received[0][1]["show_cursor"] is False
    assert received[0][1]["use_virtual_display"] is False
    assert received[0][1]["window_size"] is None
    # Opt-in: request logging is evidence about a capture, not part of it.
    assert received[0][1]["har"] == "off"
    assert received[0][1]["linkedin_google_search_chance"] == 0.3


def test_the_removed_chrome_headless_option_is_rejected(capsys):
    from career_lens.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["supervisor", "--headless"])

    assert stopped.value.code == 2
    assert "unrecognized arguments: --headless" in capsys.readouterr().err


def test_supervisor_window_size_must_be_width_by_height(capsys):
    from career_lens.cli import main

    with pytest.raises(SystemExit) as stopped:
        main(["supervisor", "--window-size", "wide"])

    assert stopped.value.code == 2
    assert "must look like WIDTHxHEIGHT" in capsys.readouterr().err


def test_supervisor_registration_error_is_reported_without_a_traceback(
        monkeypatch, tmp_path, capsys):
    import json
    from career_lens.cli import main
    from career_lens.fleet.registration import SupervisorError

    path = tmp_path / "worker.json"
    path.write_text(json.dumps({
        "id": "worker-a", "label": "Laptop",
        "workers": [{"id": "worker-a", "profile": "research"}],
    }))

    class CollidingSupervisor:
        def __init__(self, config, **kwargs):
            pass

        def run(self):
            raise SupervisorError(
                "browser worker worker-a is already owned by worker worker-b")

    monkeypatch.setattr(
        "career_lens.fleet.supervisor.Supervisor", CollidingSupervisor)

    assert main(["supervisor", "--config", str(path)]) == 1
    assert capsys.readouterr().err == (
        "career_lens: browser worker worker-a is already owned by worker worker-b\n")


def test_control_worker_has_no_browser_queue(monkeypatch):
    from career_lens.cli import main
    from career_lens.queue import app as queue_app

    seen = []
    monkeypatch.setattr(queue_app.app, "run_worker",
                        lambda **kwargs: seen.append(kwargs))
    assert main(["control"]) == 0
    assert seen == [{"queues": ["control"], "name": "control"}]


def test_scrape_forwards_request_capture_options(monkeypatch, capsys):
    from career_lens.cli import main

    received = {}

    def submit(url, **kwargs):
        received.update({"url": url, **kwargs})
        return Submission("rq-options", True)

    monkeypatch.setattr("career_lens.api.requests.submit", submit)

    assert main([
        "scrape", "https://www.linkedin.com/in/jane/",
        "--tempo", "human",
        "--linkedin-sections", "about,experience",
    ]) == 0

    assert received == {
        "url": "https://www.linkedin.com/in/jane/",
        "tempo": "human",
        "linkedin_sections": "about,experience",
        # Forwarded unset rather than omitted, so the submission applies its
        # own defaults instead of the CLI inventing a second set.
        "label": None,
        "max_attempts": None,
    }
    assert "rq-options" in capsys.readouterr().out


def test_search_submits_a_query_and_reports_its_request(monkeypatch, capsys):
    from career_lens.cli import main

    received = {}

    def submit_google(query, **kwargs):
        received.update({"query": query, **kwargs})
        return Submission("rq-search", True), "smoke testing", 3

    monkeypatch.setattr("career_lens.api.requests.submit_google", submit_google)

    assert main(["search", "smoke testing", "--top-n", "3"]) == 0
    assert received == {
        "query": "smoke testing", "top_n": 3, "label": None,
        "tempo": None,
    }
    out = capsys.readouterr().out
    assert "rq-search" in out and "queued" in out
    assert "smoke testing" in out and "top 3" in out


def test_search_says_when_a_query_was_already_outstanding(monkeypatch, capsys):
    from career_lens.cli import main

    monkeypatch.setattr(
        "career_lens.api.requests.submit_google",
        lambda query, **kwargs: (Submission("rq-existing", False),
                                 query, 3))

    assert main(["search", "smoke testing"]) == 0
    assert "already outstanding" in capsys.readouterr().out


def test_search_reports_a_rejected_query_without_a_traceback(
        monkeypatch, capsys):
    from career_lens.cli import main
    from career_lens.google.contract import InvalidGoogleRequest

    def submit_google(query, **kwargs):
        raise InvalidGoogleRequest("query must not be empty")

    monkeypatch.setattr("career_lens.api.requests.submit_google", submit_google)

    assert main(["search", " "]) == 1
    assert "query must not be empty" in capsys.readouterr().err


def _answer(document, *, pending=False):
    from career_lens.google.read import SearchResult

    return SearchResult(document, pending=pending)


def _reader(monkeypatch, *answers):
    """Stand in for the shared read path, one answer per call."""
    calls = []
    remaining = list(answers)

    def search_result(connection, request_id, **kwargs):
        calls.append({"request_id": request_id, **kwargs})
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    class Connection:
        def close(self):
            calls.append({"closed": True})

    monkeypatch.setattr("career_lens.google.read.search_result", search_result)
    monkeypatch.setattr("career_lens.store.db.connect", lambda: Connection())
    return calls


def test_search_result_prints_the_canonical_document(monkeypatch, capsys):
    import json
    from career_lens.cli import main

    document = {"schema_version": "1.0", "request_id": "rq-one",
                "status": "succeeded", "results": []}
    _reader(monkeypatch, _answer(document))

    assert main(["search-result", "rq-one"]) == 0
    assert json.loads(capsys.readouterr().out) == document


def test_search_result_asks_for_screenshots_only_when_requested(monkeypatch):
    from career_lens.cli import main

    calls = _reader(monkeypatch, _answer({"request_id": "rq-one"}))

    reads = lambda: [call for call in calls if "request_id" in call]

    assert main(["search-result", "rq-one"]) == 0
    assert reads()[-1]["include_screenshots"] is False

    assert main(["search-result", "rq-one", "--screenshots"]) == 0
    assert reads()[-1]["include_screenshots"] is True


def test_search_result_says_a_running_request_is_not_finished(
        monkeypatch, capsys):
    import json
    from career_lens.cli import main

    envelope = {"request_id": "rq-one", "status": "running", "result": None}
    _reader(monkeypatch, _answer(envelope, pending=True))

    # A distinct code, because "still running" is neither a document nor a
    # failure and a script has to be able to tell the three apart.
    assert main(["search-result", "rq-one"]) == 3
    assert json.loads(capsys.readouterr().out) == envelope


def test_search_result_waits_for_a_running_request_to_settle(
        monkeypatch, capsys):
    import json
    from career_lens.cli import main

    slept = []
    monkeypatch.setattr("career_lens.cli.time.sleep", slept.append)
    calls = _reader(
        monkeypatch,
        _answer({"status": "running"}, pending=True),
        _answer({"status": "running"}, pending=True),
        _answer({"status": "succeeded", "results": []}))

    assert main(["search-result", "rq-one", "--wait"]) == 0
    assert len([call for call in calls if "request_id" in call]) == 3
    assert slept, "waiting must not spin the database"
    assert json.loads(capsys.readouterr().out)["status"] == "succeeded"


def test_search_result_gives_up_waiting_at_the_timeout(monkeypatch, capsys):
    from career_lens.cli import main

    monkeypatch.setattr("career_lens.cli.time.sleep", lambda seconds: None)
    clock = iter([0.0, 0.0, 30.0, 61.0])
    monkeypatch.setattr("career_lens.cli.time.monotonic", lambda: next(clock))
    _reader(monkeypatch, _answer({"status": "running"}, pending=True))

    assert main(["search-result", "rq-one", "--wait", "--timeout", "60"]) == 1
    assert "did not finish" in capsys.readouterr().err


def test_search_result_reports_an_unknown_request(monkeypatch, capsys):
    from career_lens.cli import main
    from career_lens.google.read import GoogleRequestMissing

    _reader(monkeypatch, GoogleRequestMissing("no Google search request rq-x"))

    assert main(["search-result", "rq-x"]) == 1
    assert "no Google search request rq-x" in capsys.readouterr().err


def test_search_result_reports_a_document_that_failed_verification(
        monkeypatch, capsys):
    from career_lens.cli import main
    from career_lens.google.read import GoogleDocumentCorrupt

    _reader(monkeypatch, GoogleDocumentCorrupt("sha256 mismatch"))

    assert main(["search-result", "rq-one"]) == 1
    assert "integrity" in capsys.readouterr().err


def _generic_answer(document, *, pending=False):
    from career_lens.generic.read import GenericResult

    return GenericResult(document, pending=pending)


def _generic_reader(monkeypatch, *answers):
    calls = []
    remaining = list(answers)

    def url_result(connection, request_id, **kwargs):
        calls.append(request_id)
        answer = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    class Connection:
        def close(self):
            calls.append("closed")

    monkeypatch.setattr("career_lens.generic.read.url_result", url_result)
    monkeypatch.setattr("career_lens.store.db.connect", lambda: Connection())
    return calls


def test_url_result_prints_the_canonical_generic_document(monkeypatch, capsys):
    import json
    from career_lens.cli import main

    document = {"rank": 1, "url": "https://example.com", "capture": {}}
    _generic_reader(monkeypatch, _generic_answer(document))

    assert main(["url-result", "rq-one"]) == 0
    assert json.loads(capsys.readouterr().out) == document


def test_url_result_waits_for_a_running_request(monkeypatch, capsys):
    import json
    from career_lens.cli import main

    monkeypatch.setattr("career_lens.cli.time.sleep", lambda seconds: None)
    calls = _generic_reader(
        monkeypatch,
        _generic_answer({"status": "running"}, pending=True),
        _generic_answer({"rank": 1, "capture": {}}),
    )

    assert main(["url-result", "rq-one", "--wait"]) == 0
    assert calls.count("rq-one") == 2
    assert json.loads(capsys.readouterr().out)["rank"] == 1


def test_url_result_reports_a_corrupt_stored_document(monkeypatch, capsys):
    from career_lens.cli import main
    from career_lens.generic.store import GenericDocumentCorrupt

    _generic_reader(
        monkeypatch, GenericDocumentCorrupt("schema does not match"))

    assert main(["url-result", "rq-one"]) == 1
    assert "integrity verification" in capsys.readouterr().err


def _policy(monkeypatch, projection=None, *, missing=False):
    from career_lens.google import policy as google_policy

    calls = []

    class Connection:
        def close(self):
            pass

    def read(connection, worker_id, **kwargs):
        calls.append(("read", worker_id))
        if missing:
            raise google_policy.UnknownGooglePolicy(
                f"no Google policy for worker {worker_id}")
        return object()

    def update(connection, worker_id, values):
        calls.append(("update", worker_id, values))
        if missing:
            raise google_policy.UnknownGooglePolicy(
                f"no Google policy for worker {worker_id}")

    monkeypatch.setattr(google_policy, "read", read)
    monkeypatch.setattr(google_policy, "update", update)
    monkeypatch.setattr(
        google_policy, "projection",
        lambda row: projection or {
            "capable": True,
            "policy": {"enabled": True, "max_searches_hour": 5,
                       "max_searches_24h": 40},
            "usage": {"searches_hour": 1, "searches_24h": 9},
            "remaining": {"searches_hour": 4, "searches_24h": 31,
                          "dispatch": 4},
        })
    monkeypatch.setattr("career_lens.store.db.transaction",
                        lambda url=None: _transaction(Connection()))
    monkeypatch.setattr("career_lens.store.db.connect", lambda: Connection())
    return calls


import contextlib


@contextlib.contextmanager
def _transaction(connection):
    yield connection


def test_google_policy_shows_a_slot_budget(monkeypatch, capsys):
    import json
    from career_lens.cli import main

    calls = _policy(monkeypatch)

    assert main(["google-policy", "worker-a"]) == 0
    assert calls == [("read", "worker-a")]
    printed = json.loads(capsys.readouterr().out)
    assert printed["policy"]["max_searches_24h"] == 40
    assert printed["remaining"]["dispatch"] == 4


def test_google_policy_changes_only_the_flags_that_were_given(
        monkeypatch, capsys):
    from career_lens.cli import main

    calls = _policy(monkeypatch)

    assert main([
        "google-policy", "worker-a", "--max-searches-24h", "40",
    ]) == 0
    assert ("update", "worker-a", {"max_searches_24h": 40}) in calls


def test_google_policy_turns_a_slot_off_and_on(monkeypatch):
    from career_lens.cli import main

    calls = _policy(monkeypatch)
    assert main(["google-policy", "worker-a", "--disable"]) == 0
    assert ("update", "worker-a", {"enabled": False}) in calls

    calls.clear()
    assert main(["google-policy", "worker-a", "--enable"]) == 0
    assert ("update", "worker-a", {"enabled": True}) in calls


def test_google_policy_refuses_to_be_both_enabled_and_disabled(capsys):
    from career_lens.cli import main

    try:
        main(["google-policy", "worker-a", "--enable", "--disable"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted both switches")


def test_google_policy_reports_a_slot_that_has_none(monkeypatch, capsys):
    from career_lens.cli import main

    _policy(monkeypatch, missing=True)

    assert main(["google-policy", "worker-x"]) == 1
    assert "no Google policy for worker worker-x" in capsys.readouterr().err


def test_scrape_forwards_every_option_its_api_accepts(monkeypatch):
    """The CLI dropped four fields the HTTP submission has always taken, so a
    label or a callback meant reaching for curl."""
    from career_lens.cli import main

    received = {}

    def submit(url, **kwargs):
        received.update({"url": url, **kwargs})
        return Submission("rq-full", True)

    monkeypatch.setattr("career_lens.api.requests.submit", submit)

    assert main([
        "scrape", "https://www.linkedin.com/in/jane/",
        "--label", "candidate 12",
        "--max-attempts", "5",
    ]) == 0
    assert received["label"] == "candidate 12"
    assert received["max_attempts"] == 5


def test_search_accepts_a_serp_only_request(monkeypatch):
    """`top_n` is 0 through 20: zero captures the SERP and opens no results."""
    from career_lens.cli import main

    received = {}

    def submit_google(query, **kwargs):
        received.update(kwargs)
        return Submission("rq-serp", True), query, 0

    monkeypatch.setattr("career_lens.api.requests.submit_google", submit_google)

    assert main(["search", "ai agents", "--top-n", "0"]) == 0
    assert received["top_n"] == 0


def test_search_rejects_a_top_n_beyond_the_contract(capsys):
    from career_lens.cli import main

    try:
        main(["search", "ai agents", "--top-n", "6"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted a top_n above the maximum")
    assert "between 0 and 5" in capsys.readouterr().err


def test_search_forwards_the_tempo_it_was_given(monkeypatch):
    from career_lens.cli import main

    received = {}

    def submit_google(query, **kwargs):
        received.update(kwargs)
        return Submission("rq-search", True), query, 3

    monkeypatch.setattr("career_lens.api.requests.submit_google", submit_google)

    assert main(["search", "ai agents", "--tempo", "human"]) == 0
    assert received["tempo"] == "human"


def test_search_defaults_to_the_same_tempo_scrape_does(monkeypatch):
    """An operator who names no tempo is passed through as having named none,
    and the service resolves that to the same default a scrape gets."""
    from career_lens.capture.options import DEFAULT_TEMPO_NAME, normalise_tempo
    from career_lens.cli import main

    received = {}

    def submit_google(query, **kwargs):
        received.update(kwargs)
        return Submission("rq-search", True), query, 3

    monkeypatch.setattr("career_lens.api.requests.submit_google", submit_google)

    assert main(["search", "ai agents"]) == 0
    assert received["tempo"] is None
    assert normalise_tempo(received["tempo"],
                           kind="google_search") == DEFAULT_TEMPO_NAME


def test_search_rejects_a_tempo_the_worker_cannot_run(capsys):
    from career_lens.cli import main

    try:
        main(["search", "ai agents", "--tempo", "leisurely"])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover
        raise AssertionError("argparse accepted an unknown tempo")
    assert "leisurely" in capsys.readouterr().err


def test_request_logging_is_off_at_every_layer_that_defaults_it():
    """One default per layer is one chance each to leave it on.

    driver.configure() is called from a single place, so anything that
    captures without going through the supervisor falls back to the module
    default. If these ever disagree, the flag stops meaning what it says.
    The CLI's own default is covered by the worker-defaults test above,
    which reads what the parser hands the Supervisor.
    """
    import inspect

    from career_lens.capture import driver
    from career_lens.fleet import supervisor

    assert driver.HAR_MODE == "off"
    for function in (driver.configure, supervisor.run_worker,
                     supervisor.Supervisor.__init__):
        assert inspect.signature(function).parameters["har"].default == "off", (
            function.__qualname__)


def test_scrape_omits_linkedin_sections_for_a_linkedin_url_too(monkeypatch):
    # The default changed from "all" to None for every URL. The submission
    # service already reads an omitted selection as every section, so nothing
    # about a LinkedIn scrape changes.
    from career_lens.cli import main

    received = {}

    def submit(url, **kwargs):
        received.update({"url": url, **kwargs})
        return Submission("rq-person", True)

    monkeypatch.setattr("career_lens.api.requests.submit", submit)

    assert main(["scrape", "https://www.linkedin.com/in/jane/"]) == 0
    assert received["linkedin_sections"] is None


def test_scrape_refuses_linkedin_sections_on_a_generic_url(monkeypatch, capsys):
    from career_lens.cli import main

    def submit(url, **kwargs):
        raise AssertionError("no request should be submitted")

    monkeypatch.setattr("career_lens.api.requests.submit", submit)

    assert main([
        "scrape", "https://example.com/article",
        "--linkedin-sections", "about",
    ]) == 1
    assert "LinkedIn" in capsys.readouterr().err


def _declaration(tmp_path):
    import json

    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps({
        "id": "worker-a", "label": "Laptop",
        "workers": [{"id": "worker-a", "profile": "research"}],
    }))
    return path


def test_env_file_reaches_the_supervisor_with_its_quotes_intact(
        monkeypatch, tmp_path, capsys):
    """What `set -a; source .env; set +a` could not do.

    The shell removed the inner quotes from this exact line, which left the
    API and the supervisor holding different values for one unchanged file.
    """
    import os

    from career_lens.cli import main

    env = tmp_path / ".env"
    env.write_text('CAREER_LENS_CREDENTIAL_KEYS={"1":"abc"}\n')
    # setenv first so the teardown removes whatever this test leaves behind,
    # whether or not the variable was set when it started.
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", "")
    monkeypatch.delenv("CAREER_LENS_CREDENTIAL_KEYS")

    class FakeSupervisor:
        def __init__(self, config, **kwargs):
            pass
        def run(self):
            pass

    monkeypatch.setattr(
        "career_lens.fleet.supervisor.Supervisor", FakeSupervisor)
    code = main([
        "supervisor", "--config", str(_declaration(tmp_path)),
        "--env-file", str(env)])

    assert code == 0
    assert os.environ["CAREER_LENS_CREDENTIAL_KEYS"] == '{"1":"abc"}'
    assert "set 1 setting(s)" in capsys.readouterr().out


def test_a_supervisor_started_on_bad_configuration_says_what_is_wrong(
        monkeypatch, tmp_path, capsys):
    from career_lens.cli import main

    env = tmp_path / ".env"
    env.write_text("CAREER_LENS_CREDENTIAL_KEYS={1:not-json}\n"
                   "CAREER_LENS_CREDENTIAL_ACTIVE_KEY=1\n")
    for name in ("CAREER_LENS_CREDENTIAL_KEYS", "CAREER_LENS_CREDENTIAL_ACTIVE_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)

    assert main([
        "supervisor", "--config", str(_declaration(tmp_path)),
        "--env-file", str(env)]) == 1
    assert "CAREER_LENS_CREDENTIAL_KEYS must be a JSON object" in (
        capsys.readouterr().err)
