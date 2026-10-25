"""The company parser document projected into stable and lossless rows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.capture.writer import Document
from career_lens.linkedin.company_project import project_company
from career_lens.store.ids import EXTRACTION, REQUEST, new_id

NOW = datetime.now(UTC)


def test_shared_company_extraction_preserves_native_capture_context():
    from career_lens.linkedin.execution import extract_linkedin_company

    target = "https://www.linkedin.com/company/acme/"

    def captured(section, html, url, *, stopped=None, metadata=None):
        return Document(
            section=section,
            url_requested=url,
            url_final=url,
            host="www.linkedin.com",
            http_status=200,
            captured_at=NOW,
            title=None,
            html=html,
            scroll_stopped=stopped,
            metadata=metadata or {},
        )

    provenance = {
        "arrival_method": "google_search",
        "requested_url": target,
        "search_engine": "google",
        "search_query": "acme LinkedIn",
        "search_result_rank": 2,
    }
    documents = [
        captured(
            "discovery-serp",
            b"<html>google</html>",
            "https://www.google.com/search?q=acme",
        ),
        captured(
            "company",
            b"<html>company</html>",
            target,
            metadata={"provenance": provenance},
        ),
        captured(
            "about",
            b"<html>about</html>",
            f"{target}about/",
            stopped="distance",
        ),
        captured(
            "jobs-page-1",
            b"<html>jobs</html>",
            "https://www.linkedin.com/jobs/search-results/?start=0",
            metadata={"has_more": True},
        ),
    ]
    seen = {}

    def parse(home, tabs, **kwargs):
        seen.update(home=home, tabs=tabs, **kwargs)
        return {"schema_version": "1.1", "company": {}, "section_status": {}}

    def persist(database_url, **kwargs):
        seen["database_url"] = database_url
        seen["stored"] = kwargs

    store = object()
    extract_linkedin_company(
        "rq-test",
        target,
        documents,
        {
            "discovery-serp": "cap-serp",
            "company": "cap-company",
            "about": "cap-about",
            "jobs-page-1": "cap-jobs",
        },
        store,
        "postgresql://test",
        attempt_id=42,
        requested_sections=frozenset({"about", "jobs"}),
        parser=parse,
        persist=persist,
    )

    assert seen["home"] == "<html>company</html>"
    assert seen["tabs"] == {
        "about": "<html>about</html>",
        "jobs-page-1": "<html>jobs</html>",
    }
    assert seen["company_url"] == target
    assert seen["tab_urls"] == {
        "about": f"{target}about/",
        "jobs-page-1": "https://www.linkedin.com/jobs/search-results/?start=0",
    }
    assert seen["tab_capture"] == {
        "about": {"scroll_stopped": "distance"},
        "jobs-page-1": {"scroll_stopped": None, "has_more": True},
    }
    assert seen["provenance"] == provenance
    assert seen["requested_sections"] == frozenset({"about", "jobs"})
    assert seen["database_url"] == "postgresql://test"
    assert seen["stored"] == {
        "request_id": "rq-test",
        "capture_ids": {
            "company": "cap-company",
            "about": "cap-about",
            "jobs-page-1": "cap-jobs",
        },
        "document": {
            "schema_version": "1.1",
            "company": {},
            "section_status": {},
        },
        "store": store,
        "attempt_id": 42,
    }


@pytest.fixture
def extraction_id(app) -> str:
    request_id, extraction_id = new_id(REQUEST), new_id(EXTRACTION)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:request, :url, :url, 'linkedin_company', 'running', :now)
    """), {"request": request_id,
            "url": "https://www.linkedin.com/company/acme/", "now": NOW})
    app.execute(text("""
        INSERT INTO extraction
            (id, request_id, extractor, extractor_version, extracted_at,
             outcome, document_key, document_sha256, document_bytes)
        VALUES (:extraction, :request, 'linkedin_company_json', '1.0', :now,
                'ok', 'k', 'digest', 1)
    """), {"extraction": extraction_id, "request": request_id, "now": NOW})
    return extraction_id


