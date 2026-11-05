"""The parser's document into rows.

The document is the contract; these tables are a projection of it. That is what
absorbs upstream's churn: a reshuffle of the JSON is a change to this one
module rather than a migration, and because the full document is stored in
object storage, a schema change later is a re-projection over documents already
held -- no re-capture, no browser.

Two rules govern everything here:

  * Nothing is invented. A field the document does not carry becomes NULL, not
    an empty string and not a guess.
  * Nothing is dropped. A section this module does not recognise lands in
    person_additional_section with the parser's lossless text and links, so a
    new section type upstream is a table that is missing, not content that is.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from ..store.blobs import BlobStore, sha256
from ..store.db import transaction
from ..store.ids import EXTRACTION, new_id
from . import derive
from .parse import EXPECTED_VERSION

EXTRACTOR = "linkedin_profile_json"

# Section name in the document -> table it projects into. Schema 1.4 gives
# these typed fields; the stable lookup columns remain populated and `data`
# keeps the complete parser-owned entry losslessly.
PROVISIONAL = {
    "certifications": "person_certification",
    "projects": "person_project",
    "courses": "person_course",
    "languages": "person_language",
    "honors": "person_honor",
    "organizations": "person_organization",
    "patents": "person_patent",
    "publications": "person_publication",
    "featured": "person_featured",
    "volunteering": "person_volunteering",
    "test_scores": "person_test_score",
    "services": "person_service",
    "connected_apps": "person_connected_app",
}


def _text(value) -> str | None:
    """A scalar the database can hold, or None. Never a guess.

    Upstream emits entries as dicts of varying shape, and a dict rendered by
    str() into a Text column is data loss disguised as a value.
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    text_value = str(value).strip()
    return text_value or None


def _entry_fields(entry: dict) -> dict:
    """The provisional entry shape, read out of what `extract_simple` emits.

    That is `{"name", "details", "text", "links"}` and nothing else -- checked
    against the extractor, not guessed. Note `details` is plural and is a LIST
    of the entry's remaining lines; the column is one column, so the lines are
    joined. `date_range` and `url` are read anyway because the richer
    extractors emit them and a section may graduate to one of those without
    this function needing to change.
    """
    details = entry.get("details")
    if isinstance(details, (list, tuple)):
        detail = " · ".join(str(line) for line in details if line) or None
    else:
        detail = _text(details)
    return {
        "name": _text(entry.get("name") or entry.get("title")
                      or entry.get("school")),
        "detail": detail,
        "date_range": _text(entry.get("date_range")),
        "url": _text(entry.get("url")),
        "text": _text(entry.get("text")),
        "links": json.dumps(entry.get("links")) if entry.get("links") else None,
    }


def _instants(connection: Connection, extraction_id: str,
              head_section: str) -> tuple[dict, tuple]:
    """When each section was captured, and the head document's own instant.

    Readable here because `store_extraction` inserts the extraction_input rows
    before calling us, inside the same transaction. A section with no row of
    its own was parsed inline out of the head document, so it takes the head's
    instant -- and an extraction with no capture at all, which pre-dates the
    attempt bridge, takes the moment it ran.
    """
    rows = connection.execute(text("""
        SELECT i.section, c.captured_at, c.id
        FROM extraction_input i JOIN capture c ON c.id = i.capture_id
        WHERE i.extraction_id = :e
    """), {"e": extraction_id}).all()
    by_section = {row.section: (row.captured_at, row.id) for row in rows}
    ran_at = connection.execute(text(
        "SELECT extracted_at FROM extraction WHERE id = :e"),
        {"e": extraction_id}).scalar_one()
    return by_section, by_section.get(head_section, (ran_at, None))


