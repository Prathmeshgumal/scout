"""Project a LinkedIn company document into queryable rows.

The complete parser document remains the source of truth in object storage.
Stable entities get typed columns here, and every tab payload is retained in
company_section_status.data so projection never discards a parser-owned field.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from sqlalchemy import Connection, text

from ..store.blobs import BlobStore, sha256
from ..store.db import transaction
from ..store.ids import EXTRACTION, new_id
from .parse import COMPANY_EXPECTED_VERSION

EXTRACTOR = "linkedin_company_json"
EXACT_COUNT = re.compile(r"^[\d,]+$")
SECTIONS = (
    "home", "events", "about", "services", "posts", "products", "jobs",
    "life", "people",
)


def _text(value: object) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    normalized = str(value).strip()
    return normalized or None


def _exact_count(value: object) -> int | None:
    normalized = _text(value)
    if normalized is None or not EXACT_COUNT.fullmatch(normalized):
        return None
    return int(normalized.replace(",", ""))


def _json(value: object) -> str | None:
    return json.dumps(value) if value is not None else None


def _instants(connection: Connection, extraction_id: str,
              head_section: str) -> tuple[dict, tuple]:
    """When each tab was captured, and the head document's own instant.

    Readable here because `store_company_extraction` inserts the
    extraction_input rows before calling us, inside the same transaction. A tab
    with no row of its own was never fetched separately and takes the head's
    instant; an extraction with no capture at all takes the moment it ran.
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


def project_company(connection: Connection, *, extraction_id: str,
                    document: dict) -> int:
    """Write one company observation and its children."""
    if document.get("schema_version") != COMPANY_EXPECTED_VERSION:
        raise ValueError(
            f"cannot project company schema_version "
            f"{document.get('schema_version')!r}; expected "
            f"{COMPANY_EXPECTED_VERSION!r}")

    company = document.get("company") or {}
    about = company.get("about") or {}
    associated = about.get("associated_members") or {}
    by_section, head = _instants(connection, extraction_id, "company")
    observation = connection.execute(text("""
        INSERT INTO linkedin.company_observation
            (extraction_id, slug, company_url, name, logo_url,
             cover_image_url, verified, tagline, description, industry,
             location, followers, employee_count, company_size,
             associated_members_count, founded, website, follower_count,
             product_categories, external_action, affiliated_pages,
             observed_at, capture_id)
        VALUES
            (:e, :slug, :url, :name, :logo, :cover, :verified, :tagline,
             :description, :industry, :location, :followers, :employees,
             :size, :members, :founded, :website, :follower_count,
             CAST(:categories AS jsonb), CAST(:action AS jsonb),
             CAST(:affiliated AS jsonb), :observed_at, :capture_id)
        RETURNING id
    """), {
        "e": extraction_id,
        "slug": _text(company.get("slug")) or "",
        "url": _text(company.get("url")),
        "name": _text(company.get("name")),
        "logo": _text(company.get("logo_url")),
        "cover": _text(company.get("cover_image_url")),
        "verified": bool(company.get("verified")),
        "tagline": _text(company.get("tagline")),
        "description": _text(about.get("overview")),
        "industry": _text(company.get("industry") or about.get("industry")),
        "location": _text(company.get("location")),
        "followers": _text(company.get("followers")),
        "employees": _text(company.get("employee_count")),
        "size": _text(about.get("company_size")),
        "members": associated.get("count"),
        "founded": _text(about.get("founded")),
        "website": _text(about.get("website")),
        "follower_count": _exact_count(company.get("followers")),
        "categories": _json(company.get("product_categories")),
        "action": _json(company.get("external_action")),
        "affiliated": _json(company.get("affiliated_pages")),
        "observed_at": head[0], "capture_id": head[1],
    }).scalar_one()

    _section_rows(connection, observation, document, company, by_section, head)
    _offices(connection, observation, about.get("locations") or [])
    _insights(connection, observation, company.get("people") or {})
    _jobs(connection, observation, company.get("jobs") or {})
    _posts(connection, observation, company.get("posts") or {})
    return observation


def _section_rows(connection: Connection, observation: int,
                  document: dict, company: dict, by_section: dict,
                  head: tuple) -> None:
    statuses = document.get("section_status") or {}
    sources = document.get("sources") or {}
    for section in SECTIONS:
        data = company.get(section)
        source = sources.get(section) or {}
        reached_end = data.get("reached_end") if isinstance(data, dict) else None
        if section == "people" and isinstance(data, dict):
            analytics = data.get("analytics") or {}
            reached_end = analytics.get("reached_end")
        observed_at, capture_id = by_section.get(section, head)
        connection.execute(text("""
            INSERT INTO linkedin.company_section_status
                (observation_id, section, status, source_url, reached_end,
                 data, observed_at, capture_id)
            VALUES (:o, :section, :status, :url, :end, CAST(:data AS jsonb),
                    :observed_at, :capture_id)
        """), {
            "o": observation,
            "section": section,
            "observed_at": observed_at, "capture_id": capture_id,
            "status": statuses.get(section, "not_captured"),
            "url": _text(source.get("url")),
            "end": reached_end if isinstance(reached_end, bool) else None,
            "data": _json(data),
        })