def _document() -> dict:
    post = {
        "id": "urn:li:activity:123", "permalink": "https://linkedin/post/123",
        "activity_at": "2026-08-13T10:00:00Z", "relation": "posted",
        "post": {
            "author": {"name": "Acme", "followers": 12345},
            "posted": {"relative": "2d"},
            "commentary": {"text": "A release"},
            "media": [{"type": "image", "url": "https://img/1"}],
            "engagement": {
                "reactions": {"count": 8, "types": ["like"]},
                "comments": 2,
                "reposts": 1,
            },
        },
    }
    company = {
        "url": "https://www.linkedin.com/company/acme/", "slug": "acme",
        "name": "Acme", "logo_url": "https://img/logo",
        "cover_image_url": "https://img/cover", "verified": True,
        "tagline": "Dependable systems", "industry": "Software",
        "product_categories": [{"name": "Automation", "url": "https://p"}],
        "location": "London", "followers": "2M",
        "employee_count": "5K-10K",
        "external_action": {"label": "Visit website", "url": "https://acme"},
        "affiliated_pages": [{"name": "Acme Labs", "url": "https://labs"}],
        "home": {"featured": [], "newsletters": [{"name": "Acme News"}]},
        "events": {
            "upcoming": [{
                "title": "Acme launch", "starts_at": "2026-09-01",
                "url": "https://linkedin/events/acme-launch",
                "image_url": "https://img/event", "attendee_count": 132,
            }],
            "past": [],
        },
        "about": {
            "overview": "Acme overview", "website": "https://acme",
            "industry": "Software", "company_size": "5,001-10,000",
            "headquarters": "London", "founded": "2010", "specialties": "AI",
            "associated_members": {"count": 17103, "url": "https://members"},
            "commitments": [{"name": "Sustainability"}],
            "locations": [{
                "name": "London", "description": "HQ", "headquarters": True,
                "address": {"line1": "1 Main St", "city": "London",
                            "country": "GB"},
                "latitude": 51.5, "longitude": -0.1,
            }],
        },
        "services": {
            "overview": "Dependable automation services",
            "request_action": {
                "label": "Request services", "url": "https://linkedin/request",
            },
            "availability": "Remote", "pricing": "Contact for pricing",
            "services": [{"name": "Automation", "url": "https://linkedin/s/1"}],
        },
        "posts": {"source_url": "https://linkedin/acme/posts",
                  "reached_end": False, "items": [post]},
        "products": {"source_url": "https://linkedin/acme/product",
                     "name": "Acme Flow", "media": []},
        "jobs": {
            "source_url": "https://linkedin/acme/jobs", "captured_pages": 1,
            "reached_end": False,
            "items": [{"id": "42", "title": "Engineer", "company_name": "Acme",
                       "location": "London", "workplace_type": "Hybrid",
                       "posted_at": "2 days ago", "posting_status": "reposted",
                       "url": "https://linkedin/jobs/42", "page": 1,
                       "position": 2, "insights": ["Early applicant"]}],
        },
        "life": {"source_url": "https://linkedin/acme/life",
                 "title": "Life at Acme", "spotlights": []},
        "people": {
            "source_url": "https://linkedin/acme/people",
            "analytics": {"reached_end": True, "cards": [{
                "title": "Where they live",
                "items": [{"label": "London", "count": 700}],
            }]},
            "people_you_may_know": [{"name": "Jane"}],
        },
    }
    return {
        "schema_version": "1.1", "company": company,
        "sources": {section: {"url": company[section].get("source_url")}
                    for section in ("home", "events", "about", "services",
                                    "posts", "products", "jobs", "life", "people")},
        "section_status": {section: "parsed" for section in
                           ("home", "events", "about", "services", "posts",
                            "products", "jobs", "life", "people")},
        "warnings": [],
        "provenance": {"arrival_method": "direct"},
    }


def test_company_projection_keeps_typed_rows_and_lossless_sections(
        app, extraction_id):
    document = _document()
    observation = project_company(
        app, extraction_id=extraction_id, document=document)

    top = app.execute(text("""
        SELECT slug, followers, employee_count, associated_members_count,
               product_categories, external_action, affiliated_pages
        FROM linkedin.company_observation WHERE id = :observation
    """), {"observation": observation}).one()
    assert tuple(top) == (
        "acme", "2M", "5K-10K", 17103,
        [{"name": "Automation", "url": "https://p"}],
        {"label": "Visit website", "url": "https://acme"},
        [{"name": "Acme Labs", "url": "https://labs"}],
    )
    statuses = app.execute(text("""
        SELECT section, reached_end, data
        FROM linkedin.company_section_status
        WHERE observation_id = :observation ORDER BY section
    """), {"observation": observation}).all()
    assert len(statuses) == 9
    by_section = {row.section: row for row in statuses}
    assert by_section["events"].data == document["company"]["events"]
    assert by_section["services"].data == document["company"]["services"]
    assert by_section["posts"].reached_end is False
    assert by_section["products"].data == document["company"]["products"]
    assert by_section["life"].data == document["company"]["life"]
    assert by_section["people"].reached_end is True

    office = app.execute(text("""
        SELECT ordinal, city, headquarters, data
        FROM linkedin.company_office WHERE observation_id = :observation
    """), {"observation": observation}).one()
    assert (office.ordinal, office.city, office.headquarters) == (0, "London", True)
    assert office.data == document["company"]["about"]["locations"][0]

    insight = app.execute(text("""
        SELECT title, label, member_count
        FROM linkedin.company_insight WHERE observation_id = :observation
    """), {"observation": observation}).one()
    assert tuple(insight) == ("Where they live", "London", 700)

    job = app.execute(text("""
        SELECT job_id, company_name, workplace_type, posting_status, insights, data
        FROM linkedin.company_job WHERE observation_id = :observation
    """), {"observation": observation}).one()
    assert tuple(job)[:5] == (
        "42", "Acme", "Hybrid", "reposted", ["Early applicant"])
    assert job.data == document["company"]["jobs"]["items"][0]

    post = app.execute(text("""
        SELECT activity_id, author_name, author_followers, engagement, data
        FROM linkedin.company_post WHERE observation_id = :observation
    """), {"observation": observation}).one()
    assert tuple(post)[:4] == (
        "urn:li:activity:123", "Acme", 12345,
        {"reactions": {"count": 8, "types": ["like"]},
         "comments": 2, "reposts": 1})
    assert post.data == document["company"]["posts"]["items"][0]


def test_company_projection_records_an_unrequested_section(
        app, extraction_id):
    document = _document()
    document["section_status"]["home"] = "not_requested"
    del document["company"]["home"]

    observation = project_company(
        app, extraction_id=extraction_id, document=document)

    status = app.execute(text("""
        SELECT status, data
        FROM linkedin.company_section_status
        WHERE observation_id = :observation AND section = 'home'
    """), {"observation": observation}).one()
    assert (status.status, status.data) == ("not_requested", None)


def test_company_projection_rejects_an_unknown_document_version(
        app, extraction_id):
    document = _document()
    document["schema_version"] = "9.9"
    with pytest.raises(ValueError, match="expected '1.1'"):
        project_company(app, extraction_id=extraction_id, document=document)
