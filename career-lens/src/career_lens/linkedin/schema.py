"""What a LinkedIn profile means, in its own Postgres schema.

Every table hangs off `person_observation`, which hangs off `extraction`. A
re-extraction inserts a new observation and a new set of children; nothing is
ever updated. Reading "the current value" is reading the newest observation.

Sixteen of these tables are provisional. The parser emits sections it has no
semantic columns for yet, and rather than guess at those columns they share one
deliberately generic entry shape plus the parser's lossless `text`/`links`
snapshot, so nothing is lost while the shape is unknown. Altering an empty
table is free, which is why they exist now: the shape of the data is visible in
the schema and in the API from the first day.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Column, ForeignKey, Index, Integer,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from ..store.schema.common import APPEND_ONLY, TS, enum_check, pk, table

SCHEMA = "linkedin"

# The parser's own vocabulary, verbatim. `parsed` includes a legitimately empty
# list; `fallback` means only normalised text and links survived; `error` means
# the document could not be parsed at all. Collapsing any two of these loses the
# distinction the whole section-status idea exists for.
SECTION_STATUSES = (
    "parsed", "parsed_inline", "fallback", "not_captured", "not_requested",
    # The profile page linked into no such section, so this member has none.
    # Distinct from `not_captured`, which says only that no capture arrived --
    # a conflation that made a member with no patents permanently unsatisfiable
    # by a freshness check, because the patents it would never have never
    # arrived.
    "not_available",
    "error", "drift")

# The historical person vocabulary remains accepted here so append-only
# company observations written before the company-specific split stay valid.
# `not_available` is now in both and is spelled once.
COMPANY_SECTION_STATUSES = SECTION_STATUSES + ("parse_failed",)

# Every section name the parser may report on, which is also every `section` a
# capture may carry apart from `profile` and `page`. One vocabulary shared by
# the capture layer and the parser means the projection needs no translation.
SECTION_NAMES = (
    "experience", "education", "skills", "certifications", "projects",
    "courses", "languages", "honors", "organizations", "patents",
    "publications", "recommendations", "recommendations-given",
    "activity-posts", "activity-comments", "activity-articles",
    "activity-reactions",
    "featured", "about-this-member",
    # The Contact info dialog, captured the way about-this-member is: a panel
    # the profile links to, read on its own rather than as part of the page.
    "contact-info",
    # The Open to Work dialog, opened from the card's "Show details".
    "open-to-work",
    # Captured by sections.py and extracted by nobody, so it arrives in the
    # parser's `additional_sections`. Named here because a capture carries it
    # as a section and the status table must be able to record what became of it.
    "interests",
    # Public profile collections observed in the profile card or behind their
    # own detail/service page. These deliberately use the capture vocabulary,
    # including hyphens, so status resolution needs no hidden translation.
    "volunteering", "test-scores", "services", "connected-apps",
    # LinkedIn offers no causes page, so this capture is the card read off the
    # profile -- the same way Featured is captured on a profile that has no
    # page for it.
    "causes",
)

# Every company tab a capture may carry as a section, apart from `company` -- the
# head document, excluded exactly as `profile` is excluded above. The
# `jobs-page-N` sections cannot be members either, because they carry an ordinal;
# they are recorded against `jobs`, with each listing identified by
# company_job.job_id.
# `product` is singular on purpose. LinkedIn labels the tab "Products" in the
# nav and serves it at /company/<slug>/product/, and a section is the path segment
# rather than the label -- so the plural spelling here matched nothing. Measured
# on a live Stripe capture (2026-08-13), which is also where dom.TAB_MARKERS
# gets its singular `product` key.
COMPANY_TAB_NAMES = (
    "about", "people", "jobs", "posts", "life", "product", "events",
    "services",
)


person_observation = table(
    "person_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False),
    Column("handle", Text, nullable=False),
    Column("profile_url", Text, nullable=False),
    Column("name", Text),
    Column("headline", Text),
    Column("location", Text),
    Column("about", Text),
    Column("connections", Text),
    Column("followers", Text),
    Column("pronouns", Text),
    Column("current_company", Text),
    Column("current_school", Text),
    Column("contact_info_url", Text),
    Column("top_skills", JSONB),
    Column("causes", JSONB),
    # A single object whose shape is not yet stable, so JSONB rather than
    # columns invented from one example.
    Column("about_this_member", JSONB),
    # What the Contact info dialog held. `contact_info_url` above is the link
    # that opens it and says nothing of what is behind it. JSONB because which
    # details a member publishes varies, and the parser keeps a label it has no
    # key for rather than dropping it.
    Column("contact_info", JSONB),
    # What the member is open to. The card on the profile says only that they
    # are; this is what they said they would take.
    Column("open_to_work", JSONB),
    # When this reading was taken, which is the capture's time and not the
    # extraction's. The walk orders by this, so a re-extraction of an old
    # capture keeps that capture's place in the history rather than displacing
    # whatever happens to be newest.
    Column("observed_at", TS, nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

# An Index takes no `schema=`: it belongs to the table whose columns it
# indexes, so `linkedin` comes from person_observation and the DDL is already
# CREATE INDEX ... ON linkedin.person_observation. Passing it raises TypeError.
# Alembic's op.create_index() in the migration is a different API and does
# take schema= -- that is not an inconsistency to tidy away.
Index("ix_person_observation_profile_url", person_observation.c.profile_url,
      person_observation.c.id.desc())
Index("ix_person_observation_handle", person_observation.c.handle)

# The walk's read: the newest reading of this profile at or before an instant.
Index("ix_person_observation_walk", person_observation.c.profile_url,
      person_observation.c.observed_at.desc(),
      person_observation.c.id.desc())


person_section_status = table(
    "person_section_status",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("section", Text, nullable=False),
    Column("status", Text, nullable=False),
    # This section's own capture, or the head document's when it was parsed
    # inline. Denormalised from extraction_input at projection time: both rows
    # are append-only and written in one transaction, so they cannot drift.
    Column("observed_at", TS, nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    CheckConstraint(enum_check("status", SECTION_STATUSES), name="status"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_section_status", person_section_status.c.observation_id,
      person_section_status.c.section, unique=True)

Index("ix_person_section_status_walk", person_section_status.c.section,
      person_section_status.c.observed_at.desc(),
      person_section_status.c.id.desc())


person_employer = table(
    "person_employer",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("name", Text),
    # LinkedIn's own numeric id, out of href="/company/162479/". The slug can
    # change and this does not, which is what makes it the join key between a
    # person's history and a company record.
    Column("company_id", Text),
    Column("company_url", Text),
    Column("employment_type", Text),
    Column("location", Text),
    Column("total_duration", Text),
    Column("text", Text),
    Column("links", JSONB),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_employer", person_employer.c.observation_id,
      person_employer.c.ordinal, unique=True)
Index("ix_person_employer_company_id", person_employer.c.company_id)


person_role = table(
    "person_role",
    pk(),
    Column("employer_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_employer.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("title", Text),
    Column("employment_type", Text),
    # Kept exactly as the parser emitted it. The three columns below are
    # derived from it, and keeping both means a derived value can always be
    # checked against the evidence it came from.
    Column("date_range", Text),
    Column("started_on", Text),
    Column("ended_on", Text),
    Column("is_current", Boolean),
    Column("duration", Text),
    Column("location", Text),
    Column("description", Text),
    Column("skills", JSONB),
    Column("url", Text),
    Column("text", Text),
    Column("links", JSONB),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_role", person_role.c.employer_id, person_role.c.ordinal,
      unique=True)


person_education = table(
    "person_education",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("school", Text),
    Column("school_url", Text),
    Column("degree", Text),
    Column("field_of_study", Text),
    Column("date_range", Text),
    Column("started_on", Text),
    Column("ended_on", Text),
    Column("is_current", Boolean),
    Column("grade", Text),
    Column("activities", Text),
    Column("description", Text),
    Column("skills", JSONB),
    Column("text", Text),
    Column("links", JSONB),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_education", person_education.c.observation_id,
      person_education.c.ordinal, unique=True)


# --- provisional ---------------------------------------------------------
#
# One shape for sixteen sections, because the parser does not yet emit named
# fields for any of them. `text` and `links` are its lossless snapshot, so a
# section whose semantic columns are empty has still kept its content.
#
# PROVISIONAL. When the parser starts emitting real fields for one of these,
# that table gets the columns it deserves -- and while it holds no rows, that
# is a migration with no backfill.

PROVISIONAL_SECTIONS = (
    ("person_skill", "skills"),
    ("person_certification", "certifications"),
    ("person_project", "projects"),
    ("person_course", "courses"),
    ("person_language", "languages"),
    ("person_honor", "honors"),
    ("person_organization", "organizations"),
    ("person_patent", "patents"),
    ("person_publication", "publications"),
    ("person_featured", "featured"),
    ("person_interest", "interests"),
    ("person_volunteering", "volunteering"),
    ("person_test_score", "test-scores"),
    ("person_service", "services"),
    ("person_connected_app", "connected-apps"),
    ("person_additional_section", None),
)


def _provisional(name: str) -> None:
    columns = [
        pk(),
        Column("observation_id", BigInteger,
               ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
               nullable=False),
        Column("ordinal", Integer, nullable=False),
        Column("name", Text),
        Column("detail", Text),
        Column("date_range", Text),
        Column("url", Text),
        Column("text", Text),
        Column("links", JSONB),
        Column("data", JSONB),
    ]
    if name == "person_additional_section":
        # The one that also has to say which section it came from, because it
        # holds every section name the parser did not recognise.
        columns.insert(3, Column("section", Text, nullable=False))
    if name == "person_skill":
        columns.append(Column("details", JSONB))
    if name == "person_interest":
        # Existing 1.1 observations stored interests as a generic snapshot and
        # cannot be assigned a category retrospectively without inventing one.
        columns.insert(3, Column("category", Text))
        columns.insert(4, Column("source_url", Text))
    built = table(name, *columns, schema=SCHEMA, mutability=APPEND_ONLY)
    Index(f"uq_{name}", built.c.observation_id, built.c.ordinal, unique=True)
    return built


for _name, _section in PROVISIONAL_SECTIONS:
    globals()[_name] = _provisional(_name)


# Two rows the shape does not fit, because the parser splits them by a
# discriminator rather than by section name.
person_recommendation = table(
    "person_recommendation",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("direction", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("person_name", Text),
    Column("person_headline", Text),
    Column("person_company", Text),
    Column("connection_degree", Text),
    Column("profile_url", Text),
    Column("recommended_at", Text),
    Column("relationship", Text),
    Column("text", Text),
    Column("links", JSONB),
    Column("data", JSONB),
    CheckConstraint(enum_check("direction", ("received", "given")),
                    name="direction"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_recommendation", person_recommendation.c.observation_id,
      person_recommendation.c.direction, person_recommendation.c.ordinal,
      unique=True)


person_activity = table(
    "person_activity",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("kind", Text, nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("author_name", Text),
    Column("author_title", Text),
    Column("author_company", Text),
    Column("author_connection_level", Text),
    Column("author_url", Text),
    Column("author_badges", JSONB),
    Column("posted_at", Text),
    Column("edited", Boolean),
    Column("visibility", Text),
    Column("url", Text),
    Column("text", Text),
    Column("media", JSONB),
    Column("engagement", JSONB),
    Column("links", JSONB),
    Column("data", JSONB),
    CheckConstraint(enum_check(
        "kind", ("post", "comment", "article", "reaction")),
                    name="kind"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_activity", person_activity.c.observation_id,
      person_activity.c.kind, person_activity.c.ordinal,
      unique=True)


person_activity_summary = table(
    "person_activity_summary",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.person_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("kind", Text, nullable=False),
    Column("source_url", Text),
    Column("loaded", Integer),
    Column("captured", Integer, nullable=False),
    Column("complete", Boolean),
    CheckConstraint(enum_check(
        "kind", ("post", "comment", "article", "reaction")),
                    name="kind"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_person_activity_summary", person_activity_summary.c.observation_id,
      person_activity_summary.c.kind, unique=True)


# The company document is kept in object storage; these tables are its
# queryable projection. Heterogeneous tab payloads remain lossless in
# company_section_status.data while stable repeated entities get typed rows.
company_observation = table(
    "company_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False),
    Column("slug", Text, nullable=False),
    Column("company_url", Text),
    Column("name", Text),
    Column("logo_url", Text),
    Column("cover_image_url", Text),
    Column("verified", Boolean, nullable=False, server_default="false"),
    Column("tagline", Text),
    Column("description", Text),
    Column("industry", Text),
    Column("location", Text),
    # Display precision is preserved: compact values such as 2M are not
    # expanded into an invented exact count.
    Column("followers", Text),
    Column("employee_count", Text),
    Column("company_size", Text),
    Column("associated_members_count", BigInteger),
    Column("founded", Text),
    Column("website", Text),
    Column("follower_count", BigInteger),
    Column("product_categories", JSONB),
    Column("external_action", JSONB),
    Column("affiliated_pages", JSONB),
    # When this reading was taken, which is the capture's time and not the
    # extraction's. The walk orders by this, so a re-extraction of an old
    # capture keeps that capture's place in the history rather than displacing
    # whatever happens to be newest.
    Column("observed_at", TS, nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("ix_company_observation_slug", company_observation.c.slug,
      company_observation.c.id.desc())

Index("ix_company_observation_walk", company_observation.c.company_url,
      company_observation.c.observed_at.desc(),
      company_observation.c.id.desc())


# Exactly the shape dom.company_locations() emits, field for field. Latitude
# and longitude are Text because that is how they survive the response
# untouched; converting them is the parser's decision, not the schema's.
company_office = table(
    "company_office",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.company_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("name", Text),
    Column("description", Text),
    Column("headquarters", Boolean, nullable=False, server_default="false"),
    Column("line1", Text),
    Column("line2", Text),
    Column("city", Text),
    Column("geographic_area", Text),
    Column("postal_code", Text),
    Column("country", Text),
    Column("latitude", Text),
    Column("longitude", Text),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)


# One value from a People-tab analytics card. The parser only emits a numeric
# count after LinkedIn's displayed value has been parsed without ambiguity;
# the original item remains available in `data`.
company_insight = table(
    "company_insight",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.company_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("card_ordinal", Integer, nullable=False),
    Column("item_ordinal", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("label", Text),
    Column("member_count", BigInteger),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)


# One listing parsed out of a jobs-page-N results capture. Those captures are
# lists of roughly 25 postings each, not single job pages.
company_job = table(
    "company_job",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.company_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("job_id", Text),
    Column("title", Text),
    Column("company_name", Text),
    Column("location", Text),
    Column("workplace_type", Text),
    Column("posted_at", Text),
    Column("posting_status", Text),
    Column("url", Text),
    Column("page", Integer),
    Column("position", Integer),
    Column("insights", JSONB),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)


company_post = table(
    "company_post",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.company_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("activity_id", Text),
    Column("relation", Text),
    Column("author_name", Text),
    Column("author_followers", BigInteger),
    Column("text", Text),
    Column("posted_at", Text),
    Column("url", Text),
    Column("media", JSONB),
    Column("engagement", JSONB),
    Column("data", JSONB),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)


# Whether a company tab existed, was captured, and parsed successfully.
company_section_status = table(
    "company_section_status",
    pk(),
    Column("observation_id", BigInteger,
           ForeignKey(f"{SCHEMA}.company_observation.id", ondelete="RESTRICT"),
           nullable=False),
    Column("section", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("source_url", Text),
    Column("reached_end", Boolean),
    Column("data", JSONB),
    # This tab's own capture, or the head document's when nothing was fetched
    # for it. Denormalised from extraction_input at projection time: both rows
    # are append-only and written in one transaction, so they cannot drift.
    Column("observed_at", TS, nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT")),
    CheckConstraint(enum_check("status", COMPANY_SECTION_STATUSES), name="status"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)

Index("uq_company_section_status", company_section_status.c.observation_id,
      company_section_status.c.section, unique=True)

Index("ix_company_section_status_walk", company_section_status.c.section,
      company_section_status.c.observed_at.desc(),
      company_section_status.c.id.desc())


# One posting, read signed in. The full document, with the description, is the
# extraction's JSON object; this row is what a lookup by URL needs.
job_observation = table(
    "job_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False,
           unique=True),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT"),
           nullable=False),
    Column("job_url", Text, nullable=False),
    Column("job_id", Text, nullable=False),
    Column("title", Text),
    Column("company_name", Text),
    Column("posting_status", Text),
    Column("observed_at", TS, nullable=False),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)
Index("ix_job_observation_url", job_observation.c.job_url,
      job_observation.c.observed_at.desc())
Index("ix_job_observation_request", job_observation.c.request_id)


# One keyword search over LinkedIn jobs. The result list is in the
# extraction's JSON object.
job_search_observation = table(
    "job_search_observation",
    pk(),
    Column("extraction_id", Text,
           ForeignKey("extraction.id", ondelete="RESTRICT"), nullable=False,
           unique=True),
    Column("request_id", Text,
           ForeignKey("scrape_request.id", ondelete="RESTRICT"),
           nullable=False),
    Column("capture_id", Text, ForeignKey("capture.id", ondelete="RESTRICT"),
           nullable=False),
    Column("search_url", Text, nullable=False),
    Column("keywords", Text, nullable=False),
    Column("location", Text),
    Column("captured_pages", Integer, nullable=False),
    # NULL when the last page did not say whether more exist.
    Column("reached_end", Boolean),
    Column("result_count", Integer, nullable=False),
    Column("observed_at", TS, nullable=False),
    CheckConstraint("captured_pages >= 1", name="captured_pages"),
    CheckConstraint("result_count >= 0", name="result_count"),
    schema=SCHEMA,
    mutability=APPEND_ONLY,
)
Index("ix_job_search_observation_url", job_search_observation.c.search_url,
      job_search_observation.c.observed_at.desc())
Index("ix_job_search_observation_request", job_search_observation.c.request_id)
