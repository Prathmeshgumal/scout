"""The parser's document, into rows.

The projection is where the parser's shape and ours are allowed to differ. Two
things it must get right: normalising the two experience DOM variants into one
uniform employer-with-roles form, and deriving only what the document does not
already carry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

import career_lens.linkedin.project as project_module
from career_lens.capture.writer import BlobMissing, Document
from career_lens.linkedin.project import _outcome, project, store_extraction
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id

NOW = datetime.now(UTC)


def test_shared_person_extraction_preserves_base_capture_context():
    from career_lens.linkedin.execution import extract_linkedin_person

    target = "https://www.linkedin.com/in/ada/"
    profile = Document(
        section="profile",
        url_requested=target,
        url_final=target,
        host="www.linkedin.com",
        http_status=200,
        captured_at=NOW,
        title="Ada | LinkedIn",
        html=b"<html><main><h1>Ada</h1></main></html>",
        scroll_stopped="bottom",
        metadata={"pendingVisuals": 0},
    )
    seen = {}

    def parse(profile_html, sections, **kwargs):
        seen.update(profile_html=profile_html, sections=sections, **kwargs)
        return {"schema_version": "2.2", "drift": []}

    def persist(database_url, **kwargs):
        seen["stored"] = kwargs

    extract_linkedin_person(
        "rq-test",
        target,
        [profile],
        {"profile": "cap-profile"},
        object(),
        parser=parse,
        persist=persist,
    )

    assert seen["profile_capture"] == {
        "scroll_stopped": "bottom",
        "pendingVisuals": 0,
    }


@pytest.fixture
def extraction_id(app) -> str:
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'running', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/in/jane/", "t": NOW})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.4', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": NOW})
    return eid


def _document(**profile) -> dict:
    base = {
        "url": "https://www.linkedin.com/in/jane/", "handle": "jane",
        "name": "Jane Doe", "headline": None, "location": None, "about": None,
        "connections": None, "followers": None, "experiences": [],
        "education": [], "skills": [], "certifications": [], "projects": [],
        "courses": [], "languages": [], "honors": [], "organizations": [],
        "patents": [], "publications": [], "featured": [],
        "volunteering": [], "test_scores": [], "services": [],
        "connected_apps": [],
        "recommendations": {"received": [], "given": []},
        "activity": {
            name: {"source_url": None, "loaded": None, "captured": 0,
                   "complete": None, "items": []}
            for name in ("posts", "comments", "articles", "reactions")},
        "about_this_member": None, "additional_sections": {},
    }
    base.update(profile)
    return {"schema_version": "2.2", "profile": base, "sources": {},
            "section_status": {}, "warnings": [], "drift": []}


def test_a_requested_section_that_was_not_captured_makes_the_result_partial():
    document = _document()
    document["section_status"] = {
        "experience": "parsed", "interests": "not_captured"}

    assert _outcome(document) == "partial"


def test_an_unavailable_section_does_not_make_the_result_partial():
    document = _document()
    document["section_status"] = {
        "experience": "parsed", "interests": "not_available"}

    assert _outcome(document) == "ok"


def test_schema_1_9_top_card_and_typed_sections_are_lossless(app, extraction_id):
    certification = {
        "name": "Cloud Architecture", "issued_on": "2026-01",
        "credential_id": "ABC-123", "issuer": {"name": "Udemy"},
        "skills": ["Systems Design"], "skills_more": 2,
    }
    featured = {
        "position": 1, "kind": "post", "id": "urn:li:activity:123",
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:123/",
        "hashtags": ["#AI"], "engagement": {"reactions_label": "2K"},
    }
    interests = {
        "captured_categories": ["newsletters"],
        "categories": {
            "newsletters": {
                "status": "parsed",
                "source_url": "https://www.linkedin.com/in/jane/details/interests/",
                "entries": [{"name": "Systems", "cadence": "Published weekly",
                             "publisher": {"name": "Engine Works"}}],
            },
        },
    }
    document = _document(
        pronouns="She/Her", current_company="Engine Works",
        current_school="Example University",
        contact_info_url="https://www.linkedin.com/in/jane/overlay/contact-info/",
        top_skills=["Systems Design"], causes=["Education"],
        certifications=[certification], featured=[featured], interests=interests)

    observation = project(app, extraction_id=extraction_id, document=document)

    top = app.execute(text("""
        SELECT pronouns, current_company, current_school, contact_info_url,
               top_skills, causes
        FROM linkedin.person_observation WHERE id = :o
    """), {"o": observation}).one()
    assert tuple(top) == (
        "She/Her", "Engine Works", "Example University",
        "https://www.linkedin.com/in/jane/overlay/contact-info/",
        ["Systems Design"], ["Education"])
    assert app.execute(text("""
        SELECT data FROM linkedin.person_certification WHERE observation_id = :o
    """), {"o": observation}).scalar_one() == certification
    assert app.execute(text("""
        SELECT data FROM linkedin.person_featured WHERE observation_id = :o
    """), {"o": observation}).scalar_one() == featured
    interest = app.execute(text("""
        SELECT category, source_url, data
        FROM linkedin.person_interest WHERE observation_id = :o
    """), {"o": observation}).one()
    assert interest.category == "newsletters"
    assert interest.source_url == "https://www.linkedin.com/in/jane/details/interests/"
    assert interest.data == interests["categories"]["newsletters"]["entries"][0]


def test_schema_1_9_activity_envelope_and_nested_item_are_lossless(app, extraction_id):
    item = {
        "position": 1, "id": "urn:li:activity:123", "relation": "reposted",
        "activity_at": "2026-08-09T21:08:12Z",
        "post": {"author": {"name": "Ada"},
                 "commentary": {"text": "A milestone"},
                 "engagement": {
                     "reactions": {"count": 59, "types": []},
                 }},
    }
    posts = {
        "source_url": "https://www.linkedin.com/in/jane/recent-activity/all/",
        "loaded": 3, "captured": 1, "complete": False, "items": [item],
    }
    document = _document()
    document["profile"]["activity"]["posts"] = posts

    observation = project(app, extraction_id=extraction_id, document=document)

    summary = app.execute(text("""
        SELECT source_url, loaded, captured, complete
        FROM linkedin.person_activity_summary
        WHERE observation_id = :o AND kind = 'post'
    """), {"o": observation}).one()
    assert tuple(summary) == (posts["source_url"], 3, 1, False)
    assert app.execute(text("""
        SELECT data FROM linkedin.person_activity
        WHERE observation_id = :o AND kind = 'post'
    """), {"o": observation}).scalar_one() == item


def test_the_scalar_fields_land_on_the_observation(app, extraction_id):
    document = _document(name="Jane Doe", headline="Engineer", location="Pune")
    observation = project(app, extraction_id=extraction_id, document=document)
    row = app.execute(text("""
        SELECT name, headline, location, handle FROM linkedin.person_observation
        WHERE id = :o
    """), {"o": observation}).one()
    assert (row.name, row.headline, row.location, row.handle) == (
        "Jane Doe", "Engineer", "Pune", "jane")


def test_a_single_role_entry_becomes_an_employer_with_one_role(app, extraction_id):
    # Upstream flattens single-role entries so `title` sits at entry level and
    # there is no `roles` array. The projection normalises that, so a consumer
    # never has to ask which DOM variant produced a row.
    document = _document(experiences=[{
        "name": "Stripe", "title": "Engineer", "date_range": "Jan 2022 - Present",
        "duration": "2 yrs", "location": "Remote", "employment_type": "Full-time",
        "description": None, "skills": [], "url": "/company/162479/",
        "total_duration": None,
    }])
    observation = project(app, extraction_id=extraction_id, document=document)
    employer = app.execute(text("""
        SELECT id, name, company_id FROM linkedin.person_employer
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert employer.name == "Stripe"
    assert employer.company_id == "162479"
    role = app.execute(text(
        "SELECT title, started_on, ended_on, is_current FROM linkedin.person_role "
        "WHERE employer_id = :e"), {"e": employer.id}).one()
    assert role.title == "Engineer"
    assert (role.started_on, role.ended_on, role.is_current) == ("2022-01", None, True)


