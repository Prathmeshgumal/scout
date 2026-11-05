"""The queryable copy of a generic page document, and what it enables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.capture.writer import Document
from career_lens.generic.contract import build
from career_lens.generic.project import ProjectionConflict, project
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id


NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)
URL = "https://example.com/article"


def _evidence(app, *, at=NOW, url=URL, title="Example article"):
    """One generic request with a stored page capture and an extraction."""
    request_id, extraction_id = new_id(REQUEST), new_id(EXTRACTION)
    capture_id = new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'generic', 'succeeded', :t)
    """), {"r": request_id, "u": url, "t": at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'generic_page_json', '1.0', :t, 'ok', 'k', 'd', 1)
    """), {"e": extraction_id, "r": request_id, "t": at})
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, url_final,
                             host, http_status, captured_at, title)
        VALUES (:c, :r, 'page', :u, :u, 'example.com', 200, :t, :title)
    """), {"c": capture_id, "r": request_id, "u": url, "t": at,
           "title": title})
    document = build(Document(
        section="page", url_requested=url, url_final=url, host="example.com",
        http_status=200, captured_at=at, title=title, html=b"",
        text="Rendered text", scroll_stopped="bottom", scroll_growing=False,
    ), capture_id=capture_id, normalised_url=url)
    return request_id, extraction_id, capture_id, document


def test_a_document_is_projected_under_the_instant_it_was_read(app):
    """Not the instant it was projected: a temporal question asks what the
    page looked like at a moment, and the answer is only as good as the
    moment it is filed under."""
    read_at = NOW - timedelta(days=30)
    request_id, extraction_id, capture_id, document = _evidence(
        app, at=read_at)

    observation_id = project(
        app, extraction_id=extraction_id, request_id=request_id,
        document=document)

    row = app.execute(text("""
        SELECT * FROM generic.page_observation WHERE id=:id
    """), {"id": observation_id}).one()
    assert row.url_normalised == URL
    assert row.host == "example.com"
    assert row.capture_id == capture_id
    assert row.status == "succeeded"
    assert row.observed_at == read_at
    assert row.text_characters == len("Rendered text")


def test_projecting_the_same_document_twice_is_one_row(app):
    request_id, extraction_id, _, document = _evidence(app)

    first = project(app, extraction_id=extraction_id,
                    request_id=request_id, document=document)
    second = project(app, extraction_id=extraction_id,
                     request_id=request_id, document=document)

    assert first == second
    assert app.execute(text("""
        SELECT count(*) FROM generic.page_observation
    """)).scalar_one() == 1


def test_one_extraction_cannot_come_to_mean_a_different_reading(app):
    """Append-only evidence cannot be corrected by rewriting it, so a second
    document under the same extraction is refused rather than reconciled."""
    request_id, extraction_id, _, document = _evidence(app)
    project(app, extraction_id=extraction_id, request_id=request_id,
            document=document)

    other = {**document, "capture": {**document["capture"],
                                     "text": "Something else entirely",
                                     "text_characters": 23}}

    with pytest.raises(ProjectionConflict):
        project(app, extraction_id=extraction_id, request_id=request_id,
                document=other)


def test_a_reading_that_never_happened_is_not_projected(app):
    """A failed document has no instant to be filed under, and a row filed
    under the wrong instant would answer a temporal question with a page
    nobody ever read."""
    request_id, extraction_id, _, document = _evidence(app)
    document["capture"].update(
        status="failed", capture_id=None, captured_at=None, url_final=None,
        text=None, text_characters=0,
        error={"kind": "timeout", "detail": "page did not settle"})

    with pytest.raises(ProjectionConflict):
        project(app, extraction_id=extraction_id, request_id=request_id,
                document=document)


def test_the_projection_is_append_only(app):
    request_id, extraction_id, _, document = _evidence(app)
    project(app, extraction_id=extraction_id, request_id=request_id,
            document=document)

    with pytest.raises(Exception):
        app.execute(text("""
            UPDATE generic.page_observation SET host='other.example'
        """))


def test_a_generic_scrape_can_be_satisfied_by_a_recent_reading(app):
    """What the projection is for on the write side. Without it `page` never
    resolved, so every repeat request re-scraped a page we already held."""
    from career_lens.observe import freshness, resolve

    request_id, extraction_id, _, document = _evidence(
        app, at=NOW - timedelta(hours=2))
    project(app, extraction_id=extraction_id, request_id=request_id,
            document=document)

    recent = freshness.satisfies(
        app, kind=resolve.GENERIC, key=URL,
        freshness_seconds=freshness.parse("24h"), sections=["page"], now=NOW)
    ancient = freshness.satisfies(
        app, kind=resolve.GENERIC, key=URL,
        freshness_seconds=freshness.parse("90m"), sections=["page"], now=NOW)

    assert recent.satisfied is True
    assert recent.oldest_observed_at == NOW - timedelta(hours=2)
    assert ancient.satisfied is False
    assert ancient.stale == ["page"]


def test_an_unread_url_is_never_fresh(app):
    from career_lens.observe import freshness, resolve

    assert freshness.satisfies(
        app, kind=resolve.GENERIC, key=URL,
        freshness_seconds=freshness.parse("24h"), sections=["page"],
        now=NOW).satisfied is False
