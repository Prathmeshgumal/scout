"""The one task: capture a request, extract it, and record what happened.

Three things commit separately and deliberately. Each capture commits as it
lands, so a run can be watched while it happens. The extraction commits once
the parser has read every page it needs. The attempt's cause and the request's
status commit together, in `_finish`, because scrape_request gave up its
`(status = 'failed') = (failure_kind IS NOT NULL)` CHECK when the cause moved
onto the attempt, and this pairing is what replaced it.

A crash between them therefore leaves durable captures and an unfinished
request. That is the correct outcome under the three-layer model -- a capture
is what the site sent, and it does not stop being that because a later step
failed -- and `journal.sweep_stalled` is what closes the attempt afterwards.

A failure the policy says is worth another attempt settles as `queued` in this
same transaction, so a retrying request never passes through `failed`. A
request an operator cancelled settles as `cancelled` by the same fork, and the
poll that notices the cancel sits between pages in `_work`.
"""

from __future__ import annotations

import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import text

from ..journal import Journal
from ..store.db import transaction
from .app import app

if TYPE_CHECKING:  # `Document` appears only in annotations here.
    from ..capture.writer import Document

DEFAULT_PROFILE = "research"
_log = logging.getLogger("career_lens.queue.tasks")


class GoogleSearchDeferred(RuntimeError):
    """The initial query could not spend budget and must remain queued."""


def _nudge_dispatch(connection) -> None:
    """Wake dispatch without risking the transaction that settled a request."""
    try:
        with connection.begin_nested():
            from .enqueue import enqueue_dispatch

            enqueue_dispatch(connection)
    except Exception as exc:  # noqa: BLE001 -- the periodic pass is fallback
        _log.warning("career_lens: dispatch nudge failed: %s", exc)


def _session_verification_stale(row) -> bool:
    """Invalidate on lifecycle changes, never because a timer elapsed."""
    return (
        row.verified_at is None or row.verified_host is None
        or row.verified_at < row.supervisor_started_at
        or row.open_session_operation)


# Lazy compatibility seams. Control-only installations can import this module
# without Patchright/Docling, while tests and embedders can still replace the
# exact call boundaries they used before the split.
def capture_request(*args, **kwargs):
    from ..capture.driver import capture_request as implementation
    return implementation(*args, **kwargs)


def write_capture(*args, **kwargs):
    from ..capture.writer import write_capture as implementation
    return implementation(*args, **kwargs)


def open_store(*args, **kwargs):
    from ..store.blobs import open_store as implementation
    return implementation(*args, **kwargs)


def parse_profile_html(*args, **kwargs):
    from ..linkedin.parse import parse_profile_html as implementation
    return implementation(*args, **kwargs)


def parse_company_html(*args, **kwargs):
    from ..linkedin.parse import parse_company_html as implementation
    return implementation(*args, **kwargs)


def store_extraction(*args, **kwargs):
    from ..linkedin.project import store_extraction as implementation
    return implementation(*args, **kwargs)


def store_company_extraction(*args, **kwargs):
    from ..linkedin.company_project import (
        store_company_extraction as implementation,
    )
    return implementation(*args, **kwargs)


def store_google_extraction(*args, **kwargs):
    from ..google.store import store_extraction as implementation
    return implementation(*args, **kwargs)


def store_generic_extraction(*args, **kwargs):
    from ..generic.store import store_extraction as implementation
    return implementation(*args, **kwargs)


@app.periodic(cron="* * * * *", periodic_id="dispatch-pending")
@app.task(name="dispatch_pending_requests", queue="control",
          lock="dispatch:pending", queueing_lock="dispatch:pending")
def dispatch_pending_requests(timestamp: int | None = None) -> None:
    """Late-bind queued requests; timestamp is accepted for periodic kicks."""
    from ..fleet.dispatch import dispatch_pending, sweep_expired_claims
    from ..fleet.recovery import (
        sweep_orphaned_scrape_attempts,
        sweep_orphaned_worker_operations,
        sweep_restarted_native_attempts,
    )
    from ..journal import sweep_stalled

    sweep_restarted_native_attempts()
    sweep_stalled()
    sweep_orphaned_scrape_attempts()
    sweep_orphaned_worker_operations()
    sweep_expired_claims()
    dispatch_pending()


@app.periodic(cron="*/15 * * * *", periodic_id="materialise-shifts")
@app.task(name="materialise_shift_days", queue="control",
          lock="schedules:materialise", queueing_lock="schedules:materialise")
def materialise_shift_days(timestamp: int | None = None) -> None:
    """Keep a rolling week of shift plans ahead of every enabled schedule.

    Every quarter hour rather than daily: a plan that has to exist before an
    account can work is not something to generate once and hope. Re-running is
    free, because a day already planned is left exactly as it was.
    """
    from ..fleet.schedules import materialise_pending

    materialise_pending()