def test_a_grouped_entry_becomes_one_employer_with_several_roles(app, extraction_id):
    document = _document(experiences=[{
        "name": "Apple", "date_range": "Oct 2021 - Jul 2024",
        "duration": "2 yrs 10 mos", "location": None, "employment_type": None,
        "total_duration": "2 yrs 10 mos", "url": None,
        "roles": [
            {"title": "Senior Technology Analyst",
             "date_range": "Oct 2022 - Jul 2024", "duration": "1 yr 10 mos",
             "location": "Pune", "employment_type": None, "description": None,
             "skills": [], "url": None},
            {"title": "Technology Analyst", "date_range": "Oct 2021 - Sep 2022",
             "duration": "1 yr", "location": "Pune", "employment_type": None,
             "description": None, "skills": [], "url": None},
        ],
    }])
    observation = project(app, extraction_id=extraction_id, document=document)
    employers = app.execute(text(
        "SELECT id FROM linkedin.person_employer WHERE observation_id = :o"),
        {"o": observation}).scalars().all()
    assert len(employers) == 1
    titles = app.execute(text(
        "SELECT title FROM linkedin.person_role WHERE employer_id = :e "
        "ORDER BY ordinal"), {"e": employers[0]}).scalars().all()
    assert titles == ["Senior Technology Analyst", "Technology Analyst"]


