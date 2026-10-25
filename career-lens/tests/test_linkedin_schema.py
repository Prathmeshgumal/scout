"""What the linkedin tables hold, and what they refuse."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from career_lens.store.ids import EXTRACTION, REQUEST, new_id

NOW = datetime.now(UTC)


def _observation(connection) -> int:
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/in/x/", "t": NOW})
    connection.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.0', :t, 'ok',
                'k', 'abc', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    return connection.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at)
        VALUES (:e, 'x', 'https://www.linkedin.com/in/x/', 'X', :t)
        RETURNING id
    """), {"e": eid, "t": NOW}).scalar_one()


def test_a_person_observation_can_be_written_and_read(app):
    observation = _observation(app)
    assert app.execute(text(
        "SELECT name FROM linkedin.person_observation WHERE id = :i"),
        {"i": observation}).scalar_one() == "X"


def test_observations_are_append_only_for_the_owner(migrator):
    _observation(migrator)
    with pytest.raises(DBAPIError) as raised:
        migrator.execute(text(
            "UPDATE linkedin.person_observation SET name = 'Y'"))
    assert raised.value.orig.sqlstate == "CL001"


def test_the_app_role_cannot_update_an_observation(app):
    _observation(app)
    with pytest.raises(DBAPIError) as raised:
        app.execute(text("UPDATE linkedin.person_observation SET name = 'Y'"))
    assert raised.value.orig.sqlstate == "42501"


def test_re_extraction_appends_a_second_observation(app):
    first, second = _observation(app), _observation(app)
    assert first != second
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.person_observation")).scalar_one() == 2


def test_an_unknown_section_status_is_refused(app):
    observation = _observation(app)
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at)
            VALUES (:o, 'experience', 'probably_fine', :t)
        """), {"o": observation, "t": NOW})


def test_a_person_section_may_say_the_profile_has_no_such_section(app):
    """The word companies have had since 0028. Without it in the person
    vocabulary, `not_captured` meant both "we missed it" and "there is none",
    and a freshness check chased patents that will never exist."""
    observation = _observation(app)
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'patents', 'not_available', :t)
    """), {"o": observation, "t": NOW})

    assert app.execute(text("""
        SELECT status FROM linkedin.person_section_status
        WHERE observation_id = :o AND section = 'patents'
    """), {"o": observation}).scalar_one() == "not_available"


@pytest.mark.parametrize(("section", "table_name"), (
    ("volunteering", "person_volunteering"),
    ("test-scores", "person_test_score"),
    ("services", "person_service"),
    ("connected-apps", "person_connected_app"),
))
def test_new_profile_sections_have_status_and_lossless_tables(
    app, section: str, table_name: str,
):
    observation = _observation(app)
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, :section, 'parsed_inline', :t)
    """), {"o": observation, "section": section, "t": NOW})
    app.execute(text(f"""
        INSERT INTO linkedin.{table_name}
            (observation_id, ordinal, name, data)
        VALUES (:o, 0, 'Example', CAST('{{"extra":"kept"}}' AS jsonb))
    """), {"o": observation})

    assert app.execute(text(f"""
        SELECT data->>'extra' FROM linkedin.{table_name}
        WHERE observation_id = :o
    """), {"o": observation}).scalar_one() == "kept"


def test_a_section_appears_at_most_once_per_observation(app):
    observation = _observation(app)
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'experience', 'parsed', :t)
    """), {"o": observation, "t": NOW})
    with pytest.raises(IntegrityError):
        app.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at)
            VALUES (:o, 'experience', 'error', :t)
        """), {"o": observation, "t": NOW})


def test_an_empty_section_and_an_uncaptured_one_are_different_rows(app):
    # The distinction the whole status vocabulary exists for: "they list no
    # publications" is not "we never fetched the publications page".
    observation = _observation(app)
    app.execute(text("""
        INSERT INTO linkedin.person_section_status
            (observation_id, section, status, observed_at)
        VALUES (:o, 'publications', 'parsed', :t),
               (:o, 'patents', 'not_captured', :t)
    """), {"o": observation, "t": NOW})
    rows = dict(app.execute(text("""
        SELECT section, status FROM linkedin.person_section_status
        WHERE observation_id = :o
    """), {"o": observation}).all())
    assert rows == {"publications": "parsed", "patents": "not_captured"}


def test_a_role_hangs_off_an_employer_not_an_observation(app):
    observation = _observation(app)
    employer = app.execute(text("""
        INSERT INTO linkedin.person_employer (observation_id, ordinal, name)
        VALUES (:o, 0, 'Stripe') RETURNING id
    """), {"o": observation}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_role (employer_id, ordinal, title)
        VALUES (:e, 0, 'Engineer')
    """), {"e": employer})
    assert app.execute(text("""
        SELECT r.title FROM linkedin.person_role r
        JOIN linkedin.person_employer e ON e.id = r.employer_id
        WHERE e.observation_id = :o
    """), {"o": observation}).scalar_one() == "Engineer"


