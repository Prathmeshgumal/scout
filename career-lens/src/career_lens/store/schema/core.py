"""The tables that know how to capture a web page, and nothing about LinkedIn.

Three layers, and they only flow one way. A request is what someone asked for
and is the only thing that changes. A capture is what the site sent and is
immutable. An extraction is what we read out of captures, and appends.

Nothing here references the `linkedin` schema. A second site is a new schema
and a new package, touching none of this.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey,
    Index, Integer, Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from .common import APPEND_ONLY, MUTABLE, TS, enum_check, pk, table, text_pk
from ...capture.options import (
    DEFAULT_TEMPO_NAME,
    TEMPO_NAMES,
)
from ...capture.pressure import INTERVENTION_KINDS, INTERVENTION_OUTCOMES

# What the caller asked us to fetch. LinkedIn people and companies have capture
# recipes and extractors; generic requests stop after their captures are stored.
KINDS = ("linkedin_person", "linkedin_company", "linkedin_job",
         "linkedin_job_search", "generic", "google_search")

STATUSES = ("queued", "assigned", "running", "succeeded", "failed",
            "cancelled", "satisfied")


# A closed vocabulary rather than free text, because v2's retry policy is
# defined in terms of these -- a different identity after a challenge, a wait
# after a rate limit, never after not_found. Recording honest causes from the
# start is easier than reconstructing them later.
#
# `cancelled` is not a failure, and this tuple's name is now slightly wide of
# its contents. It is here because it must be: journal.close writes a cause,
# and a closed attempt with a NULL cause is how this codebase spells success --
# so a cancelled attempt with no cause would report as a successful one.
# Renaming the vocabulary would touch the driver and the console for no
# behavioural gain.
FAILURE_KINDS = (
    "blocked",           # a challenge was presented and not solved
    "login_required",    # the session is not signed in
    "not_found",         # the profile does not exist
    "timeout",           # the page did not settle
    "browser_error",     # the driver failed
    "extract_failed",    # captured, but the parser produced no document
    "parser_drift",      # captured and parsed, but the JSON is suspect
    "worker_lost",       # the worker died mid-capture; nothing else is known
    "account_mismatch",  # the physical browser carries another identity
    "storage_error",     # a verified durable object could not be stored/read
    "cancelled",         # an operator stopped this run
)

# `profile_image` and `cover_image` are the page's own images, fetched while
# it was open: LinkedIn signs media URLs with an expiry days out, so a stored
# URL is dead data and the bytes are the only durable form.
ARTIFACT_KINDS = ("html", "screenshot", "har", "text", "profile_image",
                  "cover_image")

EXTRACTION_OUTCOMES = ("ok", "partial", "suspect", "failed")

# trace.py's three phase kinds, plus `note` for its zero-duration markers. The
# strings are lowercased forms of trace.WAIT / NET / WORK -- the journal stores
# what the instrumentation already classifies rather than a second taxonomy.
EVENT_KINDS = ("wait", "net", "work", "note")

# Stored rather than derived. The level is computed at record time from three
# inputs -- a phase that ran long, a phase that failed, a phase named in
# trace.ROUTINE -- and recomputing it on read would duplicate that logic in SQL.
EVENT_LEVELS = ("debug", "info", "warning")


scrape_request = table(
    "scrape_request",
    text_pk(),
    Column("label", Text),
    Column("priority", Text, nullable=False, server_default="normal"),
    Column("url_requested", Text),
    Column("url_normalised", Text),
    Column("query_requested", Text),
    Column("query_normalised", Text),
    Column("google_top_n", Integer),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),
    Column("tempo", Text, nullable=False, server_default=DEFAULT_TEMPO_NAME),
    # NULL means every available section; [] means the base profile only.
    Column("linkedin_sections", JSONB),
    # Pages of job results to read: for a company's Jobs tab and for a job
    # search. NULL for every other kind.
    Column("job_pages", Integer),
    Column("requested_at", TS, nullable=False),
    Column("dispatch_checked_at", TS),
    Column("dispatch_wait_reason", Text),
    # How many attempts this request may spend, frozen at submit. Per-request
    # rather than a deployment-wide setting so that raising the default never
    # retroactively resurrects old failures, and so the operator's retry button
    # is an increment rather than a bypass path of its own.
    Column("max_attempts", Integer, nullable=False, server_default="3"),
    # When the request may next be dispatched. NULL means now; a future value
    # means it is serving a retry backoff. v2 §2: "retries are scheduled
    # attempts" -- the dispatcher's existing tick is the scheduler.
    Column("next_attempt_at", TS),
    # When an operator asked for this to stop. Intent, not outcome: for a
    # request with work in flight the API writes only this, and the worker
    # still closes the request -- so the attempt's cause and the status keep
    # committing together. `status='cancelled'` implies this is set; the
    # reverse does not hold, because it is written while the run is still going.
    Column("cancel_requested_at", TS),
    # How stale an answer the caller would have accepted. Kept even when the
    # request scraped anyway, because "we checked and what we had was too
    # old" is why that run exists.
    Column("freshness_seconds", Integer),
    # A request that needed no work at all. There is no attempt to derive an
    # ending from, so the ending lives here.
    Column("satisfied_at", TS),
    CheckConstraint(enum_check("kind", KINDS), name="kind"),
    CheckConstraint(enum_check("status", STATUSES), name="status"),
    CheckConstraint(enum_check("tempo", TEMPO_NAMES), name="tempo"),
    CheckConstraint(enum_check("priority", ("normal", "high")),
                    name="priority"),
    CheckConstraint("(status = 'satisfied') = (satisfied_at IS NOT NULL)",
                    name="satisfied_has_an_instant"),
    CheckConstraint("max_attempts >= 1", name="max_attempts"),
    CheckConstraint("job_pages IS NULL OR job_pages BETWEEN 1 AND 20",
                    name="job_pages"),
    CheckConstraint(
        "(kind = 'google_search' "
        "AND query_requested IS NOT NULL AND query_normalised IS NOT NULL "
        "AND google_top_n BETWEEN 0 AND 5 "
        "AND url_requested IS NULL AND url_normalised IS NULL) OR "
        "(kind <> 'google_search' "
        "AND query_requested IS NULL AND query_normalised IS NULL "
        "AND google_top_n IS NULL "
        "AND url_requested IS NOT NULL AND url_normalised IS NOT NULL)",
        name="target_shape",
    ),
    # started_at, finished_at, failure_kind and failure_detail live on
    # scrape_attempt (v2 §3.1): they are facts about an execution, and a retry
    # must be able to record a different one. The
    # `(status = 'failed') = (failure_kind IS NOT NULL)` CHECK went with them --
    # it cannot span two tables -- and is now an invariant that
    # queue/tasks.py::_finish holds by closing the attempt and setting the
    # status in one transaction. See
    # tests/test_queue.py::test_a_failed_request_always_has_an_attempt_carrying_the_cause.
    mutability=MUTABLE,
)

# Deduplication, not a prohibition on re-scraping. Outstanding work for a URL
# is unique; a URL whose earlier requests all finished can be asked for again,
# which is the entire point of the tool.
Index("uq_scrape_request_outstanding_url", scrape_request.c.url_normalised,
      unique=True,
      postgresql_where=scrape_request.c.status.in_((
          "queued", "assigned", "running")))
Index("uq_scrape_request_outstanding_google",
      scrape_request.c.query_normalised, scrape_request.c.google_top_n,
      unique=True,
      postgresql_where=(
          scrape_request.c.kind == "google_search")
      & scrape_request.c.status.in_(("queued", "assigned", "running")))

Index("ix_scrape_request_requested_at", scrape_request.c.requested_at.desc())



# Every requeue decision, including the operator override. The request's wait
# reason is transient dispatch state; this is the durable answer to who asked
# for another attempt and why.
scrape_request_retry_event = table(
    "scrape_request_retry_event",
    pk(),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"), nullable=False),
    Column("occurred_at", TS, nullable=False),
    Column("source", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("delay_seconds", Integer, nullable=False),
    CheckConstraint(enum_check("source", ("automatic", "manual")), name="source"),
    CheckConstraint("delay_seconds >= 0", name="nonnegative_delay"),
    mutability=APPEND_ONLY,
)

Index("ix_scrape_request_retry_event_request_id_occurred_at",
      scrape_request_retry_event.c.request_id,
      scrape_request_retry_event.c.occurred_at)


capture = table(
    "capture",
    text_pk(),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"), nullable=False),
    # Nullable only during the execution-attribution bridge. The dispatcher
    # writes it for every new capture; the final cutover makes it required
    # after legacy execution call sites are removed.
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT")),
    # `profile` for the main page, the section name for each details/ page, and
    # `page` for a scrape with no recipe. These are the same names the parser
    # uses for its `sections` mapping, so the projection needs no translation
    # table -- see linkedin/parse.
    Column("section", Text, nullable=False),
    Column("url_requested", Text, nullable=False),
    Column("url_final", Text),
    Column("host", Text, nullable=False),
    Column("http_status", Integer),
    Column("request_method", Text),
    Column("request_headers", JSONB),
    Column("response_headers", JSONB),
    Column("captured_at", TS, nullable=False),
    Column("browser_profile", Text),
    Column("title", Text),
    # In the database rather than object storage because this is what full-text
    # search reads, and a search that has to fetch a blob per candidate row is
    # not a search.
    Column("markdown", Text),
    Column("markdown_parser", Text),
    # How completely the page was read. Computed on every capture by page.py
    # and, until now, thrown away. The Posts and Life tabs of a company are
    # feeds with no end to reach, so one that stopped at its budget is
    # byte-identical to one that finished. Only these say which it was, and it
    # cannot be reconstructed from the stored bytes afterwards.
    Column("scroll_stopped", Text),
    Column("scroll_growing", Boolean, nullable=False, server_default="false"),
    # Why a page that read fine has no image. Distinct from a failed capture:
    # the HTML is good and only the screenshot is missing.
    Column("screenshot_error", Text),
    # Recipe-owned facts needed to interpret the stored page later. Keeping
    # this generic avoids putting LinkedIn-specific columns in the core layer.
    Column("metadata", JSONB, nullable=False, server_default="'{}'::jsonb"),
    mutability=APPEND_ONLY,
)

Index("ix_capture_request_id", capture.c.request_id)
Index("ix_capture_attempt_id", capture.c.attempt_id)
Index("ix_capture_captured_at", capture.c.captured_at.desc())


attempt_diagnostic = table(
    "attempt_diagnostic",
    Column("attempt_id", BigInteger, ForeignKey("scrape_attempt.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("blob_key", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    Column("captured_at", TS, nullable=False),
    CheckConstraint("content_type = 'image/png'", name="png"),
    CheckConstraint("byte_size > 0 AND byte_size <= 5242880", name="size"),
    mutability=APPEND_ONLY,
)


artifact = table(
    "artifact",
    pk(),
    Column("capture_id", Text,
           ForeignKey("capture.id", ondelete="RESTRICT"), nullable=False),
    Column("kind", Text, nullable=False),
    # Screenshots are banded: a page taller than one image becomes several, in
    # page order. Everything else has ordinal 0.
    Column("ordinal", Integer, nullable=False, server_default="0"),
    Column("blob_key", Text, nullable=False),
    Column("content_type", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("sha256", Text, nullable=False),
    CheckConstraint(enum_check("kind", ARTIFACT_KINDS), name="kind"),
    mutability=APPEND_ONLY,
)

Index("uq_artifact_capture_kind_ordinal", artifact.c.capture_id,
      artifact.c.kind, artifact.c.ordinal, unique=True)
Index("uq_artifact_blob_key", artifact.c.blob_key, unique=True)


extraction = table(
    "extraction",
    text_pk(),
    # Over the request rather than one capture: the parser reads the profile
    # page and its section pages together, and a reading derived from eight
    # documents cannot honestly be attributed to one of them.
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"), nullable=False),
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT")),
    Column("extractor", Text, nullable=False),
    # The parser's own schema_version, verbatim. This is how we know which
    # stored documents need re-projecting when the parser changes.
    Column("extractor_version", Text, nullable=False),
    Column("extracted_at", TS, nullable=False),
    Column("outcome", Text, nullable=False),
    Column("document_key", Text),
    Column("document_sha256", Text),
    Column("document_bytes", BigInteger),
    CheckConstraint(enum_check("outcome", EXTRACTION_OUTCOMES), name="outcome"),
    # A failed extraction has no document; anything else must have one. Keeps
    # "the parser produced nothing" from looking like "we forgot to store it".
    CheckConstraint(
        "(outcome = 'failed') = (document_key IS NULL)",
        name="document_iff_not_failed"),
    mutability=APPEND_ONLY,
)

Index("ix_extraction_request_id", extraction.c.request_id)
Index("ix_extraction_attempt_id", extraction.c.attempt_id)


extraction_input = table(
    "extraction_input",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False),
    Column("capture_id", Text,
           ForeignKey("capture.id", ondelete="RESTRICT"), nullable=False),
    Column("section", Text, nullable=False),
    mutability=APPEND_ONLY,
)

Index("uq_extraction_input", extraction_input.c.extraction_id,
      extraction_input.c.capture_id, unique=True)


# One execution of a request: which worker, which profile, when, and why it
# stopped. v2 §3.1 puts these facts here rather than on the request, because a
# retry must be free to run under a different identity from the one that
# failed.
#
# An integer key, not a prefixed text one: ids.py reserves text keys for rows
# named outside this system, and an attempt is addressed as a request and an
# ordinal, exactly as v2 §4.4 renders it.
scrape_attempt = table(
    "scrape_attempt",
    pk(),
    Column("rate_limit", JSONB),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"), nullable=False),
    # Dense from 1. The unique index below is what makes a second attempt land
    # correctly rather than silently overwrite the first, and the ordinal is
    # what the retry policy reads to know how much of the request's ceiling has
    # been spent.
    Column("attempt_no", Integer, nullable=False),
    Column("worker", Text),
    # v1's stand-in for v2's identity_id.
    Column("browser_profile", Text),
    Column("assigned_at", TS),
    Column("started_at", TS),
    Column("claim_deadline_at", TS),
    Column("queue_job_id", BigInteger),
    Column("supervisor_id", Text,
           ForeignKey("supervisor.id", ondelete="RESTRICT")),
    Column("worker_id", Text,
           ForeignKey("worker.id", ondelete="RESTRICT")),
    Column("account_id", Text,
           ForeignKey("linkedin_account.id", ondelete="RESTRICT")),
    Column("assignment_id", Text,
           ForeignKey("account_worker_assignment.id", ondelete="RESTRICT")),
    Column("ended_at", TS),
    Column("cause", Text),
    Column("cause_detail", Text),
    CheckConstraint(f"cause IS NULL OR ({enum_check('cause', FAILURE_KINDS)})",
                    name="cause"),
    # An attempt that knows why it stopped has stopped. The converse is not
    # constrained: an attempt that ended without a cause succeeded.
    CheckConstraint("cause IS NULL OR ended_at IS NOT NULL",
                    name="cause_needs_an_end"),
    mutability=MUTABLE,
)

Index("uq_scrape_attempt_request_id_attempt_no", scrape_attempt.c.request_id,
      scrape_attempt.c.attempt_no, unique=True)
Index("ix_scrape_attempt_worker_id", scrape_attempt.c.worker_id)
Index("ix_scrape_attempt_assigned_at", scrape_attempt.c.assigned_at,
      postgresql_where=scrape_attempt.c.assigned_at.is_not(None))
Index("uq_scrape_attempt_open_fleet_request", scrape_attempt.c.request_id,
      unique=True,
      postgresql_where=(scrape_attempt.c.ended_at.is_(None)
                        & scrape_attempt.c.assignment_id.is_not(None)))
Index("uq_scrape_attempt_open_worker", scrape_attempt.c.worker_id, unique=True,
      postgresql_where=(scrape_attempt.c.ended_at.is_(None)
                        & scrape_attempt.c.worker_id.is_not(None)))


# The journal. `id` is the stream cursor, and it is safe to use as one only
# because a single attempt has exactly one writer -- the worker that owns it --
# so a lower id can never become visible after a higher one. That is a property
# of the design, not of the column, and it is the condition to check before
# ever letting anything else write into an attempt's journal.
attempt_event = table(
    "attempt_event",
    pk(),
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT"), nullable=False),
    Column("at", TS, nullable=False),
    # Offset from the attempt's start, so the UI has a stable time column that
    # does not depend on how a clock is formatted.
    Column("elapsed_ms", Integer, nullable=False),
    Column("duration_ms", Integer),
    Column("kind", Text, nullable=False),
    Column("level", Text, nullable=False),
    Column("phase", Text, nullable=False),
    Column("detail", Text),
    Column("failed", Boolean, nullable=False, server_default="false"),
    # Set only on the `capture stored` event, which is how the UI learns to
    # fetch a capture that has just landed.
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    CheckConstraint(enum_check("kind", EVENT_KINDS), name="kind"),
    CheckConstraint(enum_check("level", EVENT_LEVELS), name="level"),
    mutability=APPEND_ONLY,
)

Index("ix_attempt_event_attempt_id_id", attempt_event.c.attempt_id,
      attempt_event.c.id)


# What stood between a run and the page. Its own table rather than a reserved
# `attempt_event.kind`: an intervention carries four facts -- what it was, what
# it did to us, where it happened and the evidence -- and that stopped fitting
# the journal's generic phase/detail shape.
#
# Append-only and deduplicated by its writer, not by a constraint: the same
# challenge is asked about by several code paths in one flow, and a unique
# index would turn "this was already noted" into an error the capture has to
# handle.
attempt_intervention = table(
    "attempt_intervention",
    pk(),
    Column("attempt_id", BigInteger,
           ForeignKey("scrape_attempt.id", ondelete="RESTRICT"),
           nullable=False),
    Column("at", TS, nullable=False),
    # Offset from the attempt's start, matching attempt_event, so the two
    # streams can be read against one clock.
    Column("elapsed_ms", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("outcome", Text, nullable=False),
    # From the challenge's own URL, not the request's target: one request
    # touches many hosts, and a paywall on a trade-press site is not the same
    # news as a captcha on linkedin.com.
    Column("host", Text, nullable=False),
    Column("evidence", Text),
    CheckConstraint(enum_check("kind", INTERVENTION_KINDS), name="kind"),
    CheckConstraint(enum_check("outcome", INTERVENTION_OUTCOMES),
                    name="outcome"),
    mutability=APPEND_ONLY,
)

Index("ix_attempt_intervention_attempt_id",
      attempt_intervention.c.attempt_id)


# Which stored captures a satisfied request rested on. Deliberately the same
# shape as extraction_input: an operator whose satisfied request turns out to
# have been answered by something stale will ask exactly this question.
request_satisfaction = table(
    "request_satisfaction",
    pk(),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("section", Text, nullable=False),
    Column("capture_id", Text,
           ForeignKey("capture.id", ondelete="RESTRICT")),
    Column("observed_at", TS, nullable=False),
    mutability=APPEND_ONLY,
)

Index("uq_request_satisfaction_request_id_section",
      request_satisfaction.c.request_id, request_satisfaction.c.section,
      unique=True)


# Who wants a request. One request serves every caller that asked for the same
# target while it was outstanding (api/requests.py::submit), so one consumer's
# cancel must not stop the work for the others: a consumer withdraws only its
# own interest, and the work stops when no interest is left. A row without an
# api_key_id stands for every caller that has no key -- the console or the CLI
# -- and no consumer can withdraw it.
request_interest = table(
    "request_interest",
    pk(),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"), nullable=False),
    Column("api_key_id", Text, ForeignKey("api_key.id", ondelete="RESTRICT")),
    Column("created_at", TS, nullable=False),
    Column("withdrawn_at", TS),
    CheckConstraint("withdrawn_at IS NULL OR api_key_id IS NOT NULL",
                    name="only_a_key_withdraws"),
    mutability=MUTABLE,
)

# One row for each key, and one for all the callers without a key. A key that
# asks again after it withdrew wants the work again: the same row, not a new one.
Index("uq_request_interest_key", request_interest.c.request_id,
      request_interest.c.api_key_id, unique=True,
      postgresql_where=request_interest.c.api_key_id.isnot(None))
Index("uq_request_interest_keyless", request_interest.c.request_id,
      unique=True,
      postgresql_where=request_interest.c.api_key_id.is_(None))