@app.task(name="scrape_assigned", queue="capture")
def scrape_assigned(attempt_id: int) -> None:
    run_assigned(attempt_id)


@app.task(name="worker_operation", queue="capture")
def worker_operation(operation_id: str) -> None:
    from ..fleet.operations import run_worker_operation

    run_worker_operation(operation_id)


def run_assigned(attempt_id: int, url: str | None = None, *,
                 session_factory=None, observer=None) -> None:
    """Execute one dispatcher-created attempt on its owning worker."""
    with transaction(url) as connection:
        # The kind first, and under its own lock. The authenticated claim
        # inner-joins an account and a ready assignment; an anonymous attempt
        # has neither, so asking that query about one returns nothing and the
        # attempt looks like it does not exist. Which claim to run is a
        # question about the request, so the request is what answers it.
        claim = connection.execute(text("""
            SELECT r.kind, a.assignment_id
            FROM scrape_attempt a
            JOIN scrape_request r ON r.id=a.request_id
            WHERE a.id=:attempt
            FOR UPDATE OF a, r
        """), {"attempt": attempt_id}).one_or_none()
        if claim is None:
            raise RuntimeError(f"no fleet scrape_attempt {attempt_id}")
        public_google = (
            claim.kind == "google_search" and claim.assignment_id is None)
        public_generic = claim.kind == "generic" and claim.assignment_id is None
        if public_google or public_generic:
            row = _claim_public_attempt(
                connection, attempt_id, now=datetime.now(UTC))
        else:
            row = _claim_authenticated_attempt(connection, attempt_id)
        if row is None:
            raise RuntimeError(f"no fleet scrape_attempt {attempt_id}")
        if row.ended_at is not None or row.started_at is not None:
            return
        if row.status != "assigned" or (
                not (public_google or public_generic)
                and row.assignment_state != "ready"):
            raise RuntimeError(
                f"attempt {attempt_id} is not ready for its assigned session")
        journal = Journal.start(connection, attempt_id, url=url)
        cancelled = row.cancel_requested_at is not None

    if cancelled:
        # Before the browser, not after it. A cancel that arrives while the job
        # is waiting to be claimed should cost nothing at all -- no session, no
        # first page, no traffic. Outside the block above because _finish opens
        # its own transaction.
        _finish(journal, row.request_id, url, cause="cancelled",
                detail="an operator stopped this run")
        return

    from ..capture.browser import session as default_session
    from ..capture import diagnostics, pressure, trace
    from ..capture.failures import CaptureFailed

    session_factory = session_factory or default_session
    target = (row.query_requested if row.kind == "google_search"
              else row.url_normalised)
    trace.activate([f"request {row.request_id}",
                    f"{row.kind}  {target}"], sink=journal.sink)
    pressure.activate(journal.intervention)
    try:
        from ..capture import driver

        session_options = {"profile": row.browser_profile}
        if driver.SHOW_CURSOR:
            session_options["show_cursor"] = True
        with session_factory(**session_options) as page, diagnostics.on_failure(
                page, attempt_id=attempt_id, database_url=url):
            if row.kind == "generic":
                # Also protect already-assigned legacy attempts carrying an
                # account: an arbitrary site cannot verify that identity.
                _work(
                    row.request_id, row.kind, row.url_normalised,
                    row.browser_profile, journal, url, attempt_id=attempt_id,
                    page=page, tempo_name=row.tempo, discovery_chance=0)
            elif public_google:
                _work_google(
                    row, browser_profile=row.browser_profile, journal=journal,
                    database_url=url, attempt_id=attempt_id, page=page)
                _finish(journal, row.request_id, url)
            else:
                _run_authenticated(
                    row, journal=journal, url=url, attempt_id=attempt_id,
                    page=page, observer=observer, driver=driver)
    except GoogleSearchDeferred:
        _defer_google_budget(journal, row.request_id, url)
    except CaptureFailed as failure:
        from ..fleet.retry import IDENTITY_CAUSES

        if failure.rate_limit:
            _finish(journal, row.request_id, url, cause=failure.failure_kind,
                    detail=failure.detail, rate_limit=failure.rate_limit)
            return
        if row.kind == "generic":
            finish = _finish
        elif (row.kind == "google_search"
                and getattr(failure, "provider", None) == "linkedin"):
            finish = _finish_identity_failure
        elif row.kind == "google_search":
            # A wall on the SERP belongs to Google/browser capacity. It says
            # nothing about the LinkedIn identity that may be available for
            # opening destinations, and a SERP-only attempt has no provider
            # identity at all.
            finish = _finish
        elif failure.failure_kind not in IDENTITY_CAUSES:
            finish = _finish
        else:
            finish = _finish_identity_failure
        finish(journal, row.request_id, url,
               cause=failure.failure_kind, detail=failure.detail)
    except Exception as exc:  # noqa: BLE001 -- preserve a failure record
        _finish(journal, row.request_id, url, cause="browser_error",
                detail=str(exc)[:2000])
        raise
    finally:
        trace.close()
        pressure.close()


