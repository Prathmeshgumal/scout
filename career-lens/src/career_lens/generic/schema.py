"""A queryable, append-only projection of one generic page document.

One table, where Google needs two: a generic request reads one page, so there
is no list of results hanging off the observation. What it is for is the same
-- answering "what did this URL look like, at or before this instant" with an
indexed lookup instead of a walk over stored objects.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean, CheckConstraint, Column, ForeignKey, Index, Integer, Text,
)

from career_lens.generic.contract import CAPTURE_STATUSES, FAILURE_KINDS
from career_lens.store.schema.common import (
    APPEND_ONLY, TS, enum_check, pk, table,
)


SCHEMA = "generic"


page_observation = table(
    "page_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False,
           unique=True),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    Column("url_requested", Text, nullable=False),
    Column("url_normalised", Text, nullable=False),
    Column("host", Text, nullable=False),
    Column("title", Text),
    Column("http_status", Integer),
    Column("status", Text, nullable=False),
    Column("scroll_stopped", Text, nullable=False),
    Column("scroll_growing", Boolean, nullable=False),
    Column("text_characters", Integer, nullable=False),
    # When the page was read, which is what a temporal question is asking
    # about -- not when this row was written, which says only when somebody
    # got round to projecting it.
    Column("observed_at", TS, nullable=False),
    Column("failure_kind", Text),
    Column("failure_detail", Text),
    CheckConstraint(enum_check("status", CAPTURE_STATUSES), name="status"),
    CheckConstraint("text_characters >= 0", name="nonnegative_characters"),
    CheckConstraint(
        "http_status IS NULL OR http_status BETWEEN 100 AND 599",
        name="http_status"),
    CheckConstraint(
        "failure_kind IS NULL OR ("
        + enum_check("failure_kind", FAILURE_KINDS) + ")",
        name="failure_kind"),
    CheckConstraint("(failure_kind IS NULL) = (failure_detail IS NULL)",
                    name="failure_pair"),
    CheckConstraint("status <> 'succeeded' OR failure_kind IS NULL",
                    name="success_without_failure"),
    CheckConstraint("status = 'succeeded' OR failure_kind IS NOT NULL",
                    name="incomplete_with_failure"),
    # A page that was read has a capture to point at. A failed reading is the
    # one case where there is nothing durable behind the row.
    CheckConstraint("status = 'failed' OR capture_id IS NOT NULL",
                    name="read_page_has_a_capture"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

# The temporal lookup this table exists for: newest reading of one URL at or
# before an instant.
Index("ix_page_observation_url", page_observation.c.url_normalised,
      page_observation.c.observed_at.desc())
Index("ix_page_observation_request", page_observation.c.request_id,
      page_observation.c.id.desc())
Index("ix_page_observation_host", page_observation.c.host)