def project(connection: Connection, *, extraction_id: str,
            document: dict) -> int:
    """Write one observation and its children. Returns the observation id."""
    if document.get("schema_version") != EXPECTED_VERSION:
        raise ValueError(
            f"cannot project schema_version {document.get('schema_version')!r}; "
            f"expected {EXPECTED_VERSION!r}")
    profile = document.get("profile") or {}
    by_section, head = _instants(connection, extraction_id, "profile")

    observation = connection.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, headline, location,
             about, connections, followers, pronouns, current_company,
             current_school, contact_info_url, top_skills, causes,
             about_this_member, contact_info, open_to_work,
             observed_at, capture_id)
        VALUES (:e, :handle, :url, :name, :headline, :location, :about,
                :connections, :followers, :pronouns, :company, :school,
                :contact, CAST(:top_skills AS jsonb), CAST(:causes AS jsonb),
                CAST(:atm AS jsonb), CAST(:contact_info AS jsonb),
                CAST(:open_to_work AS jsonb), :observed_at, :capture_id)
        RETURNING id
    """), {
        "e": extraction_id,
        "handle": _text(profile.get("handle")) or "",
        "url": _text(profile.get("url")) or "",
        "name": _text(profile.get("name")),
        "headline": _text(profile.get("headline")),
        "location": _text(profile.get("location")),
        "about": _text(profile.get("about")),
        "connections": _text(profile.get("connections")),
        "followers": _text(profile.get("followers")),
        "pronouns": _text(profile.get("pronouns")),
        "company": _text(profile.get("current_company")),
        "school": _text(profile.get("current_school")),
        "contact": _text(profile.get("contact_info_url")),
        "top_skills": json.dumps(profile.get("top_skills"))
        if profile.get("top_skills") is not None else None,
        "causes": json.dumps(profile.get("causes"))
        if profile.get("causes") is not None else None,
        "atm": (json.dumps(profile["about_this_member"])
                if profile.get("about_this_member") else None),
        "contact_info": (json.dumps(profile["contact_info"])
                         if profile.get("contact_info") else None),
        "open_to_work": (json.dumps(profile["open_to_work"])
                         if profile.get("open_to_work") else None),
        "observed_at": head[0], "capture_id": head[1],
    }).scalar_one()

    _statuses(connection, observation, document.get("section_status") or {},
              by_section, head)
    _experience(connection, observation, profile.get("experiences") or [])
    _education(connection, observation, profile.get("education") or [])
    _skills(connection, observation, profile.get("skills") or [])
    for section, table_name in PROVISIONAL.items():
        _provisional(connection, observation, table_name,
                     profile.get(section) or [])
    _interests(connection, observation, profile.get("interests") or {})
    _recommendations(connection, observation,
                     profile.get("recommendations") or {})
    _activity(connection, observation, profile.get("activity") or {})
    _additional(connection, observation,
                profile.get("additional_sections") or {})
    return observation


def _statuses(connection: Connection, observation: int, statuses: dict,
              by_section: dict, head: tuple) -> None:
    for section, status in statuses.items():
        observed_at, capture_id = by_section.get(section, head)
        connection.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at, capture_id)
            VALUES (:o, :s, :status, :observed_at, :capture_id)
        """), {"o": observation, "s": section, "status": status,
               "observed_at": observed_at, "capture_id": capture_id})


def _experience(connection: Connection, observation: int,
                entries: list) -> None:
    """Both DOM variants, normalised into employer-with-roles.

    Upstream flattens a single-role entry so `title` sits at the entry level
    with no `roles` array, and keeps `roles` for grouped ones. A consumer
    should not have to ask which shape produced a row, so a single-role entry
    becomes an employer with exactly one role here.
    """
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        url = _text(entry.get("url"))
        employer = connection.execute(text("""
            INSERT INTO linkedin.person_employer
                (observation_id, ordinal, name, company_id, company_url,
                 employment_type, location, total_duration, text, links, data)
            VALUES (:o, :ord, :name, :cid, :url, :type, :loc, :dur, :text,
                    CAST(:links AS jsonb), CAST(:data AS jsonb))
            RETURNING id
        """), {
            "o": observation, "ord": ordinal,
            "name": _text(entry.get("name")),
            # Only when the document does not carry it. The day upstream emits
            # company_id, this branch stops firing without a change here.
            "cid": (_text(entry.get("company_id"))
                    if "company_id" in entry
                    else derive.company_id(url)),
            "url": url,
            "type": _text(entry.get("employment_type")),
            "loc": _text(entry.get("location")),
            "dur": _text(entry.get("total_duration")),
            "text": _text(entry.get("text")),
            "links": json.dumps(entry.get("links")) if entry.get("links") else None,
            "data": json.dumps(entry),
        }).scalar_one()

        roles = entry.get("roles")
        if not roles:
            # A single-role entry: the entry itself is the role.
            roles = [entry]
        for index, role in enumerate(roles):
            _role(connection, employer, index, role)