def test_the_reader_hands_back_an_employer_with_roles(app, extraction_id):
    """The projection stores jobs; this is the shape a reader gets them in.

    Nothing covered the key this comes back under, so a vocabulary sweep
    renamed it to `sections` -- `role` is also a captured page's section, and
    only that sense moved -- and every job title reached the console as
    `undefined` while the whole suite stayed green.
    """
    from career_lens.api.serialise import _experience

    document = _document(experiences=[{
        "name": "Apple", "date_range": "Oct 2021 - Jul 2024",
        "duration": "2 yrs 10 mos", "location": None, "employment_type": None,
        "total_duration": "2 yrs 10 mos", "url": None,
        "roles": [
            {"title": "Senior Technology Analyst",
             "date_range": "Oct 2022 - Jul 2024", "duration": "1 yr 10 mos",
             "location": "Pune", "employment_type": None, "description": None,
             "skills": [], "url": None},
            {"title": "Technology Analyst", "date_range": "Oct 2021 - Sep 2022",
             "duration": "1 yr", "location": "Pune", "employment_type": None,
             "description": None, "skills": [], "url": None},
        ],
    }])
    observation = project(app, extraction_id=extraction_id, document=document)

    employers = _experience(app, observation)

    assert len(employers) == 1
    assert "sections" not in employers[0]
    assert [role["title"] for role in employers[0]["roles"]] == [
        "Senior Technology Analyst", "Technology Analyst"]


def test_a_document_that_already_carries_dates_is_not_re_derived(app, extraction_id):
    # The automatic off-switch. When upstream starts emitting these, its values
    # win with no code change here -- including when they disagree with what we
    # would have derived, which is the case that proves the branch is real.
    document = _document(experiences=[{
        "name": "Stripe", "title": "Engineer",
        "date_range": "Jan 2022 - Present",
        "started_on": "2021-12", "ended_on": "2023-01", "is_current": False,
        "duration": None, "location": None, "employment_type": None,
        "description": None, "skills": [], "url": None, "total_duration": None,
    }])
    observation = project(app, extraction_id=extraction_id, document=document)
    role = app.execute(text("""
        SELECT r.started_on, r.ended_on, r.is_current
        FROM linkedin.person_role r
        JOIN linkedin.person_employer e ON e.id = r.employer_id
        WHERE e.observation_id = :o
    """), {"o": observation}).one()
    assert (role.started_on, role.ended_on, role.is_current) == (
        "2021-12", "2023-01", False)