def _claim_public_attempt(connection, attempt_id: int, *, now: datetime):
    """Lock attempt, request, worker and supervisor for an anonymous run.

    Both attribution columns must be NULL. An attempt carrying an account is
    LinkedIn work whatever its request says, and running it here would skip
    the session verification that work depends on.

    Public eligibility is rechecked under the lock, with this attempt exempted
    from the busy test: it is the open attempt, and a worker cannot be
    disqualified by the very job it is being asked to start.
    """
    row = connection.execute(text("""
        SELECT a.request_id, a.started_at, a.ended_at, a.worker_id,
               r.url_normalised, r.query_requested, r.query_normalised,
               r.google_top_n, r.requested_at, r.kind, r.status, r.tempo,
               r.linkedin_sections, r.job_pages,
               r.cancel_requested_at,
               worker.local_name AS browser_profile,
               CAST(NULL AS text) AS assignment_id,
               CAST(NULL AS text) AS assignment_state,
               CAST(NULL AS timestamptz) AS verified_at,
               CAST(NULL AS text) AS verified_host,
               supervisor.started_at AS supervisor_started_at,
               FALSE AS open_session_operation,
               CAST(NULL AS text) AS profile_url
        FROM scrape_attempt a
        JOIN scrape_request r ON r.id=a.request_id
        JOIN worker ON worker.id=a.worker_id
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        WHERE a.id=:attempt
          AND a.account_id IS NULL AND a.assignment_id IS NULL
        FOR UPDATE OF a, r, worker
    """), {"attempt": attempt_id}).one_or_none()
    if row is None or row.started_at is not None or row.ended_at is not None:
        return row

    from datetime import timedelta

    from ..fleet.dispatch import HEARTBEAT_SECONDS
    if row.kind == "google_search":
        from ..fleet.eligibility import eligible_google_workers

        candidates, reason = eligible_google_workers(
            connection, now=now,
            heartbeat_after=now - timedelta(seconds=HEARTBEAT_SECONDS),
            allow_attempt_id=attempt_id)
    else:
        from ..fleet.eligibility import eligible_public_workers

        candidates, reason = eligible_public_workers(
            connection, now=now,
            heartbeat_after=now - timedelta(seconds=HEARTBEAT_SECONDS),
            allow_attempt_id=attempt_id)
    if not any(item.worker_id == row.worker_id for item in candidates):
        raise RuntimeError(
            f"worker {row.worker_id} cannot run attempt {attempt_id}: "
            f"{reason or 'worker_changed'}")
    return row


def _claim_authenticated_attempt(connection, attempt_id: int):
    """Keep the existing inner joins and ready-assignment lock contract."""
    return connection.execute(text("""
        SELECT a.request_id, a.started_at, a.ended_at, a.worker_id,
               r.url_normalised, r.query_requested, r.query_normalised,
               r.google_top_n, r.requested_at,
               r.kind, r.status, r.tempo,
               r.linkedin_sections, r.job_pages,
               r.cancel_requested_at,
               worker.local_name AS browser_profile,
               ass.id AS assignment_id, ass.state AS assignment_state,
               ass.verified_at, ass.verified_host,
               supervisor.started_at AS supervisor_started_at,
               EXISTS (
                   SELECT 1 FROM worker_operation operation
                   WHERE operation.assignment_id=ass.id
                     AND operation.status IN ('queued', 'running')
               ) AS open_session_operation,
               account.profile_url
               , account.recovery_phase, account.recovery_not_before,
               account.recovery_failures
        FROM scrape_attempt a
        JOIN scrape_request r ON r.id=a.request_id
        JOIN worker ON worker.id=a.worker_id
        JOIN supervisor ON supervisor.id=worker.supervisor_id
        JOIN account_worker_assignment ass ON ass.id=a.assignment_id
        JOIN linkedin_account account ON account.id=a.account_id
        WHERE a.id=:attempt
        FOR UPDATE OF a, r, worker, ass
    """), {"attempt": attempt_id}).one_or_none()



