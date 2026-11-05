"""Queryable, append-only projections of canonical Google search documents."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from career_lens.google.contract import (
    CAPTURE_MODES, CAPTURE_STATUSES, OUTCOMES, REQUEST_FAILURE_KINDS,
    RESULT_FAILURE_KINDS, SELECTION_REASONS,
)
from career_lens.store.schema.common import (
    APPEND_ONLY, MUTABLE, TS, enum_check, pk, table,
)


SCHEMA = "google"

# Ephemeral attempt progress is not evidence and cannot satisfy freshness.
search_progress = table(
    "search_progress",
    Column("attempt_id", BigInteger, ForeignKey("scrape_attempt.id", ondelete="RESTRICT"),
           primary_key=True),
    Column("document", JSONB, nullable=False),
    schema=SCHEMA, mutability=MUTABLE,
)


search_observation = table(
    "search_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False,
           unique=True),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("serp_capture_id", Text,
           ForeignKey("capture.id", ondelete="RESTRICT")),
    Column("query_requested", Text, nullable=False),
    Column("query_normalised", Text, nullable=False),
    Column("requested_top_n", Integer, nullable=False),
    Column("results_examined", Integer, nullable=False),
    Column("google_host", Text),
    Column("serp_url", Text),
    Column("browser_locale", Text),
    Column("browser_timezone", Text),
    Column("searched_at", TS),
    Column("outcome", Text, nullable=False),
    Column("failure_kind", Text),
    Column("failure_detail", Text),
    Column("examined", Integer, nullable=False),
    Column("selected", Integer, nullable=False),
    Column("captured", Integer, nullable=False),
    Column("partial", Integer, nullable=False),
    Column("failed", Integer, nullable=False),
    Column("skipped", Integer, nullable=False),
    # The ceiling stored documents were written under, not the one a caller
    # may request today (5). A run that opened 12 destinations in the past is
    # still projected, and re-projected, exactly as it happened.
    CheckConstraint("requested_top_n BETWEEN 0 AND 20", name="top_n"),
    CheckConstraint("results_examined BETWEEN 0 AND 60",
                    name="results_examined"),
    CheckConstraint("examined = results_examined", name="examined_matches"),
    CheckConstraint(
        "examined >= 0 AND selected >= 0 AND captured >= 0 "
        "AND partial >= 0 AND failed >= 0 AND skipped >= 0",
        name="nonnegative_counts"),
    CheckConstraint(
        "captured + partial + failed + skipped = examined",
        name="terminal_counts"),
    CheckConstraint(
        "captured + partial + failed = selected", name="selected_counts"),
    CheckConstraint("selected <= requested_top_n", name="selection_limit"),
    CheckConstraint(enum_check("outcome", OUTCOMES), name="outcome"),
    CheckConstraint(
        "failure_kind IS NULL OR ("
        + enum_check("failure_kind", REQUEST_FAILURE_KINDS) + ")",
        name="failure_kind"),
    CheckConstraint(
        "(failure_kind IS NULL) = (failure_detail IS NULL)",
        name="failure_pair"),
    CheckConstraint("outcome <> 'ok' OR failure_kind IS NULL",
                    name="ok_without_failure"),
    CheckConstraint("outcome <> 'failed' OR failure_kind IS NOT NULL",
                    name="failed_with_failure"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("ix_search_observation_request", search_observation.c.request_id,
      search_observation.c.id.desc())
Index("ix_search_observation_query", search_observation.c.query_normalised,
      search_observation.c.searched_at.desc())
Index("ix_search_observation_searched_at",
      search_observation.c.searched_at.desc())


search_result = table(
    "search_result",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.search_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("rank", Integer, nullable=False),
    Column("url", Text, nullable=False),
    Column("normalised_url", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("snippet", Text, nullable=False),
    Column("selected", Boolean, nullable=False),
    Column("selection_reason", Text, nullable=False),
    Column("capture_id", Text,
           ForeignKey("capture.id", ondelete="RESTRICT")),
    Column("capture_mode", Text),
    Column("capture_status", Text),
    Column("failure_kind", Text),
    Column("failure_detail", Text),
    CheckConstraint("rank BETWEEN 1 AND 60", name="rank"),
    CheckConstraint(enum_check("selection_reason", SELECTION_REASONS),
                    name="selection_reason"),
    CheckConstraint(
        "capture_mode IS NULL OR ("
        + enum_check("capture_mode", CAPTURE_MODES) + ")",
        name="capture_mode"),
    CheckConstraint(
        "capture_status IS NULL OR ("
        + enum_check("capture_status", CAPTURE_STATUSES) + ")",
        name="capture_status"),
    CheckConstraint(
        "failure_kind IS NULL OR ("
        + enum_check("failure_kind", RESULT_FAILURE_KINDS) + ")",
        name="failure_kind"),
    CheckConstraint(
        "(failure_kind IS NULL) = (failure_detail IS NULL)",
        name="failure_pair"),
    CheckConstraint(
        "(selected AND selection_reason = 'top_n' "
        "AND capture_mode IS NOT NULL AND capture_status IS NOT NULL "
        "AND (capture_status = 'failed' OR capture_id IS NOT NULL)) OR "
        "(NOT selected "
        "AND selection_reason IN "
        "('beyond_top_n', 'linkedin_unavailable', 'excluded_host') "
        "AND capture_id IS NULL AND capture_mode IS NULL "
        "AND capture_status IS NULL AND failure_kind IS NULL "
        "AND failure_detail IS NULL)",
        name="selection_shape"),
    CheckConstraint(
        "capture_status <> 'succeeded' OR failure_kind IS NULL",
        name="success_without_failure"),
    CheckConstraint(
        "capture_status NOT IN ('partial', 'failed') "
        "OR failure_kind IS NOT NULL",
        name="incomplete_with_failure"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_search_result_rank", search_result.c.observation_id,
      search_result.c.rank, unique=True)
Index("ix_search_result_normalised_url", search_result.c.normalised_url)
Index("ix_search_result_host", search_result.c.host)
Index("ix_search_result_capture_id", search_result.c.capture_id,
      postgresql_where=search_result.c.capture_id.is_not(None))
