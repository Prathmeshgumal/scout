"""How stale an answer a caller will accept, and whether we have one."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.observe import freshness, resolve
from auth_support import operator_client
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id

URL = "https://www.linkedin.com/in/jane/"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("value,expected", [
    ("90m", 90 * 60),
    ("24h", 24 * 3600),
    ("7d", 7 * 86400),
    ("1m", 60),
    (None, None),
])
def test_a_duration_reads_as_seconds(value, expected):
    assert freshness.parse(value) == expected


@pytest.mark.parametrize("value", [
    "", "24", "h", "0h", "-1h", "24 h", "1w", "1.5h", "24H", 24, "999d",
])
def test_a_duration_that_is_not_one_is_refused(value):
    with pytest.raises(freshness.InvalidFreshness):
        freshness.parse(value)


def test_the_refusal_says_what_is_accepted():
    """An operator who typed 1w should not have to guess the grammar."""
    with pytest.raises(freshness.InvalidFreshness) as raised:
        freshness.parse("1w")
    assert "m" in str(raised.value) and "d" in str(raised.value)


def _observation(app, *, at, sections, url=URL):
    """One person observation read at `at`, with `sections` as {name: status}."""
    rid, eid, cid = new_id(REQUEST), new_id(EXTRACTION), new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.7', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": at})
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at)
        VALUES (:c, :r, 'profile', :u, 'linkedin.com', :t)
    """), {"c": cid, "r": rid, "u": url, "t": at})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, observed_at, capture_id)
        VALUES (:e, 'jane', :u, :t, :c) RETURNING id
    """), {"e": eid, "u": url, "t": at, "c": cid}).scalar_one()
    for section, status in sections.items():
        app.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at, capture_id)
            VALUES (:o, :s, :status, :t, :c)
        """), {"o": observation, "s": section, "status": status,
               "t": at, "c": cid})
    return observation


def _satisfies(app, sections, window="24h"):
    return freshness.satisfies(
        app, kind=resolve.PERSON, key=URL,
        freshness_seconds=freshness.parse(window), sections=sections, now=NOW)


def test_every_requested_role_must_be_fresh(app):
    """Same target, same instant, two correct answers. Asserting only one of
    these would let a URL-level freshness check pass."""
    _observation(app, at=NOW - timedelta(days=9),
                 sections={"experience": "parsed", "skills": "parsed"})
    _observation(app, at=NOW - timedelta(hours=2),
                 sections={"skills": "parsed"})

    both = _satisfies(app, ["skills", "experience"])
    only_skills = _satisfies(app, ["skills"])

    assert both.satisfied is False
    assert both.stale == ["experience"]
    assert only_skills.satisfied is True


def test_a_failure_inside_the_window_does_not_satisfy(app):
    """An authwall thirty minutes ago must not stop the scrape trying to get
    past it."""
    _observation(app, at=NOW - timedelta(days=5),
                 sections={"experience": "parsed"})
    _observation(app, at=NOW - timedelta(minutes=30),
                 sections={"experience": "error"})

    assert _satisfies(app, ["experience"]).satisfied is False


def test_a_degraded_reading_does_satisfy(app):
    """Fallback produced data and says so. A section stuck in fallback must
    not re-scrape for ever on a problem no scrape can fix."""
    _observation(app, at=NOW - timedelta(minutes=30),
                 sections={"experience": "fallback"})

    assert _satisfies(app, ["experience"]).satisfied is True


def test_the_oldest_reading_is_the_guarantee_delivered(app):
    _observation(app, at=NOW - timedelta(hours=20),
                 sections={"experience": "parsed"})
    _observation(app, at=NOW - timedelta(hours=2),
                 sections={"skills": "parsed"})

    answer = _satisfies(app, ["skills", "experience"])

    assert answer.satisfied is True
    assert answer.oldest_observed_at == NOW - timedelta(hours=20)


def test_nothing_stored_is_never_satisfied(app):
    answer = _satisfies(app, ["skills"])
    assert answer.satisfied is False
    assert answer.stale == ["skills"]


def test_a_generic_url_has_nothing_that_could_answer(app):
    """No observation tables at all, so a `url` scrape always runs rather
    than asking a question with no table behind it."""
    answer = freshness.satisfies(
        app, kind="generic", key="https://example.test/",
        freshness_seconds=86400, sections=["page"], now=NOW)
    assert answer.satisfied is False