def test_one_employer_can_carry_several_roles(app):
    # The DOM shape that made this two tables: LinkedIn renders a multi-section
    # employer differently from a single-section one, and the projection
    # normalises both into employer-with-sections.
    observation = _observation(app)
    employer = app.execute(text("""
        INSERT INTO linkedin.person_employer (observation_id, ordinal, name)
        VALUES (:o, 0, 'Apple') RETURNING id
    """), {"o": observation}).scalar_one()
    app.execute(text("""
        INSERT INTO linkedin.person_role (employer_id, ordinal, title)
        VALUES (:e, 0, 'Senior Analyst'), (:e, 1, 'Analyst')
    """), {"e": employer})
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.person_role WHERE employer_id = :e"),
        {"e": employer}).scalar_one() == 2


def _company(connection) -> int:
    """A company observation with the request and extraction it hangs off.

    Mirrors _observation() exactly, because the foreign keys are the same and a
    shortcut here would test a shape the projection cannot actually write.
    """
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_company', 'succeeded', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/company/stripe/", "t": NOW})
    connection.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_company_json', '1.0', :t, 'ok',
                'k', 'abc', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    return connection.execute(text("""
        INSERT INTO linkedin.company_observation
            (extraction_id, slug, name, observed_at)
        VALUES (:e, 'stripe', 'Stripe', :t)
        RETURNING id
    """), {"e": eid, "t": NOW}).scalar_one()


def test_a_company_observation_can_be_written_and_read(app):
    observation = _company(app)
    assert app.execute(text(
        "SELECT slug FROM linkedin.company_observation WHERE id = :o"),
        {"o": observation}).scalar_one() == "stripe"


def test_re_extraction_appends_a_second_company_observation(app):
    first, second = _company(app), _company(app)
    assert first != second
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.company_observation")).scalar_one() == 2


def test_a_company_observation_cannot_be_updated(app):
    """Append-only: a re-reading is a new observation, never an edit."""
    observation = _company(app)
    with pytest.raises(DBAPIError):
        app.execute(text(
            "UPDATE linkedin.company_observation SET name = 'x' WHERE id = :o"),
            {"o": observation})


def test_an_unknown_company_section_status_is_refused(app):
    """One vocabulary with the person side: these distinctions are about what
    the parser managed to do, not about people."""
    observation = _company(app)
    with pytest.raises(DBAPIError):
        app.execute(text("""
            INSERT INTO linkedin.company_section_status
                (observation_id, section, status, observed_at)
            VALUES (:o, 'about', 'invented', :t)
        """), {"o": observation, "t": NOW})


def test_an_office_holds_what_the_capture_actually_yields(app):
    """dom.company_locations() emits exactly these fields, so the table takes
    them without the parser having to reshape anything."""
    observation = _company(app)
    app.execute(text("""
        INSERT INTO linkedin.company_office
            (observation_id, ordinal, name, description, headquarters, line1,
             line2, city, geographic_area, postal_code, country, latitude,
             longitude)
        VALUES (:o, 0, 'San Francisco', 'HQ', true, '510 Townsend St', NULL,
                'San Francisco', 'California', '94103', 'US', '37.7706',
                '-122.4130')
    """), {"o": observation})

    row = app.execute(text("""
        SELECT city, headquarters, latitude FROM linkedin.company_office
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert row.city == "San Francisco"
    assert row.headquarters is True
    assert row.latitude == "37.7706"


def test_every_observation_row_can_say_when_it_was_read(app):
    """The walk orders by instant, so an instant is not optional."""
    for table in ("person_observation", "person_section_status",
                  "company_observation", "company_section_status"):
        nullable = app.execute(text("""
            SELECT is_nullable FROM information_schema.columns
            WHERE table_schema = 'linkedin' AND table_name = :t
              AND column_name = 'observed_at'
        """), {"t": table}).scalar_one_or_none()
        assert nullable == "NO", f"linkedin.{table}.observed_at is missing or nullable"


def test_a_sections_capture_is_reachable_without_a_join(app):
    """`meta` names the capture behind each section, and a join per section on
    every read is what denormalising the instant exists to avoid."""
    for table in ("person_observation", "person_section_status",
                  "company_observation", "company_section_status"):
        found = app.execute(text("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'linkedin' AND table_name = :t
              AND column_name = 'capture_id'
        """), {"t": table}).scalar_one()
        assert found == 1, f"linkedin.{table} cannot name its capture"


def test_an_instant_cannot_be_corrected_after_the_fact(app):
    """observed_at is denormalised, which is only safe because neither row can
    ever be updated. A back-fill at runtime is how that would stop holding."""
    with pytest.raises(DBAPIError):
        app.execute(text(
            "UPDATE linkedin.person_section_status SET observed_at = now()"))