def _role(connection: Connection, employer: int, ordinal: int,
          role: dict) -> None:
    raw = _text(role.get("date_range"))
    if {"started_on", "ended_on", "is_current"} <= role.keys():
        started = _text(role["started_on"])
        ended = _text(role["ended_on"])
        current = role["is_current"]
    else:
        derived_started, derived_ended, derived_current = derive.date_range(raw)
        started = (_text(role["started_on"])
                   if "started_on" in role else derived_started)
        ended = (_text(role["ended_on"])
                 if "ended_on" in role else derived_ended)
        current = role["is_current"] if "is_current" in role else derived_current

    connection.execute(text("""
        INSERT INTO linkedin.person_role
            (employer_id, ordinal, title, employment_type, date_range,
             started_on, ended_on, is_current, duration, location,
             description, skills, url, text, links, data)
        VALUES (:e, :ord, :title, :type, :range, :start, :end, :current,
                :duration, :loc, :desc, CAST(:skills AS jsonb), :url, :text,
                CAST(:links AS jsonb), CAST(:data AS jsonb))
    """), {
        "e": employer, "ord": ordinal,
        "title": _text(role.get("title")),
        "type": _text(role.get("employment_type")),
        "range": raw, "start": started, "end": ended, "current": current,
        "duration": _text(role.get("duration")),
        "loc": _text(role.get("location")),
        "desc": _text(role.get("description")),
        "skills": json.dumps(role.get("skills")) if role.get("skills") else None,
        "url": _text(role.get("url")),
        "text": _text(role.get("text")),
        "links": json.dumps(role.get("links")) if role.get("links") else None,
        "data": json.dumps(role),
    })


def _education(connection: Connection, observation: int,
               entries: list) -> None:
    for ordinal, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        raw = _text(entry.get("date_range"))
        if {"started_on", "ended_on", "is_current"} <= entry.keys():
            started = _text(entry["started_on"])
            ended = _text(entry["ended_on"])
            current = entry["is_current"]
        else:
            derived_started, derived_ended, derived_current = derive.date_range(raw)
            started = (_text(entry["started_on"])
                       if "started_on" in entry else derived_started)
            ended = (_text(entry["ended_on"])
                     if "ended_on" in entry else derived_ended)
            current = (entry["is_current"]
                       if "is_current" in entry else derived_current)
        connection.execute(text("""
            INSERT INTO linkedin.person_education
                (observation_id, ordinal, school, school_url, degree,
                 field_of_study, date_range, started_on, ended_on, is_current, grade,
                 activities, description, skills, text, links, data)
            VALUES (:o, :ord, :school, :surl, :degree, :field, :range, :start,
                    :end, :current, :grade, :activities, :desc, CAST(:skills AS jsonb),
                    :text, CAST(:links AS jsonb), CAST(:data AS jsonb))
        """), {
            "o": observation, "ord": ordinal,
            "school": _text(entry.get("school")),
            "surl": _text(entry.get("url")),
            "degree": _text(entry.get("degree")),
            "field": _text(entry.get("field_of_study")),
            "range": raw, "start": started, "end": ended,
            "current": current,
            "grade": _text(entry.get("grade")),
            "activities": _text(entry.get("activities")),
            "desc": _text(entry.get("description")),
            "skills": json.dumps(entry.get("skills")) if entry.get("skills") else None,
            "text": _text(entry.get("text")),
            "links": json.dumps(entry.get("links")) if entry.get("links") else None,
            "data": json.dumps(entry),
        })