@pytest.fixture
def client(app_url, monkeypatch):

    from career_lens.api.main import create_app

    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    return operator_client(create_app(), app_url)


def _scrape(client, **body):
    return client.post("/v1/scrape/linkedin-individual",
                       json={"url": URL, **body}).json()


def test_a_fresh_target_is_satisfied_without_a_scrape(app, client):
    _observation(app, at=datetime.now(UTC) - timedelta(hours=2),
                 sections={"skills": "parsed"})

    body = _scrape(client, linkedin_sections=["skills"], freshness="24h")

    assert body["status"] == "satisfied"
    assert body["coverage"]["sections"]["skills"]["state"] == "satisfied"
    assert app.execute(text(
        "SELECT status FROM scrape_request WHERE id = :r"),
        {"r": body["request_id"]}).scalar_one() == "satisfied"
    # No work was created for it.
    assert app.execute(text(
        "SELECT count(*) FROM scrape_attempt WHERE request_id = :r"),
        {"r": body["request_id"]}).scalar_one() == 0


def test_a_stale_target_queues_and_says_what_was_stale(app, client):
    _observation(app, at=datetime.now(UTC) - timedelta(days=9),
                 sections={"skills": "parsed"})

    body = _scrape(client, linkedin_sections=["skills"], freshness="24h")

    assert body["status"] == "queued"
    # The head document is one of the units that must be fresh, and this
    # target's is as old as its skills.
    assert body["coverage"]["sections"] == {
        "profile": {"state": "expired"}, "skills": {"state": "expired"}}


def test_freshness_is_answered_before_deduplication(app, client):
    """Two freshness values, one fixture. If dedup ran first, the fresh
    caller would be made to wait on a run they do not need."""
    _observation(app, at=datetime.now(UTC) - timedelta(hours=3),
                 sections={"skills": "parsed"})
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at,
             linkedin_sections)
        VALUES (:r, :u, :u, 'linkedin_person', 'running', :t,
                CAST('["skills"]' AS jsonb))
    """), {"r": new_id(REQUEST), "u": URL, "t": datetime.now(UTC)})

    fresh = _scrape(client, linkedin_sections=["skills"], freshness="24h")
    stale = _scrape(client, linkedin_sections=["skills"], freshness="1h")

    assert fresh["status"] == "satisfied"
    assert stale["status"] == "running"


def test_omitting_freshness_always_scrapes(app, client):
    """Every caller written before this existed keeps its behaviour."""
    _observation(app, at=datetime.now(UTC) - timedelta(minutes=1),
                 sections={"skills": "parsed"})

    assert _scrape(client)["status"] == "queued"


def test_a_satisfied_request_records_what_answered_it(app, client):
    _observation(app, at=datetime.now(UTC) - timedelta(hours=2),
                 sections={"skills": "parsed"})

    body = _scrape(client, linkedin_sections=["skills"], freshness="24h")

    sections = app.execute(text("""
        SELECT section FROM request_satisfaction WHERE request_id = :r
    """), {"r": body["request_id"]}).scalars().all()
    assert sorted(sections) == ["profile", "skills"]


def test_a_section_the_member_does_not_have_satisfies_freshness(app, client):
    """§9 of the time-travel spec, closed. A member with no patents had no
    patents reading, so a full-profile freshness check was never satisfied and
    re-scraped for ever chasing a section that will never exist."""
    _observation(app, at=datetime.now(UTC) - timedelta(hours=2),
                 sections={"skills": "parsed", "patents": "not_available"})

    body = _scrape(client, linkedin_sections=["skills", "patents"],
                   freshness="24h")

    assert body["status"] == "satisfied"


def test_a_section_we_merely_missed_still_does_not_satisfy(app, client):
    """The other half of the same distinction: `not_captured` must keep
    scraping, or the fix would satisfy freshness on sections nobody read."""
    _observation(app, at=datetime.now(UTC) - timedelta(hours=2),
                 sections={"skills": "parsed", "patents": "not_captured"})

    body = _scrape(client, linkedin_sections=["skills", "patents"],
                   freshness="24h")

    assert body["status"] == "queued"
    assert body["coverage"]["sections"]["patents"]["state"] == "missing"


def test_a_bad_freshness_is_a_400_naming_what_is_accepted(app, client):
    response = client.post("/v1/scrape/linkedin-individual",
                           json={"url": URL, "freshness": "1w"})
    assert response.status_code == 400
    assert "d" in response.json()["detail"]