def _offices(connection: Connection, observation: int,
             locations: list) -> None:
    for ordinal, location in enumerate(locations):
        if not isinstance(location, dict):
            continue
        address = location.get("address") or {}
        connection.execute(text("""
            INSERT INTO linkedin.company_office
                (observation_id, ordinal, name, description, headquarters,
                 line1, line2, city, geographic_area, postal_code, country,
                 latitude, longitude, data)
            VALUES (:o, :ordinal, :name, :description, :headquarters,
                    :line1, :line2, :city, :area, :postal, :country,
                    :latitude, :longitude, CAST(:data AS jsonb))
        """), {
            "o": observation, "ordinal": ordinal,
            "name": _text(location.get("name")),
            "description": _text(location.get("description")),
            "headquarters": bool(location.get("headquarters")),
            "line1": _text(address.get("line1")),
            "line2": _text(address.get("line2")),
            "city": _text(address.get("city")),
            "area": _text(address.get("geographic_area")),
            "postal": _text(address.get("postal_code")),
            "country": _text(address.get("country")),
            "latitude": _text(location.get("latitude")),
            "longitude": _text(location.get("longitude")),
            "data": _json(location),
        })


def _insights(connection: Connection, observation: int, people: dict) -> None:
    analytics = people.get("analytics") or {}
    for card_ordinal, card in enumerate(analytics.get("cards") or []):
        if not isinstance(card, dict):
            continue
        title = _text(card.get("title"))
        if title is None:
            continue
        for item_ordinal, item in enumerate(card.get("items") or []):
            if not isinstance(item, dict):
                continue
            connection.execute(text("""
                INSERT INTO linkedin.company_insight
                    (observation_id, card_ordinal, item_ordinal, title, label,
                     member_count, data)
                VALUES (:o, :card, :item, :title, :label, :count,
                        CAST(:data AS jsonb))
            """), {
                "o": observation, "card": card_ordinal,
                "item": item_ordinal, "title": title,
                "label": _text(item.get("label")),
                "count": item.get("count"), "data": _json(item),
            })


def _jobs(connection: Connection, observation: int, jobs: dict) -> None:
    for ordinal, item in enumerate(jobs.get("items") or []):
        if not isinstance(item, dict):
            continue
        connection.execute(text("""
            INSERT INTO linkedin.company_job
                (observation_id, ordinal, job_id, title, company_name,
                 location, workplace_type, posted_at, posting_status, url,
                 page, position, insights, data)
            VALUES (:o, :ordinal, :job_id, :title, :company, :location,
                    :workplace, :posted, :status, :url, :page, :position,
                    CAST(:insights AS jsonb), CAST(:data AS jsonb))
        """), {
            "o": observation, "ordinal": ordinal,
            "job_id": _text(item.get("id")), "title": _text(item.get("title")),
            "company": _text(item.get("company_name")),
            "location": _text(item.get("location")),
            "workplace": _text(item.get("workplace_type")),
            "posted": _text(item.get("posted_at")),
            "status": _text(item.get("posting_status")),
            "url": _text(item.get("url")), "page": item.get("page"),
            "position": item.get("position"),
            "insights": _json(item.get("insights")), "data": _json(item),
        })


def _posts(connection: Connection, observation: int, posts: dict) -> None:
    for ordinal, item in enumerate(posts.get("items") or []):
        if not isinstance(item, dict):
            continue
        post = item.get("post") or {}
        author = post.get("author") or {}
        commentary = post.get("commentary") or {}
        posted = post.get("posted") or {}
        connection.execute(text("""
            INSERT INTO linkedin.company_post
                (observation_id, ordinal, activity_id, relation, author_name,
                 author_followers, text, posted_at, url, media, engagement, data)
            VALUES (:o, :ordinal, :activity_id, :relation, :author,
                    :followers, :text, :posted, :url, CAST(:media AS jsonb),
                    CAST(:engagement AS jsonb), CAST(:data AS jsonb))
        """), {
            "o": observation, "ordinal": ordinal,
            "activity_id": _text(item.get("id")),
            "relation": _text(item.get("relation")),
            "author": _text(author.get("name")),
            "followers": author.get("followers"),
            "text": _text(commentary.get("text")),
            "posted": _text(item.get("activity_at") or posted.get("relative")),
            "url": _text(item.get("permalink")),
            "media": _json(post.get("media")),
            "engagement": _json(post.get("engagement")),
            "data": _json(item),
        })


def store_company_extraction(url: str | None, *, request_id: str,
                             capture_ids: dict[str, str], document: dict,
                             store: BlobStore,
                             attempt_id: int | None = None) -> str:
    """Store, register, and project one company parser document."""
    from ..capture.writer import store_verified

    extraction_id = new_id(EXTRACTION)
    payload = json.dumps(document, ensure_ascii=False,
                         indent=2, sort_keys=True).encode("utf-8")
    key = f"extractions/{extraction_id}/company.json"
    store_verified(store, key, payload, "application/json")

    version = str(document.get("schema_version") or "unknown")
    with transaction(url) as connection:
        connection.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version,
                 extracted_at, outcome, document_key, document_sha256,
                 document_bytes)
            VALUES (:id, :request, :attempt, :extractor, :version,
                    :extracted, :outcome, :key, :digest, :size)
        """), {
            "id": extraction_id, "request": request_id,
            "attempt": attempt_id,
            "extractor": EXTRACTOR, "version": version,
            "extracted": datetime.now(UTC), "outcome": _outcome(document),
            "key": key, "digest": sha256(payload), "size": len(payload),
        })
        for section, capture_id in capture_ids.items():
            connection.execute(text("""
                INSERT INTO extraction_input (extraction_id, capture_id, section)
                VALUES (:extraction, :capture, :section)
            """), {
                "extraction": extraction_id, "capture": capture_id,
                "section": section,
            })
        project_company(connection, extraction_id=extraction_id,
                        document=document)
    return extraction_id


def _outcome(document: dict) -> str:
    statuses = set((document.get("section_status") or {}).values())
    return (
        "partial"
        if statuses & {"fallback", "error", "not_captured", "parse_failed"}
        else "ok"
    )