def _provisional(connection: Connection, observation: int, table_name: str,
                 entries: list) -> None:
    for ordinal, entry in enumerate(entries):
        # Upstream emits some of these as bare strings and some as objects.
        # Both are stored; neither is coerced into the other's shape.
        fields = (_entry_fields(entry) if isinstance(entry, dict)
                  else {"name": _text(entry), "detail": None,
                        "date_range": None, "url": None, "text": _text(entry),
                        "links": None})
        connection.execute(text(f"""
            INSERT INTO linkedin.{table_name}
                (observation_id, ordinal, name, detail, date_range, url, text,
                 links, data)
            VALUES (:o, :ord, :name, :detail, :range, :url, :text,
                    CAST(:links AS jsonb), CAST(:data AS jsonb))
        """), {"o": observation, "ord": ordinal, "range": fields["date_range"],
               "data": json.dumps(entry),
               **{k: v for k, v in fields.items() if k != "date_range"}})


def _interests(connection: Connection, observation: int, grouped: dict) -> None:
    """Flatten category buckets while preserving each typed interest entry."""
    if not isinstance(grouped, dict):
        return
    category_envelopes = grouped.get("categories")
    if isinstance(category_envelopes, dict):
        categories = grouped.get("captured_categories") or list(category_envelopes)
    else:
        category_envelopes = None
        categories = grouped.get("captured") or [
            key for key, value in grouped.items()
            if key not in {"source_url", "captured"} and isinstance(value, list)
        ]
    ordinal = 0
    for category in categories:
        if isinstance(category_envelopes, dict):
            envelope = category_envelopes.get(category) or {}
            entries = envelope.get("entries") or []
            source_url = _text(envelope.get("source_url"))
        else:
            entries = grouped.get(category) or []
            source_url = _text(grouped.get("source_url"))
        for entry in entries:
            entry = entry if isinstance(entry, dict) else {"name": entry}
            fields = _entry_fields(entry)
            connection.execute(text("""
                INSERT INTO linkedin.person_interest
                    (observation_id, ordinal, category, source_url, name, detail,
                     date_range, url, text, links, data)
                VALUES (:o, :ord, :category, :source_url, :name, :detail, :range, :url,
                        :text, CAST(:links AS jsonb), CAST(:data AS jsonb))
            """), {
                "o": observation, "ord": ordinal, "category": category,
                "source_url": source_url, "range": fields["date_range"],
                "data": json.dumps(entry),
                **{k: v for k, v in fields.items() if k != "date_range"},
            })
            ordinal += 1


def _skills(connection: Connection, observation: int, entries: list) -> None:
    """Keep parser 0.2's per-detail text/URL objects as structured evidence."""
    for ordinal, entry in enumerate(entries):
        entry = entry if isinstance(entry, dict) else {"name": entry}
        details = entry.get("details")
        legacy_details = None
        if isinstance(details, list) and all(
                not isinstance(item, dict) for item in details):
            legacy_details = " · ".join(str(item) for item in details if item) or None
        connection.execute(text("""
            INSERT INTO linkedin.person_skill
                (observation_id, ordinal, name, detail, date_range, url, text,
                 links, details, data)
            VALUES (:o, :ord, :name, :detail, :range, :url, :text,
                    CAST(:links AS jsonb), CAST(:details AS jsonb),
                    CAST(:data AS jsonb))
        """), {
            "o": observation, "ord": ordinal,
            "name": _text(entry.get("name")), "detail": legacy_details,
            "range": _text(entry.get("date_range")),
            "url": _text(entry.get("url")), "text": _text(entry.get("text")),
            "links": json.dumps(entry.get("links")) if entry.get("links") else None,
            "details": (json.dumps(details)
                        if isinstance(details, list) and details else None),
            "data": json.dumps(entry),
        })