def _run_authenticated(row, *, journal: Journal, url: str | None,
                       attempt_id: int, page, observer, driver) -> None:
    """Verify the LinkedIn session if needed, then run the request's recipe.

    Lifted out of `run_assigned` unchanged when the claim split. The
    LinkedIn observer import lives here rather than beside the browser
    session, so an anonymous run never loads it at all.
    """
    from ..capture import trace
    from ..linkedin.session import (
        observe_session, same_profile, verified_host,
    )

    observer = observer or observe_session
    if _session_verification_stale(row):
        identity = observer(page)
        cause = None
        detail = identity.detail
        if identity.state == "challenge":
            cause = "blocked"
        elif identity.state == "signed_out":
            cause = "login_required"
        elif identity.state != "signed_in":
            cause = "browser_error"
            detail = (detail or
                      "LinkedIn identity was not observable")
        elif (not row.profile_url or not identity.profile_url
              or not same_profile(identity.profile_url,
                                  row.profile_url)):
            cause = "account_mismatch"
            detail = (
                "browser is not signed in as the assigned "
                "LinkedIn account")
        if cause is not None:
            _finish_identity_failure(
                journal, row.request_id, url, cause=cause,
                detail=detail or cause)
            return
        with trace.phase(
                trace.WORK, "store session verification"):
            with transaction(url) as connection:
                refreshed = connection.execute(text("""
                    UPDATE account_worker_assignment
                    SET verified_at=:now, verified_host=:host
                    WHERE id=:assignment AND state='ready'
                      AND ended_at IS NULL
                    RETURNING id
                """), {
                    "assignment": row.assignment_id,
                    "now": datetime.now(UTC),
                    "host": verified_host(identity),
                }).one_or_none()
                if refreshed is None:
                    raise RuntimeError(
                        f"assignment {row.assignment_id} changed "
                        "during session verification")
    if row.kind == "google_search":
        _work_google(
            row, browser_profile=row.browser_profile,
            journal=journal, database_url=url,
            attempt_id=attempt_id, page=page)
        _finish(journal, row.request_id, url)
    else:
        from ..google.budget import charge_search

        def before_google_search(host: str) -> None:
            charge_search(
                url, worker_id=row.worker_id, request_id=row.request_id,
                attempt_id=attempt_id, google_host=host, wait=True)

        _work(
            row.request_id, row.kind, row.url_normalised,
            row.browser_profile, journal, url, attempt_id=attempt_id,
            page=page, tempo_name=row.tempo,
            discovery_chance=driver.LINKEDIN_GOOGLE_SEARCH_CHANCE,
            linkedin_sections=row.linkedin_sections,
            job_pages=row.job_pages,
            before_google_search=before_google_search)


def sections_of(stored: list[tuple[str, str]]) -> dict[str, str]:
    """Section to capture id, keeping every capture rather than only the last.

    The parser is handed a mapping keyed on section, and person sections are unique
    -- one `experience` page, one `education` page. Company job result pages
    are not, and neither is any future recipe that captures a section twice. A
    plain `by_section[section] = id` drops the earlier capture from both the parser's
    input and extraction_input, and reports nothing.

    Later captures of a section are suffixed rather than overwritten, so a
    collision shows up in the data instead of vanishing from it.
    """
    sections: dict[str, str] = {}
    for section, capture_id in stored:
        name = section
        ordinal = 1
        while name in sections:
            ordinal += 1
            name = f"{section}-{ordinal}"
        sections[name] = capture_id
    return sections


def _cancel_requested(request_id: str, url: str | None) -> bool:
    """Whether an operator has asked for this run to stop.

    One indexed single-row read, called between pages -- up to ~13 times for a
    full person profile. The page boundary is the abort point because it is
    already a boundary: `_work` consumes the capture as a generator and stores
    each page as it lands, so stopping here costs nothing in the driver and
    nothing in the browser.
    """
    with transaction(url) as connection:
        return connection.execute(text(
            "SELECT cancel_requested_at IS NOT NULL FROM scrape_request "
            "WHERE id = :r"), {"r": request_id}).scalar_one()


def _store_documents(
    documents, *, request_id: str, browser_profile: str, journal: Journal,
    database_url: str | None, attempt_id: int | None, store,
) -> tuple[list[Document], list[tuple[str, str]]]:
    """Write each document durably, journal it, and stop after a cancellation.

    Consumed lazily, so a generator's next page is not fetched until the
    previous one is stored. That is what makes a run watchable while it
    happens and what makes the cancellation poll below a real boundary.
    """
    from ..capture import trace
    from ..capture.failures import CaptureFailed

    kept: list[Document] = []
    stored: list[tuple[str, str]] = []

    for document in documents:
        with trace.phase(
                trace.WORK, "store capture", document.section) as span:
            capture_id = write_capture(
                database_url, request_id=request_id, document=document,
                store=store, browser_profile=browser_profile,
                attempt_id=attempt_id)
            # After the capture's transaction, never before: the id does
            # not exist until then and the event's foreign key would
            # refuse it.
            journal.capture_stored(capture_id, document.section)
            kept.append(document)
            stored.append((document.section, capture_id))
            span.detail = f"{document.section} · {capture_id}"
            # After the capture is durable, never before: a page we
            # fetched is a page we keep, and the operator asked to stop
            # the run, not to un-send what the site already returned.
            if _cancel_requested(request_id, database_url):
                raise CaptureFailed(
                    "cancelled", "an operator stopped this run")
    return kept, stored


