"""Rows into response shapes.

Every entity response carries when it was observed and the capture it came
from. Every section carries its status alongside its entries, preserving the
difference between empty, not captured, and failed extraction.

Which reading answers for a section is not decided here: `observe.resolve`
owns that, so the read side and the scrape side cannot drift apart.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from ..observe import resolve
from ..store.blobs import open_store
from ..store.config import artifact_url_ttl_seconds


COMPANY_SECTIONS = (
    "home", "events", "about", "services", "posts", "products", "jobs",
    "life", "people",
)


def _images(connection: Connection,
            capture_id: str | None) -> dict[str, str | None]:
    """The page's own pictures, as stored with the reading being served.

    Read from the capture rather than from a column, because that is where
    they are: the avatar and cover are fetched while the page is open and kept
    as artifacts, since their LinkedIn URLs are signed and expire in days. A
    column holding one of those URLs -- which is what `company_observation`
    still has -- points at nothing within a week.

    Keyed off the head capture, so an older instant serves the avatar that
    reading saw rather than the one on the profile today.

    Both keys are always present. A client that has to test for the field
    before testing its value cannot tell a picture we never took from a
    response shape it does not recognise.
    """
    absent: dict[str, str | None] = {"profile": None, "cover": None}
    if capture_id is None:
        return absent
    rows = connection.execute(text("""
        SELECT kind, blob_key FROM artifact
        WHERE capture_id = :c AND kind IN ('profile_image', 'cover_image')
        ORDER BY kind, ordinal
    """), {"c": capture_id}).all()
    if not rows:
        return absent
    keys: dict[str, str] = {}
    for row in rows:
        keys.setdefault(row.kind, row.blob_key)
    store = open_store()
    ttl = artifact_url_ttl_seconds()
    return {
        "profile": (store.url(keys["profile_image"], ttl)
                    if "profile_image" in keys else None),
        "cover": (store.url(keys["cover_image"], ttl)
                  if "cover_image" in keys else None),
    }


def _meta(found: dict[str, resolve.Resolution]) -> dict[str, dict[str, Any]]:
    """Which capture answered for each unit, and when it was read.

    These timestamps are stored nowhere -- they are simply which reading the
    walk stopped on, which is why a headline refreshed this morning and an
    experience section from last week report honestly different instants.
    """
    return {
        name: {
            "observed_at": (resolution.observed_at.isoformat()
                            if resolution.observed_at else None),
            "capture_id": resolution.capture_id,
            **({"status": resolution.status} if resolution.status else {}),
        }
        for name, resolution in found.items()
    }


def _as_of(moment: datetime, head: resolve.Resolution,
           pinned: bool) -> datetime:
    """The instant this answer is good as of.

    For a walk that is the question's upper bound -- now, or whatever `at`
    asked for. A pin asks no temporal question at all: it names one reading,
    and that reading's instant is the only currency it can claim. Reporting
    `now` there would stamp a month-old snapshot as current.
    """
    return head.observed_at if pinned else moment


def company(connection: Connection, company_url: str, *,
            at: datetime | None = None, allow_stale: bool = True,
            capture_id: str | None = None) -> dict[str, Any] | None:
    """Return the company as it stood at `at`, tab by tab.

    The same walk as `person`, over a vocabulary with one extra member:
    `not_available` means this company has no such tab, which is a reading and
    not a gap, so the walk stops on it rather than reaching further back.
    """
    moment = resolve.moment(at)
    if capture_id is not None:
        found = resolve.pinned(connection, kind=resolve.COMPANY,
                               key=company_url, capture_id=capture_id)
    else:
        found = resolve.walk(connection, kind=resolve.COMPANY,
                             key=company_url, at=moment,
                             allow_stale=allow_stale)
    head = found.get("company")
    if head is None:
        return None

    observation = connection.execute(text("""
        SELECT o.*, e.extracted_at, e.extractor_version, e.request_id
        FROM linkedin.company_observation o
        JOIN extraction e ON e.id = o.extraction_id
        WHERE o.id = :o
    """), {"o": head.observation_id}).one()

    def section_row(name: str):
        """That tab's row, from whichever observation the walk stopped on.

        None when nothing was read: either the tab has no reading at all, or
        the walk resolved it to a failed attempt, which carries a status but
        no observation to read a payload from.
        """
        resolution = found.get(name)
        if resolution is None or resolution.observation_id is None:
            return None
        return connection.execute(text("""
            SELECT section, status, source_url, reached_end, data
            FROM linkedin.company_section_status
            WHERE observation_id = :o AND section = :s
        """), {"o": resolution.observation_id, "s": name}).one_or_none()

    def section_status(name: str) -> str:
        """What became of this tab, whether or not there is a payload."""
        resolution = found.get(name)
        if resolution is None or resolution.status is None:
            return "not_captured"
        return resolution.status

    data = {
        "name": observation.name,
        "logo_url": observation.logo_url,
        "cover_image_url": observation.cover_image_url,
        "verified": observation.verified,
        "tagline": observation.tagline,
        "industry": observation.industry,
        "location": observation.location,
        "followers": observation.followers,
        "employee_count": observation.employee_count,
        "product_categories": observation.product_categories or [],
        "external_action": observation.external_action,
        "affiliated_pages": observation.affiliated_pages or [],
    }
    for section in COMPANY_SECTIONS:
        row = section_row(section)
        data[section] = {
            "status": section_status(section),
            "source_url": row.source_url if row is not None else None,
            "reached_end": row.reached_end if row is not None else None,
            "data": row.data if row is not None else None,
        }

    return {
        "entity": {
            "kind": "linkedin_company",
            "handle": observation.slug,
            "url": observation.company_url,
        },
        "as_of": _as_of(moment, head, capture_id is not None).isoformat(),
        "observed_at": head.observed_at.isoformat(),
        "capture_id": head.capture_id,
        "images": _images(connection, head.capture_id),
        "extractor_version": observation.extractor_version,
        "request_id": observation.request_id,
        "meta": _meta(found),
        "data": data,
    }


def person(connection: Connection, profile_url: str, *,
           at: datetime | None = None, allow_stale: bool = True,
           capture_id: str | None = None) -> dict[str, Any] | None:
    """Return the profile as it stood at `at`, section by section.

    Sections resolve independently, so a scrape that read only skills does not
    erase the experience a scrape last week read. They are never merged
    *within* a section: every experience row comes from one reading, because
    March's jobs beside June's would invent a history that never existed.
    """
    moment = resolve.moment(at)
    if capture_id is not None:
        found = resolve.pinned(connection, kind=resolve.PERSON,
                               key=profile_url,
                               capture_id=capture_id)
    else:
        found = resolve.walk(connection, kind=resolve.PERSON, key=profile_url,
                             at=moment, allow_stale=allow_stale)
    head = found.get("profile")
    if head is None:
        return None

    observation = connection.execute(text("""
        SELECT o.*, e.extracted_at, e.extractor_version, e.request_id
        FROM linkedin.person_observation o
        JOIN extraction e ON e.id = o.extraction_id
        WHERE o.id = :o
    """), {"o": head.observation_id}).one()

    def source(section: str) -> int | None:
        """The observation this section resolved to, if any did."""
        resolution = found.get(section)
        return resolution.observation_id if resolution else None

    statuses = {name: r.status for name, r in found.items()
                if r.status is not None}

    rows: dict[int, Any] = {head.observation_id: observation}

    def inline(section: str, column: str):
        """A section stored on the observation row rather than a child table.

        `causes` and `about_this_member` are columns, so they need the same
        resolution `source` gives every other section. Reading them off the
        head instead would pair an older reading's status with the newest
        scrape's empty column -- the exact mismatch the walk exists to stop.
        """
        other = source(section)
        if other is None:
            return None
        if other not in rows:
            rows[other] = connection.execute(text("""
                SELECT causes, about_this_member, contact_info,
                       open_to_work
                FROM linkedin.person_observation WHERE id = :o
            """), {"o": other}).one()
        return getattr(rows[other], column)

    # Public API field, parser/capture section name, projection table. Most
    # spellings coincide; the explicit middle value prevents snake_case API
    # names from silently looking up a status that is stored with hyphens.
    provisional = (
        ("certifications", "certifications", "person_certification"),
        ("projects", "projects", "person_project"),
        ("courses", "courses", "person_course"),
        ("languages", "languages", "person_language"),
        ("honors", "honors", "person_honor"),
        ("organizations", "organizations", "person_organization"),
        ("patents", "patents", "person_patent"),
        ("publications", "publications", "person_publication"),
        ("featured", "featured", "person_featured"),
        ("volunteering", "volunteering", "person_volunteering"),
        ("test_scores", "test-scores", "person_test_score"),
        ("services", "services", "person_service"),
        ("connected_apps", "connected-apps", "person_connected_app"),
    )
    return {
        "entity": {"kind": "linkedin_profile", "handle": observation.handle,
                   "url": observation.profile_url},
        # What was asked for, and what the head document itself carries. A
        # section's own instant is in `meta`, because with a mixed cadence
        # there is no single one for the whole profile.
        "as_of": _as_of(moment, head, capture_id is not None).isoformat(),
        "observed_at": head.observed_at.isoformat(),
        "capture_id": head.capture_id,
        "images": _images(connection, head.capture_id),
        "extractor_version": observation.extractor_version,
        "request_id": observation.request_id,
        "meta": _meta(found),
        "data": {
            "name": observation.name,
            "headline": observation.headline,
            "location": observation.location,
            "about": observation.about,
            "connections": observation.connections,
            "followers": observation.followers,
            "pronouns": observation.pronouns,
            "current_company": observation.current_company,
            "current_school": observation.current_school,
            "contact_info_url": observation.contact_info_url,
            "top_skills": observation.top_skills or [],
            # A section with a status, not a scalar: `parsed` where its card
            # was captured, `parsed_inline` where only the profile page had
            # it, `not_captured` where the profile carried no card at all --
            # which a bare list could not tell apart.
            "causes": _section(statuses, "causes",
                               inline("causes", "causes") or []),
            # Read from the dialog behind the profile's More button rather
            # than from the page, which is why it carries a status of its own
            # and why an operator asking when an account was made has nowhere
            # else to look.
            "about_this_member": _about_this_member(
                statuses,
                inline("about-this-member", "about_this_member")),
            # The dialog the top card links to. `contact_info_url` above is
            # that link; this is what was behind it.
            "contact_info": _contact_info(
                statuses, inline("contact-info", "contact_info")),
            # Absent on most profiles: a member who is not looking has no
            # card, so the status carries that and there is nothing beside it.
            "open_to_work": _open_to_work(
                statuses, inline("open-to-work", "open_to_work")),
            "experience": _section(statuses, "experience",
                                   _experience(connection,
                                               source("experience"))),
            "education": _section(statuses, "education",
                                  _education(connection, source("education"))),
            "skills": _section(statuses, "skills",
                               _skills(connection, source("skills"))),
            **{field: _section(
                    statuses, section,
                    _provisional(connection, source(section), table))
               for field, section, table in provisional},
            "recommendations": {
                "received": _section(
                    statuses, "recommendations",
                    _recommendations(connection, source("recommendations"),
                                     "received")),
                "given": _section(
                    statuses, "recommendations-given",
                    _recommendations(connection,
                                     source("recommendations-given"),
                                     "given")),
            },
            "activity": {
                plural: _activity_section(
                    connection, source(f"activity-{plural}"), statuses,
                    plural, kind)
                for plural, kind in (
                    ("posts", "post"), ("comments", "comment"),
                    ("articles", "article"), ("reactions", "reaction"))
            },
            "interests": _interests_section(connection, source("interests"),
                                            statuses),
        },
    }


def _section(statuses: dict, name: str, entries: list) -> dict:
    return {"status": statuses.get(name, "not_captured"), "entries": entries}


# What the dialog holds when there was no dialog to read. Spelled out rather
# than left absent so a consumer reads the same keys either way, and so the
# status is the only thing that has to be checked to know which it was.
EMPTY_ABOUT_THIS_MEMBER = {
    "account_history": {"rows": []},
    "verifications": [],
    "links": [],
}


def _open_to_work(statuses: dict, stored: dict | None) -> dict:
    """What the member is open to, with the status of the read that found it.

    Only what they set, for the reason `_contact_info` reports only what was
    published: the status already says whether the dialog was read.
    """
    return {
        "status": statuses.get("open-to-work", "not_captured"),
        **(stored or {}),
    }


def _contact_info(statuses: dict, stored: dict | None) -> dict:
    """The Contact info dialog, with the status of the read that found it.

    One record rather than a list, so it takes the same flat shape the About
    this member dialog takes rather than the {status, entries} envelope the
    list sections use.

    Only what the member published, unlike About this member above. Padding
    the absent keys served nine nulls around the two answers that were real,
    and said nothing a caller could not already read off the status: the
    dialog was opened, and this is what was in it.
    """
    return {
        "status": statuses.get("contact-info", "not_captured"),
        **(stored or {}),
    }


def _about_this_member(statuses: dict, stored: dict | None) -> dict:
    """The About this member dialog, with the status of the read that found it.

    Not the {status, entries} envelope the list sections use: this section is
    one record rather than a list of them, so a one-item list would claim there
    could have been several. `interests` departs from that envelope for the
    same kind of reason.

    Whatever the extractor stored is passed through whole, so a key a later
    schema adds reaches a caller without this function being touched.
    """
    return {
        "status": statuses.get("about-this-member", "not_captured"),
        **EMPTY_ABOUT_THIS_MEMBER,
        **(stored or {}),
    }


def _experience(connection: Connection,
                observation: int | None) -> list[dict]:
    if observation is None:
        return []
    employers = connection.execute(text("""
        SELECT * FROM linkedin.person_employer
        WHERE observation_id = :o ORDER BY ordinal
    """), {"o": observation}).all()
    output = []
    for employer in employers:
        # A person's jobs at this employer. `role` here is the position they
        # held, which is not the `section` of a captured page -- same word,
        # unrelated meanings, and only the second one was renamed.
        roles = connection.execute(text("""
            SELECT * FROM linkedin.person_role
            WHERE employer_id = :e ORDER BY ordinal
        """), {"e": employer.id}).all()
        employer_data = dict(employer.data or {})
        output.append(employer_data | {
            "company": employer.name,
            "company_id": employer.company_id,
            "company_url": employer.company_url,
            "employment_type": employer.employment_type,
            "location": employer.location,
            "total_duration": employer.total_duration,
            "roles": [dict(role.data or {}) | {
                "title": role.title,
                "employment_type": role.employment_type,
                "date_range": role.date_range,
                "started_on": role.started_on,
                "ended_on": role.ended_on,
                "is_current": role.is_current,
                "duration": role.duration,
                "location": role.location,
                "description": role.description,
                "skills": role.skills,
            } for role in roles],
        })
    return output


def _education(connection: Connection,
               observation: int | None) -> list[dict]:
    if observation is None:
        return []
    rows = connection.execute(text("""
        SELECT * FROM linkedin.person_education
        WHERE observation_id = :o ORDER BY ordinal
    """), {"o": observation}).all()
    return [dict(row.data or {}) | {
        "school": row.school, "school_url": row.school_url, "degree": row.degree,
        "field_of_study": row.field_of_study, "date_range": row.date_range,
        "started_on": row.started_on, "ended_on": row.ended_on,
        "is_current": row.is_current,
        "grade": row.grade, "activities": row.activities,
        "description": row.description,
    } for row in rows]


def _provisional(connection: Connection, observation: int | None,
                 table: str, *, category: bool = False) -> list[dict]:
    if observation is None:
        return []
    # `table` comes only from the constant mapping in person(), never input.
    rows = connection.execute(text(f"""
        SELECT * FROM linkedin.{table}
        WHERE observation_id = :o ORDER BY ordinal
    """), {"o": observation}).all()
    output = []
    for row in rows:
        entry = dict(row.data or {})
        if not entry:
            entry = {"name": row.name, "detail": row.detail,
                     "date_range": row.date_range, "url": row.url,
                     "text": row.text}
        if category and row.category is not None:
            entry["category"] = row.category
        output.append(entry)
    return output


def _skills(connection: Connection,
            observation: int | None) -> list[dict]:
    if observation is None:
        return []
    rows = connection.execute(text("""
        SELECT name, detail, url, text, details, data
        FROM linkedin.person_skill
        WHERE observation_id = :o ORDER BY ordinal
    """), {"o": observation}).all()
    output = []
    for row in rows:
        if row.data:
            output.append(dict(row.data))
            continue
        details = row.details
        if details is None and row.detail:
            details = [{"text": row.detail, "url": row.url}]
        entry = {"name": row.name, "details": details or []}
        if row.text:
            entry["text"] = row.text
        output.append(entry)
    return output


INTEREST_CATEGORY_ROLES = {
    "top_voices": "interests",
    "companies": "interests-companies",
    "groups": "interests-groups",
    "newsletters": "interests-newsletters",
    "schools": "interests-schools",
}


def _interests_section(connection: Connection, observation: int | None,
                       statuses: dict) -> dict:
    if observation is None:
        return {"status": statuses.get("interests", "not_captured"),
                "captured_categories": [], "categories": {}}
    rows = connection.execute(text("""
        SELECT category, source_url, data
        FROM linkedin.person_interest
        WHERE observation_id = :o ORDER BY ordinal
    """), {"o": observation}).all()
    captured_categories: list[str] = []
    categories: dict[str, dict[str, Any]] = {}
    for row in rows:
        category = row.category or "unknown"
        if category not in captured_categories:
            captured_categories.append(category)
        envelope = categories.setdefault(category, {
            "status": statuses.get(
                INTEREST_CATEGORY_ROLES.get(category, "interests"),
                statuses.get("interests", "not_captured"),
            ),
            "source_url": row.source_url,
            "entries": [],
        })
        if envelope["source_url"] is None and row.source_url is not None:
            envelope["source_url"] = row.source_url
        entry = dict(row.data or {})
        entry.pop("category", None)
        envelope["entries"].append(entry)
    return {
        "status": statuses.get("interests", "not_captured"),
        "captured_categories": captured_categories,
        "categories": categories,
    }


def _recommendations(connection: Connection, observation: int | None,
                     direction: str) -> list[dict]:
    if observation is None:
        return []
    rows = connection.execute(text("""
        SELECT person_name, person_headline, person_company, connection_degree,
               profile_url, recommended_at, relationship, text, data
        FROM linkedin.person_recommendation
        WHERE observation_id = :o AND direction = :d ORDER BY ordinal
    """), {"o": observation, "d": direction}).all()
    return [dict(row.data) if row.data else {
        "person": {
            "name": row.person_name,
            "headline": row.person_headline,
            "company": row.person_company,
            "connection_degree": row.connection_degree,
            "profile_url": row.profile_url,
        },
        "date": row.recommended_at,
        "relationship": row.relationship,
        "text": row.text,
    } for row in rows]


def _activity(connection: Connection, observation: int,
              kind: str) -> list[dict]:
    rows = connection.execute(text("""
        SELECT * FROM linkedin.person_activity
        WHERE observation_id = :o AND kind = :kind ORDER BY ordinal
    """), {"o": observation, "kind": kind}).all()
    return [dict(row.data) if row.data else {
        "author": {
            "name": row.author_name,
            "title": row.author_title,
            "company": row.author_company,
            "connection_level": row.author_connection_level,
            "url": row.author_url,
            "badges": row.author_badges or [],
        },
        "timestamp": row.posted_at,
        "edited": row.edited,
        "visibility": row.visibility,
        "url": row.url,
        "content": {"text": row.text, "media": row.media or []},
        "engagement": row.engagement or {},
    } for row in rows]


def _activity_section(connection: Connection, observation: int | None,
                      statuses: dict, plural: str, kind: str) -> dict:
    if observation is None:
        return _section(statuses, f"activity-{plural}", [])
    summary = connection.execute(text("""
        SELECT source_url, loaded, captured, complete
        FROM linkedin.person_activity_summary
        WHERE observation_id = :o AND kind = :kind
    """), {"o": observation, "kind": kind}).one_or_none()
    result = _section(
        statuses, f"activity-{plural}",
        _activity(connection, observation, kind))
    if summary is not None:
        result.update({
            "source_url": summary.source_url,
            "loaded": summary.loaded,
            "captured": summary.captured,
            "complete": summary.complete,
        })
    return result


def _section_statuses(connection: Connection,
                      extraction_id: str) -> list[dict[str, str]]:
    """How each section of one extraction read.

    Projected rows rather than the stored document: the projection is already
    keyed by section, and reading a multi-megabyte JSON blob to answer "what
    happened to honors" would be the wrong shape of work.

    Person and company are asked separately rather than unioned. Only one can
    match a given extraction, an empty result from the other costs an index
    probe, and a UNION here would need the two schemas to keep agreeing about
    column order forever.
    """
    for table in ("person_section_status", "person_observation"), \
                 ("company_section_status", "company_observation"):
        rows = connection.execute(text(f"""
            SELECT s.section, s.status
            FROM linkedin.{table[0]} s
            JOIN linkedin.{table[1]} o ON o.id = s.observation_id
            WHERE o.extraction_id = :e
            ORDER BY s.section
        """), {"e": extraction_id}).all()
        if rows:
            return [{"section": row.section, "status": row.status}
                    for row in rows]
    return []


def scrape_request(connection: Connection,
                   request_id: str) -> dict[str, Any] | None:
    """Project one request for the public surface."""
    row = connection.execute(text(
        "SELECT * FROM scrape_request WHERE id = :r"),
        {"r": request_id}).one_or_none()
    if row is None:
        return None

    # The attempt's ordinal rather than its id: a retried request keeps every
    # page it ever captured, and without this the console cannot say which run
    # produced which page. NULL for pre-fleet captures, which had one attempt.
    captures = connection.execute(text("""
        SELECT c.id, c.section, c.url_final, c.http_status, c.captured_at,
               c.browser_profile, attempt.attempt_no
        FROM capture c
        LEFT JOIN scrape_attempt attempt ON attempt.id = c.attempt_id
        WHERE c.request_id = :r ORDER BY c.captured_at
    """), {"r": request_id}).all()
    extraction = connection.execute(text("""
        SELECT id, extractor, extractor_version, extracted_at, outcome
        FROM extraction WHERE request_id = :r ORDER BY extracted_at DESC LIMIT 1
    """), {"r": request_id}).one_or_none()
    sections = (_section_statuses(connection, extraction.id)
                if extraction else [])
    attempts = connection.execute(text("""
        SELECT attempt.attempt_no, attempt.worker, attempt.browser_profile,
               attempt.started_at, attempt.ended_at, attempt.cause,
               attempt.cause_detail,
               supervisor.id AS supervisor_id,
               supervisor.label AS supervisor_label,
               worker.id AS worker_id, worker.local_name AS worker_name,
               account.id AS account_id, account.label AS account_label,
               assignment.id AS assignment_id
        FROM scrape_attempt attempt
        LEFT JOIN supervisor ON supervisor.id=attempt.supervisor_id
        LEFT JOIN worker ON worker.id=attempt.worker_id
        LEFT JOIN linkedin_account account ON account.id=attempt.account_id
        LEFT JOIN account_worker_assignment assignment
          ON assignment.id=attempt.assignment_id
        WHERE attempt.request_id = :r ORDER BY attempt.attempt_no
    """), {"r": request_id}).all()
    # The timings and the failure are derived rather than stored, so a consumer
    # written against the old scrape_request columns keeps working while the
    # rows they came from live somewhere else.
    last = attempts[-1] if attempts else None

    from .requests import display_target

    return {
        "request_id": row.id,
        "priority": row.priority,
        "label": display_target(
            row.label, url=row.url_normalised, query=row.query_requested),
        "url": row.url_normalised,
        "url_requested": row.url_requested,
        "query": row.query_requested,
        "normalised_query": row.query_normalised,
        "top_n": row.google_top_n,
        "target": ({
            "kind": "google_search", "query": row.query_requested,
            "top_n": row.google_top_n,
        } if row.kind == "google_search" else {
            "kind": row.kind, "url": row.url_normalised,
        }),
        "kind": row.kind,
        "status": row.status,
        # The pacing this run gets, from the request rather than from whatever
        # the journal happened to mention. Every kind has one -- the column is
        # NOT NULL with a default -- and only one capture path ever wrote a
        # tempo into its phase detail, which is why reading it back from there
        # reported Google searches as unpaced.
        "tempo": row.tempo,
        "linkedin_sections": row.linkedin_sections,
        "requested_at": row.requested_at.isoformat(),
        "started_at": (attempts[0].started_at.isoformat()
                       if attempts and attempts[0].started_at else None),
        # A satisfied request never ran, so it has no attempt to end. The
        # instant it was answered is its ending -- same COALESCE the list
        # route makes, so the two views of one request agree.
        "finished_at": (last.ended_at.isoformat()
                        if last is not None and last.ended_at
                        else row.satisfied_at.isoformat()
                        if row.satisfied_at else None),
        "failure": ({"kind": last.cause, "detail": last.cause_detail}
                    if last is not None and last.cause else None),
        # How many attempts this request may spend, and when the next one is
        # due. `next_attempt_at` in the future on a queued request is what
        # "retrying" means -- there is no separate status for it.
        "max_attempts": row.max_attempts,
        "next_attempt_at": (row.next_attempt_at.isoformat()
                            if row.next_attempt_at else None),
        # Intent, not outcome. Set while a run is still going, which is what
        # lets the console say "Stopping…" instead of showing an unchanged
        # `running` for up to a page's duration.
        "cancel_requested_at": (row.cancel_requested_at.isoformat()
                                if row.cancel_requested_at else None),
        "freshness_seconds": row.freshness_seconds,
        "satisfied_at": (row.satisfied_at.isoformat()
                         if row.satisfied_at else None),
        # Which stored captures this answer rests on, for the request that
        # took none of its own. The first question an operator asks about a
        # satisfied request is what satisfied it.
        "satisfied_by": _satisfied_by(connection, request_id),
        "attempts": [{
            "n": attempt.attempt_no,
            "started_at": (attempt.started_at.isoformat()
                           if attempt.started_at else None),
            "ended_at": (attempt.ended_at.isoformat()
                         if attempt.ended_at else None),
            "cause": attempt.cause,
            "cause_detail": attempt.cause_detail,
        } for attempt in attempts],
        "captures": [{
            "id": capture.id, "section": capture.section,
            "url_final": capture.url_final, "http_status": capture.http_status,
            "captured_at": capture.captured_at.isoformat(),
            "attempt": capture.attempt_no,
        } for capture in captures],
        "extraction": ({
            "id": extraction.id, "extractor": extraction.extractor,
            "version": extraction.extractor_version,
            "extracted_at": extraction.extracted_at.isoformat(),
            "outcome": extraction.outcome,
        } if extraction else None),
        # What became of each section, whether or not it produced a capture.
        # A short section has no "Show all" link and so no page of its own:
        # LinkedIn renders it inline, the parser reads it off the profile, and
        # nothing about it appears among the captures. Neither does a section
        # that was asked for and never arrived. Both are answers a caller
        # needs, and the captures table cannot give either.
        "sections": sections,
    }


def _satisfied_by(connection: Connection, request_id: str) -> dict | None:
    """What answered a request that needed no work, or None if work was done."""
    rows = connection.execute(text("""
        SELECT section, capture_id, observed_at FROM request_satisfaction
        WHERE request_id = :r ORDER BY section
    """), {"r": request_id}).all()
    if not rows:
        return None
    return {
        # The guarantee delivered: every unit is at least this recent.
        "oldest_observed_at": min(row.observed_at for row in rows).isoformat(),
        "sections": [{"section": row.section, "capture_id": row.capture_id,
                   "observed_at": row.observed_at.isoformat()}
                  for row in rows],
    }