def _recommendations(connection: Connection, observation: int,
                     grouped: dict) -> None:
    for direction in ("received", "given"):
        for ordinal, entry in enumerate(grouped.get(direction) or []):
            entry = entry if isinstance(entry, dict) else {"text": entry}
            person = (entry.get("from") if direction == "received"
                      else entry.get("to")) or entry.get("author") or {}
            details = entry.get("details") if isinstance(entry, dict) else None
            if isinstance(details, (list, tuple)):
                detail = " · ".join(str(line) for line in details if line) or None
            else:
                detail = _text(details)
            connection.execute(text("""
                INSERT INTO linkedin.person_recommendation
                    (observation_id, direction, ordinal, person_name,
                     person_headline, person_company, connection_degree,
                     profile_url, recommended_at, relationship, text, links,
                     data)
                VALUES (:o, :d, :ord, :name, :headline, :company, :degree,
                        :url, :date, :relationship, :text,
                        CAST(:links AS jsonb), CAST(:data AS jsonb))
            """), {
                "o": observation, "d": direction, "ord": ordinal,
                "name": _text(entry.get("name")) or _text(person.get("name")),
                "headline": detail or _text(
                    person.get("headline") or person.get("title")),
                "company": _text(person.get("company")),
                "degree": _text(person.get("connection_degree")),
                "url": _text(person.get("profile_url") or person.get("url")),
                "date": _text(entry.get("date")),
                "relationship": _text(entry.get("relationship")),
                "text": _text(entry.get("text")),
                "links": (json.dumps(entry.get("links"))
                          if entry.get("links") else None),
                "data": json.dumps(entry),
            })


def _activity(connection: Connection, observation: int, grouped: dict) -> None:
    for plural, kind in (("posts", "post"), ("comments", "comment"),
                         ("articles", "article"), ("reactions", "reaction")):
        envelope = grouped.get(plural) or {}
        if isinstance(envelope, list):
            envelope = {"items": envelope, "captured": len(envelope)}
        connection.execute(text("""
            INSERT INTO linkedin.person_activity_summary
                (observation_id, kind, source_url, loaded, captured, complete)
            VALUES (:o, :kind, :url, :loaded, :captured, :complete)
        """), {
            "o": observation, "kind": kind,
            "url": _text(envelope.get("source_url")),
            "loaded": envelope.get("loaded"),
            "captured": envelope.get("captured", len(envelope.get("items") or [])),
            "complete": envelope.get("complete"),
        })
        for ordinal, entry in enumerate(envelope.get("items") or []):
            entry = entry if isinstance(entry, dict) else {"text": entry}
            post = entry.get("post") or {}
            author = entry.get("author") or post.get("author") or {}
            content = entry.get("content") or post.get("commentary") or {}
            posted = post.get("posted") or {}
            connection.execute(text("""
                INSERT INTO linkedin.person_activity
                    (observation_id, kind, ordinal, author_name, author_title,
                     author_company, author_connection_level, author_url,
                     author_badges, posted_at, edited, visibility, url, text,
                     media, engagement, links, data)
                VALUES (:o, :k, :ord, :name, :title, :company, :level, :author_url,
                        CAST(:badges AS jsonb), :at, :edited, :visibility, :url,
                        :text, CAST(:media AS jsonb), CAST(:engagement AS jsonb),
                        CAST(:links AS jsonb), CAST(:data AS jsonb))
            """), {
                "o": observation, "k": kind, "ord": ordinal,
                "name": _text(author.get("name")),
                "title": _text(author.get("title")),
                "company": _text(author.get("company")),
                "level": _text(author.get("connection_level")),
                "author_url": _text(author.get("url")),
                "badges": json.dumps(author.get("badges")) if author.get("badges") else None,
                "at": _text(entry.get("activity_at") or entry.get("timestamp")
                            or entry.get("posted_at") or posted.get("relative")),
                "edited": (entry.get("edited") if "edited" in entry
                           else posted.get("edited")),
                "visibility": _text(entry.get("visibility")
                                    or posted.get("visibility")),
                "url": _text(entry.get("permalink") or entry.get("url")),
                "text": (_text(content.get("text"))
                         if isinstance(content, dict) else None) or _text(entry.get("text")),
                "media": json.dumps(post.get("media") or content.get("media"))
                if (post.get("media") or content.get("media")) else None,
                "engagement": json.dumps(entry.get("engagement")
                                         or post.get("engagement"))
                if (entry.get("engagement") or post.get("engagement")) else None,
                "links": json.dumps(entry.get("links")) if entry.get("links") else None,
                "data": json.dumps(entry),
            })