def _work(request_id: str, kind: str, target: str, browser_profile: str,
          journal: Journal, url: str | None = None,
          attempt_id: int | None = None, page=None, *,
          tempo_name: str, discovery_chance: float,
          before_google_search=None,
          linkedin_sections: list[str] | None = None,
          job_pages: int | None = None) -> None:
    from ..capture import pace, trace

    store = open_store()
    with tempfile.TemporaryDirectory(prefix="career-lens-") as scratch:
        documents, stored = _store_documents(
            capture_request(kind, target,
                            browser_profile=browser_profile,
                            out_dir=Path(scratch),
                            tempo=pace.TEMPOS[tempo_name],
                            discovery_chance=discovery_chance,
                            linkedin_sections=(
                                frozenset(linkedin_sections)
                                if linkedin_sections is not None
                                else None
                            ),
                            job_pages=job_pages,
                            page=page,
                            before_search=before_google_search),
            request_id=request_id, browser_profile=browser_profile,
            journal=journal, database_url=url, attempt_id=attempt_id,
            store=store)

    extractor = EXTRACTORS.get(kind)
    if extractor is not None:
        with trace.phase(trace.WORK, "extract document", kind) as span:
            extractor(
                request_id, target, documents, sections_of(stored), store, url,
                attempt_id=attempt_id,
                requested_sections=(
                    frozenset(linkedin_sections)
                    if linkedin_sections is not None else None
                ),
            )
            span.detail = kind
    _finish(journal, request_id, url)


def _linkedin_destination_host(
        database_url: str | None, attempt_id: int) -> str | None:
    """Return the ready assignment's verified host immediately before traffic."""
    with transaction(database_url) as connection:
        value = connection.execute(text("""
            SELECT CASE WHEN (
                ass.state='ready' AND ass.ended_at IS NULL
                AND ass.operator_attention_code IS NULL
                AND ass.verified_host IS NOT NULL
                AND worker.desired_state='enabled'
                AND worker.health IN ('healthy', 'unknown')
                AND worker.operator_attention_code IS NULL
                AND account.state='enabled'
                AND account.operator_attention_code IS NULL
                AND (account.cooldown_until IS NULL
                     OR account.cooldown_until <= :now)
            ) THEN ass.verified_host ELSE NULL END
            FROM scrape_attempt attempt
            JOIN account_worker_assignment ass
              ON ass.id=attempt.assignment_id
            JOIN worker ON worker.id=attempt.worker_id
            JOIN linkedin_account account ON account.id=attempt.account_id
            WHERE attempt.id=:attempt
        """), {
            "attempt": attempt_id, "now": datetime.now(UTC),
        }).scalar_one_or_none()
    return value


