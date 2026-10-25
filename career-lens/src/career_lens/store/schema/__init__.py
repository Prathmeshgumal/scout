"""Every table in the database, and which of them may never be changed.

Importing this module is what puts the tables into `metadata`. Alembic's env.py
imports it for autogenerate; the schema agreement test imports it to compare
what is declared against what exists.
"""

from __future__ import annotations

from . import auth  # noqa: F401  -- imported for the side effect
from . import core  # noqa: F401  -- imported for the side effect
from . import fleet  # noqa: F401  -- imported for the side effect
from .common import metadata
from career_lens.generic import schema as generic_schema  # noqa: F401
from career_lens.google import schema as google_schema  # noqa: F401
from career_lens.linkedin import schema as linkedin_schema  # noqa: F401

# Named here rather than derived from the tables, so that making a table
# append-only is an edit to this list as well as to its definition. Two places
# is the point: the test compares them, and a single place could not be wrong.
APPEND_ONLY_TABLES = (
    "attempt_diagnostic",
    "capture",
    "artifact",
    "extraction",
    "extraction_input",
    "request_satisfaction",
    "attempt_event",
    "attempt_intervention",
    "scrape_request_retry_event",
    "linkedin_account_event",
    "worker_google_event",
    "generic.page_observation",
    "google.search_observation",
    "google.search_result",
    "linkedin.person_observation",
    "linkedin.person_section_status",
    "linkedin.person_employer",
    "linkedin.person_role",
    "linkedin.person_education",
    "linkedin.person_skill",
    "linkedin.person_certification",
    "linkedin.person_project",
    "linkedin.person_course",
    "linkedin.person_language",
    "linkedin.person_honor",
    "linkedin.person_organization",
    "linkedin.person_patent",
    "linkedin.person_publication",
    "linkedin.person_featured",
    "linkedin.person_interest",
    "linkedin.person_volunteering",
    "linkedin.person_test_score",
    "linkedin.person_service",
    "linkedin.person_connected_app",
    "linkedin.person_additional_section",
    "linkedin.person_recommendation",
    "linkedin.person_activity",
    "linkedin.person_activity_summary",
    "linkedin.company_observation",
    "linkedin.company_section_status",
    "linkedin.company_office",
    "linkedin.company_insight",
    "linkedin.company_job",
    "linkedin.company_post",
    "linkedin.job_observation",
    "linkedin.job_search_observation",
)

__all__ = ["metadata", "APPEND_ONLY_TABLES"]
