"""career_lens's command line.

The commands run in different places. `migrate` runs anywhere with the owner's
credentials -- the compose file runs it in a one-shot container. `supervisor`
runs on the host, because it drives a browser against a signed-in profile and
a container does not do that. `control` runs the dispatcher queue.

The rest are conveniences for working without an HTTP client, and they reach
the database directly rather than calling the service, so they work in the
one-shot container and in a supervisor shell where no API is listening.
`scrape` and `search` submit the two kinds of request; `search-result` and
`url-result` read canonical documents back; `google-policy` shows or changes a
worker's search budget.

Each shares its implementation with the HTTP route that does the same job --
`api.requests.submit`, `google.read.search_result`, `generic.read.url_result`,
`google.policy` -- so a command and its endpoint cannot answer the same
question differently.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url

from .store.config import ConfigError
from .store.db import DatabaseUnavailable, migrate
from .capture.options import (
    DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE,
    DEFAULT_TEMPO_NAME,
    TEMPO_NAMES,
)


def _probability(value: str) -> float:
    chance = float(value)
    if not 0 <= chance <= 1:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return chance


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _window_size(value: str) -> tuple[int, int]:
    parts = value.strip().lower().split("x")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("must look like WIDTHxHEIGHT")
    try:
        width, height = (int(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must look like WIDTHxHEIGHT") from None
    if width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError("width and height must be positive")
    return width, height


def _top_n(value: str) -> int:
    """The contract's own range, refused here rather than a round trip later.

    Zero is meaningful and not a mistake: it captures the results page and
    opens none of the destinations behind it.
    """
    from .google.contract import MAX_TOP_N

    number = int(value)
    if not 0 <= number <= MAX_TOP_N:
        raise argparse.ArgumentTypeError(f"must be between 0 and {MAX_TOP_N}")
    return number


def _submission_arguments(parser: argparse.ArgumentParser) -> None:
    """The options every submission shares, so the two commands cannot drift."""
    parser.add_argument(
        "--label", default=None,
        help="a short name for this request, shown in the console")


def _safe(exc: Exception) -> str:
    """An exception's message with anything URL-shaped stripped of its password.

    SQLAlchemy puts the connection URL into several of its errors, and that URL
    carries the password. `store.db._where` exists to keep passwords out of the
    messages it raises; this is the same duty on the path that prints whatever
    the driver raised.
    """
    text = str(exc)
    for word in text.split():
        if "://" in word and "@" in word:
            try:
                text = text.replace(
                    word, make_url(word).render_as_string(hide_password=True))
            except Exception:
                text = text.replace(word, "<url>")
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="career-lens")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="bring the database schema up to date")
    api_key_parser = sub.add_parser("api-key", help="manage v1 API keys")
    api_key_sub = api_key_parser.add_subparsers(
        dest="api_key_command", required=True,
    )
    api_key_create = api_key_sub.add_parser(
        "create", help="create a customer API key",
    )
    api_key_create.add_argument("name")
    api_key_sub.add_parser("list", help="list customer API keys")
    api_key_revoke = api_key_sub.add_parser(
        "revoke", help="revoke a customer API key",
    )
    api_key_revoke.add_argument("key_id")
    supervisor_parser = sub.add_parser(
        "supervisor", help="run this machine's workers (needs a browser)")
    supervisor_parser.add_argument(
        "--config", type=Path, default=None,
        help="local supervisor JSON declaration (defaults to the standard "
             "path)")
    supervisor_parser.add_argument(
        "--env-file", type=Path, default=None, metavar="PATH",
        help="read settings from a docker compose env file instead of "
             "exporting it with `set -a; source .env`. The shell strips "
             "quotes the compose parser keeps, so the same file could give "
             "the containers and this process different values; this reads "
             "it the way compose does. Anything already set in the "
             "environment wins.")
    supervisor_parser.add_argument(
        "--log-level", default="info", choices=["debug", "info", "warning"],
        help="how much of each run to print (default: info). The database "
             "keeps every phase whatever this is set to.")
    supervisor_parser.add_argument(
        "--company-tabs", default="all", metavar="TABS",
        help="which tabs to sweep on a company page: 'all' (default), 'none', "
             "or a comma-separated list such as 'about,people,jobs'")
    supervisor_parser.add_argument(
        "--show-cursor", action="store_true",
        help="show a cursor overlay in each worker's browser. The overlay "
             "is stripped from stored HTML, so it changes nothing that is "
             "kept.")
    supervisor_parser.add_argument(
        "--use-virtual-display", action="store_true",
        help="run headed Chrome inside Xvfb instead of the current desktop "
             "display (default: off)")
    supervisor_parser.add_argument(
        "--window-size", type=_window_size, default=None,
        metavar="WIDTHxHEIGHT",
        help="browser window dimensions. Virtual-display mode defaults to "
             "1920x1080; desktop mode defaults to the maximized display")
    supervisor_parser.add_argument(
        "--har", choices=("off", "headers", "bodies"), default="off",
        help="store the network exchange beside each captured page, as a "
             "gzipped HAR (default: off). 'headers' records every request and "
             "response without fetching any response body, which costs no "
             "round trips; 'bodies' adds first-party XHR and fetch payloads "
             "under 512KB, which costs about 1-2%% of capture time.")
    supervisor_parser.add_argument(
        "--linkedin-google-search-chance", type=_probability,
        default=DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE, metavar="P",
        help="probability from 0 to 1 that a worker reaches a direct "
             "LinkedIn person or company through Google first (default: 0.3)")
    enqueue_parser = sub.add_parser("scrape", help="enqueue one URL")
    enqueue_parser.add_argument("url")
    enqueue_parser.add_argument(
        # Leave omitted pacing to the service; preserve explicit overrides.
        "--tempo", choices=TEMPO_NAMES, default=None,
        help=f"capture pacing profile (default: {DEFAULT_TEMPO_NAME})")
    enqueue_parser.add_argument(
        # Default None rather than "all". Both mean every section to the
        # submission service, but only None says the operator never asked --
        # which is what lets a non-LinkedIn URL refuse an explicit selection
        # instead of silently ignoring it.
        "--linkedin-sections", default=None, metavar="SECTIONS",
        help="LinkedIn person or company sections to capture: 'all' (the "
             "default), 'none', or comma-separated names such as "
             "'about,experience,activity-posts'. LinkedIn URLs only.")
    enqueue_parser.add_argument(
        "--max-attempts", type=_positive_int, default=None, metavar="N",
        help="how many times a retryable failure may be retried (default: "
             "the service default)")
    _submission_arguments(enqueue_parser)
    search_parser = sub.add_parser(
        "search", help="enqueue one Google search")
    search_parser.add_argument("query")
    search_parser.add_argument(
        "--tempo", choices=TEMPO_NAMES, default=None,
        help=f"capture pacing profile (default: {DEFAULT_TEMPO_NAME}). The "
             "worker paces a query run the same way it paces a profile one.")
    search_parser.add_argument(
        "--top-n", type=_top_n, default=None, metavar="N",
        help="how many results to capture behind the search, 0 through 20 "
             "(default: the service default). Zero captures the results page "
             "and opens nothing behind it.")
    _submission_arguments(search_parser)
    result_parser = sub.add_parser(
        "search-result",
        help="print one Google search's canonical JSON document")
    result_parser.add_argument("request_id")
    result_parser.add_argument(
        "--screenshots", action="store_true",
        help="include expiring artifact URLs for the SERP and each capture")
    result_parser.add_argument(
        "--wait", action="store_true",
        help="block until the request finishes instead of reporting that it "
             "is still running")
    result_parser.add_argument(
        "--timeout", type=_positive_int, default=300, metavar="SECONDS",
        help="how long --wait will block before giving up (default: 300)")
    result_parser.add_argument(
        "--poll", type=_positive_int, default=2, metavar="SECONDS",
        help="how often --wait re-reads the request (default: 2)")
    url_result_parser = sub.add_parser(
        "url-result",
        help="print one generic URL's canonical JSON document")
    url_result_parser.add_argument("request_id")
    url_result_parser.add_argument(
        "--screenshots", action="store_true",
        help="include expiring screenshot URLs for the captured page")
    url_result_parser.add_argument(
        "--wait", action="store_true",
        help="block until the request finishes instead of reporting that it "
             "is still running")
    url_result_parser.add_argument(
        "--timeout", type=_positive_int, default=300, metavar="SECONDS",
        help="how long --wait will block before giving up (default: 300)")
    url_result_parser.add_argument(
        "--poll", type=_positive_int, default=2, metavar="SECONDS",
        help="how often --wait re-reads the request (default: 2)")
    policy_parser = sub.add_parser(
        "google-policy",
        help="show or change one browser worker's Google search budget")
    policy_parser.add_argument("worker_id")
    switch = policy_parser.add_mutually_exclusive_group()
    switch.add_argument(
        "--enable", dest="enabled", action="store_true", default=None,
        help="let this worker run Google searches")
    switch.add_argument(
        "--disable", dest="enabled", action="store_false", default=None,
        help="stop dispatching Google searches to this worker")
    policy_parser.add_argument(
        "--max-searches-hour", type=_positive_int, default=None, metavar="N")
    policy_parser.add_argument(
        "--max-searches-24h", type=_positive_int, default=None, metavar="N")
    sub.add_parser(
        "control", help="run dispatcher and recovery jobs")
    arguments = parser.parse_args(argv)

    if arguments.command == "api-key":
        from .auth import service as api_keys
        from .store.db import connect, transaction

        try:
            if arguments.api_key_command == "create":
                with transaction() as connection:
                    issued = api_keys.create_api_key(
                        connection, arguments.name, datetime.now(UTC),
                    )
                print(f"career_lens: created {issued.id} {issued.name}")
                print(issued.token)
                return 0
            if arguments.api_key_command == "list":
                connection = connect()
                try:
                    keys = api_keys.list_api_keys(connection)
                finally:
                    connection.close()
                if not keys:
                    print("career_lens: no API keys")
                    return 0
                for key in keys:
                    state = "revoked" if key.revoked_at else "active"
                    print(f"{key.id}  {state}  {key.prefix}  {key.name}")
                return 0
            with transaction() as connection:
                revoked = api_keys.revoke_api_key(
                    connection, arguments.key_id, datetime.now(UTC),
                )
            if revoked is None:
                print(
                    f"career_lens: API key {arguments.key_id} not found",
                    file=sys.stderr,
                )
                return 1
            print(f"career_lens: revoked {revoked.id}")
            return 0
        except (DatabaseUnavailable, ConfigError, ValueError) as exc:
            print(f"career_lens: {_safe(exc)}", file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            print(f"career_lens: {_safe(exc)}", file=sys.stderr)
            return 1

    if arguments.command == "migrate":
        try:
            revision = migrate()
        # migrate() runs command.upgrade() directly against the owner's URL --
        # it never calls store.db.connect(), so DatabaseUnavailable alone
        # cannot catch what it raises. A bad CAREER_LENS_MIGRATE_DATABASE_URL
        # scheme raises ConfigError; a stopped Postgres, wrong credentials or
        # database, or any other failure Alembic hits mid-migration surfaces
        # as SQLAlchemyError.
        except (DatabaseUnavailable, ConfigError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        except SQLAlchemyError as exc:
            print(f"career_lens: {_safe(exc)}", file=sys.stderr)
            return 1
        print(f"career_lens: schema at {revision}")
        return 0
    if arguments.command == "supervisor":
        from .fleet.config import (
            SupervisorConfigError, load_supervisor_config,
            supervisor_config_path,
        )
        from .fleet.registration import SupervisorError

        config_path = arguments.config or supervisor_config_path()
        try:
            if arguments.env_file is not None:
                from .store.env_file import load_env_file

                applied, kept = load_env_file(arguments.env_file)
                print(f"career_lens: {arguments.env_file} set "
                      f"{len(applied)} setting(s), left {len(kept)} already "
                      f"in the environment")
            config = load_supervisor_config(config_path)
            from .fleet.supervisor import Supervisor, configure_logging

            configure_logging(arguments.log_level)
            Supervisor(
                config, log_level=arguments.log_level,
                company_tabs=arguments.company_tabs,
                show_cursor=arguments.show_cursor,
                use_virtual_display=arguments.use_virtual_display,
                window_size=arguments.window_size,
                har=arguments.har,
                linkedin_google_search_chance=(
                    arguments.linkedin_google_search_chance),
            ).run()
        # ConfigError is what the startup preflight raises. It reaches here
        # instead of a traceback because the whole point of checking the
        # environment before any worker claims a job is to say what is wrong
        # in one line the operator can act on.
        except (SupervisorConfigError, SupervisorError, ConfigError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        return 0
    if arguments.command == "search":
        from .api.requests import submit_google

        try:
            answer, query, top_n = submit_google(
                arguments.query, top_n=arguments.top_n,
                label=arguments.label, tempo=arguments.tempo)
        # Every rejection -- empty query, top_n out of range -- is a ValueError.
        except (DatabaseUnavailable, ConfigError, ValueError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        print(f"career_lens: {answer.request_id} "
              f"({'queued' if answer.created else 'already outstanding'}) "
              f"{query!r} top {top_n}")
        return 0
    if arguments.command == "search-result":
        from .google import read as google_read
        from .store.blobs import open_store
        from .store.db import connect

        deadline = time.monotonic() + arguments.timeout
        try:
            connection = connect()
        except (DatabaseUnavailable, ConfigError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        try:
            while True:
                try:
                    answer = google_read.search_result(
                        connection, arguments.request_id,
                        open_store=open_store,
                        include_screenshots=arguments.screenshots)
                except google_read.GoogleRequestMissing as exc:
                    print(f"career_lens: {exc}", file=sys.stderr)
                    return 1
                except google_read.GoogleDocumentUnavailable as exc:
                    print(f"career_lens: {exc}", file=sys.stderr)
                    return 1
                except google_read.GoogleDocumentCorrupt:
                    print("career_lens: the Google search document failed "
                          "integrity verification", file=sys.stderr)
                    return 1
                if not answer.pending:
                    print(json.dumps(answer.document, indent=2, default=str))
                    return 0
                if not arguments.wait:
                    # Not a failure: the request exists and this is the true
                    # answer about it. A separate code so a script can wait,
                    # retry or move on without parsing the document.
                    print(json.dumps(answer.document, indent=2, default=str))
                    return 3
                if time.monotonic() >= deadline:
                    print(f"career_lens: {arguments.request_id} did not finish "
                          f"within {arguments.timeout}s", file=sys.stderr)
                    return 1
                time.sleep(arguments.poll)
        finally:
            connection.close()
    if arguments.command == "url-result":
        from .generic import read as generic_read
        from .generic.store import GenericDocumentCorrupt
        from .store.blobs import open_store
        from .store.db import connect

        deadline = time.monotonic() + arguments.timeout
        try:
            connection = connect()
        except (DatabaseUnavailable, ConfigError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        try:
            while True:
                try:
                    answer = generic_read.url_result(
                        connection, arguments.request_id,
                        open_store=open_store,
                        include_screenshots=arguments.screenshots)
                except generic_read.GenericRequestMissing as exc:
                    print(f"career_lens: {exc}", file=sys.stderr)
                    return 1
                except (generic_read.GenericResultUnavailable,
                        generic_read.GenericTextUnavailable) as exc:
                    print(f"career_lens: {exc}", file=sys.stderr)
                    return 1
                except GenericDocumentCorrupt:
                    print("career_lens: the generic URL document failed "
                          "integrity verification", file=sys.stderr)
                    return 1
                if not answer.pending:
                    print(json.dumps(answer.document, indent=2, default=str))
                    return 0
                if not arguments.wait:
                    print(json.dumps(answer.document, indent=2, default=str))
                    return 3
                if time.monotonic() >= deadline:
                    print(f"career_lens: {arguments.request_id} did not finish "
                          f"within {arguments.timeout}s", file=sys.stderr)
                    return 1
                time.sleep(arguments.poll)
        finally:
            connection.close()
    if arguments.command == "google-policy":
        from .google import policy as google_policy
        from .store.db import connect, transaction

        # Only what was actually typed. Sending the untouched fields back at
        # their current values would make this command overwrite a change
        # somebody else made between the read and the write.
        changes = {
            name: value for name, value in (
                ("enabled", arguments.enabled),
                ("max_searches_hour", arguments.max_searches_hour),
                ("max_searches_24h", arguments.max_searches_24h),
            ) if value is not None
        }
        try:
            if changes:
                with transaction() as connection:
                    google_policy.update(
                        connection, arguments.worker_id, changes)
                    projection = google_policy.projection(
                        google_policy.read(connection, arguments.worker_id))
            else:
                connection = connect()
                try:
                    projection = google_policy.projection(
                        google_policy.read(connection, arguments.worker_id))
                finally:
                    connection.close()
        except google_policy.UnknownGooglePolicy as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        except (DatabaseUnavailable, ConfigError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(projection, indent=2, default=str))
        return 0
    if arguments.command == "control":
        from .queue.app import app as queue_app

        queue_app.run_worker(queues=["control"], name="control")
        return 0
    if arguments.command == "scrape":
        # Imported lazily: migrate does not need the API submission path.
        from .api.requests import submit
        from .capture.urls import UrlError, canonical, classify

        if arguments.linkedin_sections is not None:
            # Classified before submission so the refusal costs no round trip
            # and creates no request. An unusable URL is reported as itself
            # rather than as a section problem.
            try:
                kind = classify(canonical(arguments.url))
            except UrlError as exc:
                print(f"career_lens: {exc}", file=sys.stderr)
                return 1
            if (arguments.linkedin_sections is not None
                    and kind not in ("linkedin_person", "linkedin_company")):
                print("career_lens: --linkedin-sections applies only to "
                      "LinkedIn person and company URLs", file=sys.stderr)
                return 1

        try:
            answer = submit(
                arguments.url, tempo=arguments.tempo,
                linkedin_sections=arguments.linkedin_sections,
                label=arguments.label,
                max_attempts=arguments.max_attempts)
        # Every way a submission can be rejected -- a bad label, an unknown
        # section -- is a ValueError.
        except (DatabaseUnavailable, ConfigError, UrlError, ValueError) as exc:
            print(f"career_lens: {exc}", file=sys.stderr)
            return 1
        print(f"career_lens: {answer.request_id} "
              f"({'queued' if answer.created else 'already outstanding'})")
        return 0
    return 1