def test_a_document_that_already_carries_company_id_is_not_re_derived(app, extraction_id):
    document = _document(experiences=[{
        "name": "Stripe", "title": "Engineer", "company_id": "999",
        "url": "/company/162479/", "date_range": None, "duration": None,
        "location": None, "employment_type": None, "description": None,
        "skills": [], "total_duration": None,
    }])
    observation = project(app, extraction_id=extraction_id, document=document)
    assert app.execute(text(
        "SELECT company_id FROM linkedin.person_employer WHERE observation_id = :o"),
        {"o": observation}).scalar_one() == "999"


def test_an_explicit_null_company_id_is_not_re_derived(
        app, extraction_id, monkeypatch):
    monkeypatch.setattr(
        project_module.derive, "company_id", _derivation_must_not_run)
    document = _document(experiences=[{
        "name": "Stripe", "title": "Engineer", "company_id": None,
        "url": "/company/162479/", "date_range": None, "duration": None,
        "location": None, "employment_type": None, "description": None,
        "skills": [], "total_duration": None,
    }])

    observation = project(
        app, extraction_id=extraction_id, document=document)

    assert app.execute(text(
        "SELECT company_id FROM linkedin.person_employer WHERE observation_id = :o"),
        {"o": observation}).scalar_one() is None


def test_section_statuses_are_recorded_verbatim(app, extraction_id):
    document = _document()
    document["section_status"] = {
        "experience": "parsed", "patents": "not_captured",
        "education": "parsed_inline", "publications": "fallback",
        "courses": "error",
    }
    observation = project(app, extraction_id=extraction_id, document=document)
    rows = dict(app.execute(text("""
        SELECT section, status FROM linkedin.person_section_status
        WHERE observation_id = :o
    """), {"o": observation}).all())
    assert rows == document["section_status"]


