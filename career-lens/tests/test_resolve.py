"""Which stored reading answers, and when it was read.

Every test here builds its rows by hand rather than running a projection: the
walk's contract is about ordering and grades, and a fixture that had to produce
a real extraction would obscure which column the assertion is about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.observe import resolve
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id

URL = "https://www.linkedin.com/in/jane/"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


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


def test_a_section_the_newest_reading_did_not_look_at_walks_back(app):
    """The defect this fixes: a scrape of one section must not erase the rest."""
    old = _observation(app, at=NOW - timedelta(days=9),
                       sections={"experience": "parsed", "skills": "parsed"})
    _observation(app, at=NOW - timedelta(hours=2),
                 sections={"experience": "not_requested", "skills": "parsed"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW)

    assert found["experience"].observation_id == old
    assert found["experience"].observed_at == NOW - timedelta(days=9)
    assert found["skills"].observed_at == NOW - timedelta(hours=2)


def test_an_older_capture_projected_later_does_not_become_current(app):
    """Ordering is by instant. Insertion order is a tiebreak, nothing more --
    which is what will make re-extraction safe to add."""
    recent = _observation(app, at=NOW - timedelta(hours=1),
                          sections={"skills": "parsed"})
    _observation(app, at=NOW - timedelta(days=30),
                 sections={"skills": "parsed"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW)

    assert found["skills"].observation_id == recent


def test_at_bounds_the_belief_from_above(app):
    march = _observation(app, at=datetime(2026, 3, 1, tzinfo=UTC),
                         sections={"skills": "parsed"})
    _observation(app, at=datetime(2026, 5, 1, tzinfo=UTC),
                 sections={"skills": "parsed"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL,
                         at=datetime(2026, 4, 1, tzinfo=UTC))

    assert found["skills"].observation_id == march


def test_nothing_observed_yet_resolves_to_nothing(app):
    _observation(app, at=NOW, sections={"skills": "parsed"})
    assert resolve.walk(app, kind=resolve.PERSON, key=URL,
                        at=NOW - timedelta(days=1)) == {}


def test_a_drifted_section_is_stepped_over_when_stale_is_allowed(app):
    good = _observation(app, at=NOW - timedelta(days=6),
                        sections={"experience": "parsed"})
    _observation(app, at=NOW - timedelta(hours=8),
                 sections={"experience": "drift"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW)

    assert found["experience"].observation_id == good
    assert found["experience"].grade == resolve.READ


def test_a_drifted_section_is_fatal_when_stale_is_refused(app):
    _observation(app, at=NOW - timedelta(days=6),
                 sections={"experience": "parsed"})
    _observation(app, at=NOW - timedelta(hours=8),
                 sections={"experience": "drift"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW,
                         allow_stale=False)

    assert found["experience"].grade == resolve.FAILED
    assert found["experience"].status == "drift"


def test_a_capture_that_never_looked_is_stepped_over_in_both_modes(app):
    """Not evidence either way -- so neither mode may stop on it."""
    good = _observation(app, at=NOW - timedelta(days=3),
                        sections={"experience": "parsed"})
    _observation(app, at=NOW - timedelta(hours=1),
                 sections={"experience": "not_requested"})

    for allow_stale in (True, False):
        found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW,
                             allow_stale=allow_stale)
        assert found["experience"].observation_id == good, allow_stale


def test_the_head_resolves_alongside_the_sections(app):
    _observation(app, at=NOW - timedelta(days=2), sections={})
    newest = _observation(app, at=NOW - timedelta(hours=3), sections={})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW)

    assert found["profile"].observation_id == newest
    assert found["profile"].grade == resolve.READ


def test_roles_narrows_what_is_asked_for(app):
    _observation(app, at=NOW, sections={"skills": "parsed",
                                        "experience": "parsed"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW,
                         sections=["skills"])

    assert set(found) == {"skills", "profile"}


@pytest.mark.parametrize("status,expected", [
    ("parsed", resolve.READ),
    ("parsed_inline", resolve.READ),
    ("fallback", resolve.DEGRADED),
    ("error", resolve.FAILED),
    ("drift", resolve.FAILED),
    ("not_captured", resolve.NEVER),
    ("not_requested", resolve.NEVER),
    # A company tab that does not exist is a reading: "this company has no
    # Life tab" is data, not a gap.
    ("not_available", resolve.READ),
    ("parse_failed", resolve.FAILED),
])
def test_every_stored_status_has_a_deliberate_grade(status, expected):
    assert resolve.grade(status) == expected


def test_the_grade_table_covers_both_stored_vocabularies():
    """An enumeration, not a spot check: a status added to the parser without
    a grade must fail here rather than default into silence."""
    from career_lens.linkedin.schema import (
        COMPANY_SECTION_STATUSES, SECTION_STATUSES,
    )

    for status in set(SECTION_STATUSES) | set(COMPANY_SECTION_STATUSES):
        assert resolve.grade(status) in (
            resolve.READ, resolve.DEGRADED, resolve.FAILED, resolve.NEVER), (
            f"{status} has no grade")


def test_an_ungraded_status_raises_rather_than_defaulting():
    with pytest.raises(resolve.UnknownStatus):
        resolve.grade("invented_by_a_later_parser")


def test_a_section_whose_only_reading_failed_says_so_rather_than_vanishing(app):
    """Walking back to nothing is not the same as never having looked, and
    reporting one as the other is exactly the lie the status vocabulary exists
    to prevent."""
    _observation(app, at=NOW - timedelta(hours=2),
                 sections={"experience": "error"})

    found = resolve.walk(app, kind=resolve.PERSON, key=URL, at=NOW)

    assert found["experience"].grade == resolve.FAILED
    assert found["experience"].status == "error"
    # No observation to read a payload from: the section is empty, and the
    # caller is told why.
    assert found["experience"].observation_id is None