def _work_google(
        request_row, *, browser_profile: str, journal: Journal,
        database_url: str | None, attempt_id: int, page) -> str:
    """Run one query recipe, persisting captures and canonical JSON in order."""
    from ..capture import pace, trace
    from ..capture.driver import CaptureFailed
    from ..google import contract, recipe
    from ..google.progress import save_progress
    from ..google.budget import SearchBudgetExhausted, charge_search
    from ..google.execution import build_search_document

    try:
        store = open_store()
    except Exception as error:
        raise CaptureFailed("storage_error", str(error)[:2000]) from error
    stored: list[tuple[str, str]] = []

    def persist(document: Document) -> str:
        with trace.phase(
                trace.WORK, "store capture", document.section) as span:
            try:
                capture_id = write_capture(
                    database_url, request_id=request_row.request_id,
                    document=document, store=store,
                    browser_profile=browser_profile, attempt_id=attempt_id)
            except Exception as error:
                raise CaptureFailed(
                    "storage_error", str(error)[:2000]) from error
            stored.append((document.section, capture_id))
            journal.capture_stored(capture_id, document.section)
            span.detail = f"{document.section} · {capture_id}"
            # The same boundary `_work` polls on, for the same reason: a page
            # we fetched is a page we keep, and the recipe's next destination
            # is the work the operator asked to stop. A second capture path
            # needs its own poll -- cancelling has to mean the same thing for
            # every kind.
            if _cancel_requested(request_row.request_id, database_url):
                raise CaptureFailed(
                    "cancelled", "an operator stopped this run")
            return capture_id

    def before_search(host: str) -> None:
        with trace.phase(trace.WORK, "google budget", host) as span:
            charge_search(
                database_url, worker_id=request_row.worker_id,
                request_id=request_row.request_id, attempt_id=attempt_id,
                google_host=host, wait=True)
            span.detail = host

    with tempfile.TemporaryDirectory(prefix="career-lens-google-") as scratch:
        try:
            result = recipe.run(
                page, query=request_row.query_requested,
                top_n=request_row.google_top_n, out_dir=Path(scratch),
                before_search=before_search,
                linkedin_host=lambda: _linkedin_destination_host(
                    database_url, attempt_id),
                persist=persist, tempo=pace.TEMPOS[request_row.tempo],
                progress=lambda document: save_progress(database_url, attempt_id, document),
            )
        except SearchBudgetExhausted as error:
            raise GoogleSearchDeferred(str(error)) from error

    with transaction(database_url) as connection:
        timestamps = connection.execute(text("""
            SELECT request.requested_at, attempt.started_at
            FROM scrape_request request
            JOIN scrape_attempt attempt ON attempt.request_id=request.id
            WHERE request.id=:request AND attempt.id=:attempt
        """), {
            "request": request_row.request_id, "attempt": attempt_id,
        }).one()

    recipe_document = result.to_contract()
    finished_at = datetime.now(UTC).isoformat()
    search = recipe_document["search"]
    document = build_search_document(
        request_id=request_row.request_id,
        requested_at=timestamps.requested_at.isoformat(),
        started_at=timestamps.started_at.isoformat(),
        finished_at=finished_at,
        query=request_row.query_requested,
        normalised_query=request_row.query_normalised,
        requested_top_n=request_row.google_top_n,
        google_host=search["google_host"],
        serp_url=search["serp_url"],
        searched_at=search["searched_at"],
        browser_locale=search["browser_locale"],
        browser_timezone=search["browser_timezone"],
        serp=recipe_document["serp"],
        results=recipe_document["results"],
        failure=recipe_document["failure"],
    )
    outcome = document["outcome"]
    failure = document["failure"]
    try:
        with trace.phase(trace.WORK, "google json validate") as span:
            contract.validate_document(document)
            span.detail = (
                f"{len(document['results'])} results · outcome {outcome}")
    except Exception as error:
        raise CaptureFailed("extract_failed", str(error)[:2000]) from error
    try:
        with trace.phase(trace.WORK, "google extraction upload") as span:
            extraction_id = store_google_extraction(
                database_url, request_id=request_row.request_id,
                attempt_id=attempt_id, capture_ids=stored,
                document=document, store=store)
            span.detail = f"{extraction_id} · {len(stored)} captures"
    except Exception as error:
        raise CaptureFailed("storage_error", str(error)[:2000]) from error
    if failure is not None:
        raise CaptureFailed(failure["kind"], failure["detail"])
    return extraction_id


def _defer_google_budget(journal: Journal, request_id: str,
                         url: str | None) -> None:
    """Close an empty attempt and return its non-terminal request to queue."""
    from ..capture import trace

    with transaction(url) as connection:
        with trace.phase(trace.WORK, "finalize request") as span:
            connection.execute(text("""
                UPDATE scrape_request
                SET status='queued', dispatch_checked_at=:now,
                    dispatch_wait_reason='google_budget_exhausted'
                WHERE id=:request AND status='running'
            """), {"request": request_id, "now": datetime.now(UTC)})
            _nudge_dispatch(connection)
            span.detail = "queued · google budget exhausted"
        journal.close(connection)


def _extract(request_id: str, target: str, documents, by_section, store,
             url: str | None = None,
             attempt_id: int | None = None,
             requested_sections: frozenset[str] | None = None) -> None:
    from ..linkedin.execution import extract_linkedin_person

    extract_linkedin_person(
        request_id,
        target,
        documents,
        by_section,
        store,
        url,
        attempt_id=attempt_id,
        requested_sections=requested_sections,
        parser=parse_profile_html,
        persist=store_extraction,
    )


def _extract_company(request_id: str, target: str, documents, by_section, store,
                     url: str | None = None,
                     attempt_id: int | None = None,
                     requested_sections: frozenset[str] | None = None) -> None:
    from ..linkedin.execution import extract_linkedin_company

    extract_linkedin_company(
        request_id,
        target,
        documents,
        by_section,
        store,
        url,
        attempt_id=attempt_id,
        requested_sections=requested_sections,
        parser=parse_company_html,
        persist=store_company_extraction,
    )


def _render_drift(drift: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{item.get('section')}: {item.get('contract')} - "
        f"expected {item.get('expected')}, found {item.get('found')}"
        for item in drift
    )


def _extract_generic(request_id: str, target: str, documents, by_section, store,
                     url: str | None = None, attempt_id: int | None = None,
                     **options) -> None:
    from ..generic.execution import extract_generic

    extract_generic(
        request_id, target, documents, by_section, store, url,
        attempt_id=attempt_id, persist=store_generic_extraction, **options,
    )