def test_a_provisional_entrys_detail_lines_are_kept(app, extraction_id):
    # extract_simple emits `details` -- plural, and a list of the entry's
    # remaining lines. Reading `detail` instead silently stores nothing, which
    # is why this pins the real key rather than trusting the column name.
    document = _document(skills=[
        {"name": "Python", "details": ["3 endorsements", "Top skill"],
         "text": "Python 3 endorsements", "links": []}])
    observation = project(app, extraction_id=extraction_id, document=document)
    row = app.execute(text("""
        SELECT name, detail, text FROM linkedin.person_skill
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert row.name == "Python"
    assert "3 endorsements" in row.detail
    assert "Top skill" in row.detail


@pytest.mark.parametrize(("field", "table_name"), (
    ("volunteering", "person_volunteering"),
    ("test_scores", "person_test_score"),
    ("services", "person_service"),
    ("connected_apps", "person_connected_app"),
))
def test_new_profile_collections_project_losslessly(
    app, extraction_id, field: str, table_name: str,
):
    entry = {
        "name": "Example", "details": ["Supporting detail"],
        "text": "Example\nSupporting detail", "links": [],
    }
    observation = project(
        app, extraction_id=extraction_id, document=_document(**{field: [entry]}))

    row = app.execute(text(f"""
        SELECT name, detail, data FROM linkedin.{table_name}
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert row.name == "Example"
    assert row.detail == "Supporting detail"
    assert row.data == entry


def test_structured_skill_details_are_kept_without_flattening(app, extraction_id):
    details = [
        {"text": "Endorsed by 25 colleagues", "url": "https://example/25"},
        {"text": "99+ endorsements", "url": "https://example/99"},
    ]
    observation = project(
        app, extraction_id=extraction_id,
        document=_document(skills=[{"name": "Python", "details": details}]))

    row = app.execute(text("""
        SELECT name, details FROM linkedin.person_skill
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert row.name == "Python"
    assert row.details == details


def test_an_unknown_section_lands_in_additional_sections(app, extraction_id):
    # `interests` is captured by sections.py and has no extractor upstream, so
    # it arrives here. It must be stored rather than dropped.
    document = _document()
    document["profile"]["additional_sections"] = {
        "interests": {"text": "Companies, Groups", "links": []}}
    observation = project(app, extraction_id=extraction_id, document=document)
    row = app.execute(text("""
        SELECT section, text FROM linkedin.person_additional_section
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert row.section == "interests"
    assert "Companies" in row.text


def test_recommendations_keep_their_direction(app, extraction_id):
    document = _document()
    document["profile"]["recommendations"] = {
        # Recommendations use extract_simple, not the activity extractor: the
        # parser emits its name/details/text/links shape, with no author dict.
        "received": [{
            "name": "A", "details": ["Director", "Example Co"],
            "text": "A\nDirector\nExample Co\nGreat work",
            "links": [{"url": "https://www.linkedin.com/in/a/", "text": "A"}],
        }],
        "given": [{
            "name": "B", "details": ["Engineer"],
            "text": "B\nEngineer\nAlso great", "links": [],
        }],
    }
    observation = project(app, extraction_id=extraction_id, document=document)
    rows = {
        row.direction: row for row in app.execute(text("""
        SELECT direction, person_name, person_headline, text, links
        FROM linkedin.person_recommendation
        WHERE observation_id = :o
    """), {"o": observation}).all()
    }
    assert rows["received"].person_name == "A"
    assert rows["received"].person_headline == "Director · Example Co"
    assert rows["received"].text == "A\nDirector\nExample Co\nGreat work"
    assert rows["received"].links == [{"url": "https://www.linkedin.com/in/a/", "text": "A"}]
    assert rows["given"].person_name == "B"
    assert rows["given"].person_headline == "Engineer"


def test_structured_recommendations_keep_their_party_and_context(
        app, extraction_id):
    document = _document()
    document["profile"]["recommendations"] = {
        "received": [{
            "from": {
                "name": "Charles Babbage", "headline": "Inventor at Engine Works",
                "company": "Engine Works", "connection_degree": "3rd+",
                "profile_url": "https://www.linkedin.com/in/charles/",
            },
            "date": "April 10, 1843", "relationship": "Charles was Ada’s client",
            "text": "A remarkable collaborator.",
        }],
        "given": [],
    }
    observation = project(app, extraction_id=extraction_id, document=document)

    row = app.execute(text("""
        SELECT person_name, person_headline, person_company, connection_degree,
               profile_url, recommended_at, relationship, text
        FROM linkedin.person_recommendation WHERE observation_id = :o
    """), {"o": observation}).one()
    assert tuple(row) == (
        "Charles Babbage", "Inventor at Engine Works", "Engine Works", "3rd+",
        "https://www.linkedin.com/in/charles/", "April 10, 1843",
        "Charles was Ada’s client", "A remarkable collaborator.")


def test_real_parser_activity_shape_uses_timestamp_and_nested_content(app, extraction_id):
    # extract_activity emits timestamp and content.text, not posted_at and a
    # top-level text field. This is the actual parser boundary shape.
    document = _document()
    document["profile"]["activity"] = {
        "posts": [{
            "author": {"name": "A", "title": "Director", "url": "https://www.linkedin.com/in/a/"},
            "timestamp": "2d", "edited": False, "visibility": None,
            "content": {"text": "A real parser-shaped post", "media": []},
            "engagement": {
                "reactions": {"count": 0, "types": []},
                "comments": 0,
                "reposts": 0,
            },
        }],
        "comments": [], "articles": [],
    }
    observation = project(app, extraction_id=extraction_id, document=document)
    row = app.execute(text("""
        SELECT author_name, posted_at, text FROM linkedin.person_activity
        WHERE observation_id = :o AND kind = 'post'
    """), {"o": observation}).one()
    assert (row.author_name, row.posted_at, row.text) == (
        "A", "2d", "A real parser-shaped post")


def test_activity_metadata_and_reactions_are_projected(app, extraction_id):
    entry = {
        "author": {
            "name": "Ada", "title": "Mathematician", "company": "Engine Works",
            "connection_level": "2nd", "url": "https://linkedin.com/in/ada/",
            "badges": ["Verified"],
        },
        "timestamp": "3h", "edited": True, "visibility": "Anyone",
        "content": {
            "text": "Reacted to a note",
            "media": [{"type": "link_preview", "url": "https://example.test"}],
        },
        "engagement": {
            "reactions": {"count": 4, "types": ["like"]},
            "comments": 2,
            "reposts": 1,
        },
    }
    document = _document()
    document["profile"]["activity"]["reactions"] = [entry]
    observation = project(app, extraction_id=extraction_id, document=document)

    row = app.execute(text("""
        SELECT kind, author_name, author_title, author_company,
               author_connection_level, author_url, author_badges, edited,
               visibility, media, engagement, text
        FROM linkedin.person_activity WHERE observation_id = :o
    """), {"o": observation}).one()
    assert tuple(row) == (
        "reaction", "Ada", "Mathematician", "Engine Works", "2nd",
        "https://linkedin.com/in/ada/", ["Verified"], True, "Anyone",
        [{"type": "link_preview", "url": "https://example.test"}],
        {
            "reactions": {"count": 4, "types": ["like"]},
            "comments": 2,
            "reposts": 1,
        },
        "Reacted to a note",
    )


@pytest.mark.parametrize("provided,expected", [
    ({"started_on": None}, (None, None, True)),
    ({"ended_on": "2020-12"}, ("2022-01", "2020-12", True)),
    ({"is_current": None}, ("2022-01", None, None)),
])
def test_each_present_role_date_field_wins_over_derivation(
        app, extraction_id, provided, expected):
    # Field presence, including an explicit null, is the off-switch. Each case
    # independently catches an accidental truthiness/non-null check.
    role = {
        "name": "Stripe", "title": "Engineer", "date_range": "Jan 2022 - Present",
        "duration": None, "location": None, "employment_type": None,
        "description": None, "skills": [], "url": None, "total_duration": None,
        **provided,
    }
    observation = project(app, extraction_id=extraction_id,
                          document=_document(experiences=[role]))
    row = app.execute(text("""
        SELECT r.started_on, r.ended_on, r.is_current
        FROM linkedin.person_role r
        JOIN linkedin.person_employer e ON e.id = r.employer_id
        WHERE e.observation_id = :o
    """), {"o": observation}).one()
    assert (row.started_on, row.ended_on, row.is_current) == expected


@pytest.mark.parametrize("provided,expected", [
    ({"started_on": None}, (None, "2024-07")),
    ({"ended_on": "2020-12"}, ("2021-10", "2020-12")),
])
def test_each_present_education_date_field_wins_over_derivation(
        app, extraction_id, provided, expected):
    education = {
        "school": "Example University", "url": None, "degree": None,
        "field_of_study": None, "date_range": "Oct 2021 - Jul 2024",
        "grade": None, "activities": None, "description": None,
        "skills": [], "text": None, "links": [], **provided,
    }
    observation = project(app, extraction_id=extraction_id,
                          document=_document(education=[education]))
    row = app.execute(text("""
        SELECT started_on, ended_on FROM linkedin.person_education
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert (row.started_on, row.ended_on) == expected


def _derivation_must_not_run(_: str | None):
    raise AssertionError("fully parser-supplied date fields must switch derivation off")


def test_a_fully_supplied_role_never_calls_date_derivation(
        app, extraction_id, monkeypatch):
    monkeypatch.setattr(project_module.derive, "date_range", _derivation_must_not_run)
    role = {
        "name": "Stripe", "title": "Engineer", "date_range": "not a range",
        "started_on": "2021-12", "ended_on": None, "is_current": False,
        "duration": None, "location": None, "employment_type": None,
        "description": None, "skills": [], "url": None, "total_duration": None,
    }
    observation = project(app, extraction_id=extraction_id,
                          document=_document(experiences=[role]))
    row = app.execute(text("""
        SELECT r.started_on, r.ended_on, r.is_current
        FROM linkedin.person_role r
        JOIN linkedin.person_employer e ON e.id = r.employer_id
        WHERE e.observation_id = :o
    """), {"o": observation}).one()
    assert (row.started_on, row.ended_on, row.is_current) == (
        "2021-12", None, False)


def test_a_fully_supplied_education_never_calls_date_derivation(
        app, extraction_id, monkeypatch):
    monkeypatch.setattr(project_module.derive, "date_range", _derivation_must_not_run)
    education = {
        "school": "Example University", "url": None, "degree": None,
        "field_of_study": None, "date_range": "not a range",
        "started_on": None, "ended_on": "2024-07", "is_current": False,
        "grade": None,
        "activities": None, "description": None, "skills": [],
        "text": None, "links": [],
    }
    observation = project(app, extraction_id=extraction_id,
                          document=_document(education=[education]))
    row = app.execute(text("""
        SELECT started_on, ended_on, is_current FROM linkedin.person_education
        WHERE observation_id = :o
    """), {"o": observation}).one()
    assert (row.started_on, row.ended_on, row.is_current) == (
        None, "2024-07", False)


def test_projecting_twice_yields_two_independent_observations(app, extraction_id):
    document = _document(name="Jane Doe")
    first = project(app, extraction_id=extraction_id, document=document)
    second = project(app, extraction_id=extraction_id, document=document)
    assert first != second
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.person_observation")).scalar_one() == 2


def test_a_document_from_another_parser_version_is_not_projected(app, extraction_id):
    document = _document()
    document["schema_version"] = "9.9"
    with pytest.raises(ValueError, match="schema_version"):
        project(app, extraction_id=extraction_id, document=document)
    assert app.execute(text(
        "SELECT count(*) FROM linkedin.person_observation")).scalar_one() == 0


class _Store:
    """In-memory blob store for the project writer's verified-write boundary."""

    def __init__(self, *, swallow: bool = False):
        self.objects: dict[str, bytes] = {}
        self.swallow = swallow

    def put(self, key, data, content_type):
        if not self.swallow:
            self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def url(self, key, expires=3600):
        return f"https://blobs.invalid/{key}"


def test_a_silently_dropped_document_blob_creates_no_extraction(app_url, app):
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'running', :t)
    """), {"r": request_id, "u": "https://www.linkedin.com/in/jane/", "t": NOW})
    with pytest.raises(BlobMissing):
        store_extraction(app_url, request_id=request_id, capture_ids={},
                         document=_document(), store=_Store(swallow=True))
    assert app.execute(text("SELECT count(*) FROM extraction")).scalar_one() == 0


def _captured_extraction(app, *, head_at, skills_at):
    """A request whose profile page and skills page were fetched apart."""
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    head, skills = new_id(CAPTURE), new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'running', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/in/jane/", "t": head_at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.4', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": skills_at + timedelta(hours=1)})
    for capture_id, section, when in ((head, "profile", head_at),
                                      (skills, "skills", skills_at)):
        app.execute(text("""
            INSERT INTO capture (id, request_id, section, url_requested, host,
                                 captured_at)
            VALUES (:c, :r, :section, 'https://x/', 'x', :t)
        """), {"c": capture_id, "r": rid, "section": section, "t": when})
        app.execute(text("""
            INSERT INTO extraction_input (extraction_id, capture_id, section)
            VALUES (:e, :c, :section)
        """), {"e": eid, "c": capture_id, "section": section})
    return eid, head, skills


def test_the_projection_records_each_sections_own_capture(app):
    """Two pages fetched three minutes apart report two different instants,
    and neither reports when the extraction ran an hour later."""
    early = datetime(2026, 6, 1, 9, 4, tzinfo=UTC)
    late = early + timedelta(minutes=3)
    eid, head, skills = _captured_extraction(app, head_at=early,
                                             skills_at=late)
    document = _document()
    document["section_status"] = {"skills": "parsed",
                                  "education": "parsed_inline"}

    observation = project(app, extraction_id=eid, document=document)

    row = app.execute(text("""
        SELECT observed_at, capture_id FROM linkedin.person_observation
        WHERE id = :o
    """), {"o": observation}).one()
    assert (row.observed_at, row.capture_id) == (early, head)

    rows = {r.section: r for r in app.execute(text("""
        SELECT section, observed_at, capture_id
        FROM linkedin.person_section_status WHERE observation_id = :o
    """), {"o": observation}).all()}
    assert (rows["skills"].observed_at, rows["skills"].capture_id) == (
        late, skills)
    # Parsed inline: no page of its own, so the head document's -- which is
    # literally where the value was read.
    assert (rows["education"].observed_at, rows["education"].capture_id) == (
        early, head)


def test_an_extraction_with_no_capture_falls_back_to_when_it_ran(
        app, extraction_id):
    """Extractions from before the attempt bridge have no capture. They still
    need an instant, and the moment they ran is the only one that exists."""
    document = _document()
    document["section_status"] = {"skills": "parsed"}
    observation = project(app, extraction_id=extraction_id, document=document)
    row = app.execute(text("""
        SELECT observed_at, capture_id FROM linkedin.person_observation
        WHERE id = :o
    """), {"o": observation}).one()
    assert row.capture_id is None
    assert row.observed_at is not None


CONTACT_INFO = {
    "profile_url": "https://www.linkedin.com/in/jane/",
    "websites": [{"url": "https://example.com/", "label": "Company"}],
    "email": "jane@example.com",
    "phones": [{"number": "+1 555 0100", "type": "Mobile"}],
    "im": [], "address": None, "birthday": "February 17",
    "connected_on": None, "other": [], "links": [],
}


def test_contact_info_is_stored_as_the_dialog_reported_it(app, extraction_id):
    """A column of its own rather than the top card's link. The link says
    where the details are; this says what they were."""
    project(app, extraction_id=extraction_id,
            document=_document(contact_info=CONTACT_INFO))

    stored = app.execute(text("""
        SELECT contact_info FROM linkedin.person_observation
    """)).scalar_one()
    assert stored == CONTACT_INFO


def test_a_profile_with_no_contact_dialog_stores_no_contact_info(app,
                                                                 extraction_id):
    """Null rather than an empty object: nothing was read, and a row claiming
    an empty dialog would be a reading nobody took."""
    project(app, extraction_id=extraction_id, document=_document())

    assert app.execute(text("""
        SELECT contact_info FROM linkedin.person_observation
    """)).scalar_one() is None


OPEN_TO_WORK = {
    "job_titles": ["Software Engineer", "Data Engineer"],
    "location_types": ["On-site", "Hybrid"],
    "locations": ["California, United States"],
    "start_date": "Immediately, I am actively applying",
    "employment_types": ["Full-time"],
}


def test_open_to_work_is_stored_as_the_dialog_reported_it(app, extraction_id):
    project(app, extraction_id=extraction_id,
            document=_document(open_to_work=OPEN_TO_WORK))

    assert app.execute(text("""
        SELECT open_to_work FROM linkedin.person_observation
    """)).scalar_one() == OPEN_TO_WORK


def test_a_member_who_is_not_looking_stores_no_open_to_work(app, extraction_id):
    """Most profiles. Null rather than an empty object, which would claim a
    dialog somebody opened and found bare."""
    project(app, extraction_id=extraction_id, document=_document())

    assert app.execute(text("""
        SELECT open_to_work FROM linkedin.person_observation
    """)).scalar_one() is None