def _additional(connection: Connection, observation: int,
                sections: dict) -> None:
    """Sections upstream did not recognise, kept rather than dropped.

    `interests` is the live example: sections.py captures it and no extractor
    reads it, so it arrives here every time. A section in this table is one
    that needs a projection written, and it says so by being here.
    """
    for ordinal, (name, snapshot) in enumerate(sections.items()):
        snapshot = snapshot if isinstance(snapshot, dict) else {"text": snapshot}
        connection.execute(text("""
            INSERT INTO linkedin.person_additional_section
                (observation_id, ordinal, section, name, detail, date_range,
                 url, text, links, data)
            VALUES (:o, :ord, :section, NULL, NULL, NULL, NULL, :text,
                    CAST(:links AS jsonb), CAST(:data AS jsonb))
        """), {
            "o": observation, "ord": ordinal, "section": name,
            "text": _text(snapshot.get("text")),
            "links": json.dumps(snapshot.get("links")) if snapshot.get("links") else None,
            "data": json.dumps(snapshot),
        })


def store_extraction(url: str | None, *, request_id: str,
                     capture_ids: dict[str, str], document: dict,
                     store: BlobStore, attempt_id: int | None = None) -> str:
    """Store the document, write the extraction row, and project it.

    The document goes to object storage first and is verified there, for the
    same reason capture artifacts are: a row naming a missing object is
    corruption, and this row is the only pointer to the reading it describes.
    """
    from ..capture.writer import store_verified

    extraction_id = new_id(EXTRACTION)
    payload = json.dumps(document, ensure_ascii=False,
                         indent=2, sort_keys=True).encode("utf-8")
    key = f"extractions/{extraction_id}/profile.json"
    store_verified(store, key, payload, "application/json")

    version = str(document.get("schema_version") or "unknown")
    with transaction(url) as connection:
        connection.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version, extracted_at,
                 outcome, document_key, document_sha256, document_bytes)
            VALUES (:id, :r, :attempt, :extractor, :version, :t, :outcome, :key,
                    :digest, :size)
        """), {
            "id": extraction_id, "r": request_id, "attempt": attempt_id,
            "extractor": EXTRACTOR,
            "version": version, "t": datetime.now(UTC),
            "outcome": _outcome(document), "key": key,
            "digest": sha256(payload), "size": len(payload),
        })
        for section, capture_id in capture_ids.items():
            connection.execute(text("""
                INSERT INTO extraction_input (extraction_id, capture_id, section)
                VALUES (:e, :c, :section)
            """), {"e": extraction_id, "c": capture_id, "section": section})
        project(connection, extraction_id=extraction_id, document=document)
    return extraction_id


def _outcome(document: dict) -> str:
    """`ok`, `partial`, or `suspect` for parser drift.

    A document whose Patents page errored is not a failed extraction -- it is a
    successful one that says so, and section_status carries the detail.
    """
    if document.get("drift"):
        return "suspect"
    statuses = set((document.get("section_status") or {}).values())
    if "drift" in statuses:
        return "suspect"
    return "partial" if statuses & {
        "fallback", "error", "not_captured", "parse_failed",
    } else "ok"