def _extract_job(*args, **kwargs) -> None:
    from ..linkedin.jobs import extract_job
    extract_job(*args, **kwargs)


def _extract_job_search(*args, **kwargs) -> None:
    from ..linkedin.jobs import extract_job_search
    extract_job_search(*args, **kwargs)


# Which extractor reads a request of each kind. Registering rather than
# branching means adding one costs an entry here, not an edit to _work.
EXTRACTORS = {
    "linkedin_person": _extract,
    "linkedin_company": _extract_company,
    "linkedin_job": _extract_job,
    "linkedin_job_search": _extract_job_search,
    "generic": _extract_generic,
}


def _finish(journal: Journal, request_id: str, url: str | None, *,
            cause: str | None = None, detail: str | None = None,
            rate_limit: dict | None = None) -> None:
    """Close the attempt and set the request's status, in one transaction.

    Together, always. This is what replaced scrape_request's
    `(status = 'failed') = (failure_kind IS NOT NULL)` CHECK: as long as this
    is the only path, a failed request without a reason is not representable.

    A failure the policy says is worth another attempt settles as `queued`
    rather than `failed`, in this same transaction -- so the request never
    passes through a terminal state it is going to leave, and the callback is
    deferred only for an outcome the caller can act on.

    A pending cancel makes every outcome terminal except a clean finish. The
    exception matters: a run whose last page landed before the poll fired has a
    complete extraction, and `terminal_status` keeps it as `succeeded` rather
    than throwing that away.
    """
    from ..capture import trace
    from ..fleet import retry as retry_policy

    with transaction(url) as connection:
        with trace.phase(trace.WORK, "finalize request") as span:
            row = connection.execute(text("""
                SELECT attempt.worker_id IS NOT NULL AS fleet_attempt,
                       attempt.attempt_no, request.max_attempts,
                       request.cancel_requested_at IS NOT NULL
                           AS cancel_requested
                FROM scrape_attempt attempt
                JOIN scrape_request request ON request.id = attempt.request_id
                WHERE attempt.id = :attempt
            """), {"attempt": journal.attempt_id}).one()
            now = datetime.now(UTC)
            if cause is None:
                from ..fleet.site_cooldowns import reset_after_success

                reset_after_success(connection, journal.attempt_id, now=now)
            decision = None if cause is None else retry_policy.decide_for_attempt(
                connection, journal.attempt_id, now=now, rate_limit=rate_limit,
                cause=cause,
                attempt_no=retry_policy.attempts_spent(connection, request_id),
                max_attempts=row.max_attempts,
                cancel_requested=row.cancel_requested)
            if isinstance(decision, retry_policy.Retry):
                retry_policy.requeue(
                    connection, request_id,
                    now=now, delay=decision.delay, reason=decision.reason)
                status = "queued"
            else:
                status = retry_policy.terminal_status(
                    cause, row.cancel_requested)
                connection.execute(text("""
                    UPDATE scrape_request
                    SET status = :s, next_attempt_at = NULL WHERE id = :r
                """), {"r": request_id, "s": status})
            if row.fleet_attempt:
                # The worker becomes eligible at this commit, so wake the oldest
                # queued request without waiting for the periodic fallback.
                # That also picks up a retry whose delay was zero.
                _nudge_dispatch(connection)
            span.detail = status
        if rate_limit:
            from ..fleet.site_cooldowns import failure_detail

            detail = failure_detail(connection, journal.attempt_id, detail, decision)
        journal.close(connection, cause=cause, detail=detail)


def _finish_identity_failure(journal: Journal, request_id: str,
                             url: str | None, *, cause: str,
                             detail: str) -> None:
    """Stop this execution and quarantine its exact domain resources atomically.

    The quarantine happens whether or not the request gets another attempt: a
    challenged or signed-out account needs a human either way, and the retry
    lands on a different identity precisely because eligibility has just
    dropped this one.
    """
    import json

    from ..capture import trace
    from ..fleet.account_recovery import (
        RecoveryState, configured_policy, signed_out,
    )
    from ..fleet import retry as retry_policy
    from ..fleet.attention import (
        LINKEDIN_ACCOUNT_MISMATCH, LINKEDIN_CHALLENGE,
        LINKEDIN_IDENTITY_UNOBSERVABLE, LINKEDIN_SIGNED_OUT,
    )

    attention_code = {
        "account_mismatch": LINKEDIN_ACCOUNT_MISMATCH,
        "blocked": LINKEDIN_CHALLENGE,
        "browser_error": LINKEDIN_IDENTITY_UNOBSERVABLE,
        "login_required": LINKEDIN_SIGNED_OUT,
    }[cause]
    with transaction(url) as connection:
        with trace.phase(trace.WORK, "finalize request") as span:
            # FOR UPDATE OF attempt, not a bare FOR UPDATE: the join brings in
            # the request only to read its ceiling, and locking that row here
            # would be a wider lock than this function needs.
            row = connection.execute(text("""
                SELECT attempt.account_id, attempt.worker_id,
                       attempt.assignment_id, attempt.attempt_no,
                       request.max_attempts,
                       account.recovery_phase,
                       account.recovery_not_before,
                       account.recovery_failures,
                       account.recovery_login_retry_seconds,
                       account.recovery_post_login_seconds,
                       request.cancel_requested_at IS NOT NULL
                           AS cancel_requested
                FROM scrape_attempt attempt
                JOIN scrape_request request ON request.id = attempt.request_id
                JOIN linkedin_account account ON account.id=attempt.account_id
                WHERE attempt.id=:attempt
                FOR UPDATE OF attempt, account
            """), {"attempt": journal.attempt_id}).one()
            decision = retry_policy.decide(
                cause=cause,
                attempt_no=retry_policy.attempts_spent(connection, request_id),
                max_attempts=row.max_attempts,
                cancel_requested=row.cancel_requested)
            if isinstance(decision, retry_policy.Retry):
                retry_policy.requeue(
                    connection, request_id,
                    now=datetime.now(UTC), delay=decision.delay)
                status = "queued"
            else:
                status = retry_policy.terminal_status(
                    cause, row.cancel_requested)
                retry_policy.fail(connection, request_id, status=status)
            if cause == "login_required":
                changed_at = datetime.now(UTC)
                recovery = signed_out(
                    RecoveryState(
                        phase=row.recovery_phase,
                        not_before=row.recovery_not_before,
                        failures=row.recovery_failures,
                    ),
                    now=changed_at,
                    policy=configured_policy(
                        row.recovery_login_retry_seconds,
                        row.recovery_post_login_seconds,
                    ),
                )
                connection.execute(text("""
                    UPDATE account_worker_assignment
                    SET state='attention_required', state_detail=:detail,
                        operator_attention_code=:attention_code
                    WHERE id=:assignment AND ended_at IS NULL
                """), {
                    "assignment": row.assignment_id,
                    "detail": detail[:1000],
                    "attention_code": attention_code,
                })
                connection.execute(text("""
                    UPDATE linkedin_account
                    SET recovery_phase=:phase,
                        recovery_not_before=:not_before,
                        recovery_failures=:failures,
                        updated_at=:now
                    WHERE id=:account
                """), {
                    "account": row.account_id,
                    "phase": recovery.state.phase,
                    "not_before": recovery.state.not_before,
                    "failures": recovery.state.failures,
                    "now": changed_at,
                })
                connection.execute(text("""
                    INSERT INTO linkedin_account_event
                        (account_id, worker_id, attempt_id, kind,
                         occurred_at, detail)
                    VALUES (:account, :worker, :attempt, 'checkpoint', :now,
                            CAST(:detail AS jsonb))
                """), {
                    "account": row.account_id,
                    "worker": row.worker_id,
                    "attempt": journal.attempt_id,
                    "now": changed_at,
                    "detail": json.dumps({
                        "recovery": "scheduled",
                        "not_before": recovery.state.not_before.isoformat(),
                    }),
                })
            else:
                connection.execute(text("""
                    INSERT INTO linkedin_account_event
                        (account_id, worker_id, attempt_id, kind, occurred_at, detail)
                    VALUES (:account, :worker, :attempt, :kind, :now,
                            CAST(:detail AS jsonb))
                """), {
                    "account": row.account_id, "worker": row.worker_id,
                    "attempt": journal.attempt_id,
                    "kind": "blocked" if cause == "blocked" else "checkpoint",
                    "now": datetime.now(UTC),
                    "detail": json.dumps({"attention_code": attention_code}),
                })
                connection.execute(text("""
                    UPDATE account_worker_assignment
                    SET state=CASE WHEN :cause='account_mismatch'
                                   THEN 'account_mismatch'
                                   ELSE 'attention_required' END,
                        state_detail=:detail,
                        operator_attention_code=:attention_code
                    WHERE id=:assignment AND ended_at IS NULL
                """), {
                    "assignment": row.assignment_id, "cause": cause,
                    "detail": detail[:1000], "attention_code": attention_code,
                })
            if cause not in {"account_mismatch", "login_required"}:
                connection.execute(text("""
                    UPDATE linkedin_account
                    SET state='attention_required', attention_detail=:detail,
                        operator_attention_code=:attention_code,
                        updated_at=:now WHERE id=:account
                """), {
                    "account": row.account_id, "detail": detail[:1000],
                    "attention_code": attention_code,
                    "now": datetime.now(UTC),
                })
            _nudge_dispatch(connection)
            span.detail = f"{status} · {attention_code}"
        journal.close(connection, cause=cause, detail=detail[:2000])
