"""The copied parser, over real captured HTML.

These tests are not re-testing upstream's logic -- it has its own suite. What
they establish is that the copy works in this repo, on our fixtures, and
produces the shape ../project.py is written against. A change upstream that
breaks that shape should fail here loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from career_lens.linkedin.parse import EXPECTED_VERSION, parse_profile_html
from career_lens.linkedin.schema import SECTION_STATUSES

FIXTURES = Path(__file__).parent / "fixtures" / "profiles"


def _load(handle: str) -> tuple[str, dict[str, str]]:
    root = FIXTURES / handle
    profile = (root / "profile.html").read_text(encoding="utf-8")
    sections = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.html")) if path.stem != "profile"
    }
    return profile, sections


def _handles() -> list[str]:
    return sorted(p.name for p in FIXTURES.iterdir() if p.is_dir())


@pytest.fixture(scope="session", params=_handles())
def fixture_input(request) -> tuple[str, str, dict[str, str]]:
    profile, sections = _load(request.param)
    return request.param, profile, sections


@pytest.fixture(scope="session")
def parsed(fixture_input) -> dict:
    handle, profile, sections = fixture_input
    return parse_profile_html(
        profile, sections,
        profile_url=f"https://www.linkedin.com/in/{handle}/")


def test_the_document_carries_the_version_we_project_against(parsed):
    # Not an assertion that upstream must never change -- an assertion that we
    # find out when it does, at the moment it does, rather than through a
    # projection quietly reading fields that moved.
    assert parsed["schema_version"] == EXPECTED_VERSION


def test_the_document_has_the_six_top_level_keys(parsed):
    assert set(parsed) == {
        "schema_version", "profile", "sources", "section_status", "warnings",
        "drift"}


def test_every_fixture_section_is_represented_as_a_source(fixture_input, parsed):
    # If the parser drops a supplied capture, its content cannot be recovered
    # downstream. Derive the expected names from fixture inputs, not output.
    _, _, supplied_sections = fixture_input
    assert set(parsed["sources"]) == {"profile", *supplied_sections}


def test_every_fixture_section_has_a_meaningful_status(fixture_input, parsed):
    # A supplied section missing from section_status, or marked not_captured,
    # has an unknown fate. The fixture inputs are the independent oracle.
    _, _, supplied_sections = fixture_input
    supplied_names = set(supplied_sections)
    assert supplied_names <= set(parsed["section_status"])
    assert {
        parsed["section_status"][name] for name in supplied_names
    } <= {"parsed", "fallback", "error"}
    assert parsed["section_status"]
    # From the schema rather than restated here: the column's CHECK is the
    # authority on what a status may be, and a copy of it drifts.
    assert set(parsed["section_status"].values()) <= set(SECTION_STATUSES)


def test_unrequested_sections_are_distinct_from_missing_requested_sections():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"

    parsed = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/jane/",
        requested_sections=frozenset({"experience"}),
    )

    assert parsed["section_status"]["experience"] == "not_captured"
    assert parsed["section_status"]["education"] == "not_requested"
    assert parsed["section_status"]["activity-posts"] == "not_requested"


def test_activity_card_class_change_is_reported_as_parser_drift():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"
    activity = """
    <html><body><main>
      <p>Loaded 20 Posts posts</p>
      <div class="renamed-feed-card" data-urn="urn:li:activity:123">
        <span>Jane posted this</span>
      </div>
    </main></body></html>
    """

    parsed = parse_profile_html(
        profile, {"activity-posts": activity},
        profile_url="https://www.linkedin.com/in/jane/")

    assert parsed["section_status"]["activity-posts"] == "drift"
    assert parsed["drift"] == [{
        "section": "activity-posts",
        "contract": "feed activity cards",
        "expected": "at least one feed-shared-update-v2 card",
        "found": "0 cards while LinkedIn announced 20 posts",
    }]
    assert parsed["sources"]["activity-posts"]["content"]["text"]


def test_certification_date_wording_change_is_reported_as_parser_drift():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"
    certifications = """
    <html><body><main><section>
      <div>
        <div>
          <p>Licenses & certifications</p>
          <div><p>Cloud One</p><p>Issued through Org One · Jun 2022</p></div>
          <div><p>Cloud Two</p><p>Issued through Org Two · Jul 2022</p></div>
          <div><p>Cloud Three</p><p>Issued through Org Three · Aug 2022</p></div>
        </div>
      </div>
    </section></main></body></html>
    """

    parsed = parse_profile_html(
        profile, {"certifications": certifications},
        profile_url="https://www.linkedin.com/in/jane/")

    assert parsed["section_status"]["certifications"] == "drift"
    assert parsed["drift"] == [{
        "section": "certifications",
        "contract": "certification issued date",
        "expected": "issued_on on at least one of 3 entries",
        "found": "0 of 3 entries had issued_on",
    }]


def test_featured_item_anchor_change_is_reported_as_parser_drift():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"
    featured = """
    <html><body><main>
      <section>
        <h2>Featured</h2>
        <button data-id="urn:li:activity:1234567890">Featured post</button>
        <img src="https://media.example/company-logo.png"/>
      </section>
    </main></body></html>
    """

    parsed = parse_profile_html(
        profile, {"featured": featured},
        profile_url="https://www.linkedin.com/in/jane/")

    assert parsed["section_status"]["featured"] == "drift"
    assert parsed["drift"] == [{
        "section": "featured",
        "contract": "featured item anchors",
        "expected": "at least one featured item anchor",
        "found": "0 items while the DOM contained an activity URN",
    }]


def test_fixture_extracts_the_subject_name(parsed):
    # Catches a semantic profile-extraction regression rather than merely a
    # shape or serialization failure.
    assert parsed["profile"]["name"] == "Afreen Munshi, PMP®"


def test_fixture_extracts_a_skill(parsed):
    # Catches section routing/extraction regressions with a stable value from
    # the committed, reviewed fixture.
    assert parsed["profile"]["skills"][0]["name"] == "Machine Learning"


def test_interests_are_grouped_under_category_envelopes():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"
    top_voices = """
    <html><body><main>
      <div role="radio" aria-checked="true">Top voices</div>
      <a href="/in/kunalshah1/">
        <span>Kunal Shah, Person</span><span>Kunal Shah</span>
        <span>Head - WhatsApp</span><span>1,406,322 followers</span>
      </a>
    </main></body></html>
    """
    companies = """
    <html><body><main>
      <div role="radio" aria-checked="true">Companies</div>
      <a href="/company/onit-india/">
        <span>Onit India, Company</span><span>Onit India</span>
        <span>98,461 followers</span>
      </a>
    </main></body></html>
    """

    parsed = parse_profile_html(
        profile, {"interests": top_voices, "interests-companies": companies},
        profile_url="https://www.linkedin.com/in/jane/")

    assert parsed["sources"]["interests"]["interest_category"] == "top_voices"
    assert (
        parsed["sources"]["interests-companies"]["interest_category"]
        == "companies"
    )
    assert parsed["profile"]["interests"] == {
        "captured_categories": ["top_voices", "companies"],
        "categories": {
            "top_voices": {
                "status": "parsed",
                "source_url": "https://www.linkedin.com/in/jane/",
                "entries": [{
                    "name": "Kunal Shah",
                    "url": "https://www.linkedin.com/in/kunalshah1/",
                    "headline": "Head - WhatsApp",
                    "followers": 1406322,
                }],
            },
            "companies": {
                "status": "parsed",
                "source_url": "https://www.linkedin.com/in/jane/",
                "entries": [{
                    "name": "Onit India",
                    "url": "https://www.linkedin.com/company/onit-india/",
                    "followers": 98461,
                }],
            },
        },
    }


def test_only_unparsed_sources_keep_a_duplicate_content_snapshot(parsed):
    # Parsed content is represented structurally and its raw HTML remains in
    # the capture artifact. Fallback/error content keeps the JSON snapshot as
    # the completeness backstop.
    for name, source in parsed["sources"].items():
        status = parsed["section_status"].get(name)
        if name == "profile" or status in {"parsed", "parsed_inline"}:
            assert "content" not in source, name
        else:
            assert source.get("content") is not None, name


def test_the_document_is_json_serialisable(parsed):
    # It is stored as an object in MinIO, so anything that cannot be serialised
    # is a failure at write time rather than a surprise in production.
    json.dumps(parsed)


def test_a_profile_with_no_sections_still_parses():
    profile = "<html><body><main><h1>Jane Doe</h1></main></body></html>"
    result = parse_profile_html(profile, {},
                                profile_url="https://www.linkedin.com/in/x/")
    assert result["profile"]["experiences"] == []
    assert set(result["section_status"].values()) == {"not_captured"}


def test_unparseable_html_is_recorded_rather_than_raised():
    profile, _ = _load(_handles()[0])
    result = parse_profile_html(
        profile, {"experience": "\x00 not html at all"},
        profile_url="https://www.linkedin.com/in/x/")
    assert result["section_status"]["experience"] in ("error", "fallback")
    # A failure in one section never discards what other sections yielded.
    assert result["profile"]["name"] is not None


PROFILE_HTML = """
<html><body><main><section aria-label="Primary content">
  <div><h2><a href="/in/ada/">Ada Lovelace</a></h2>
  <p>Analytical engine programmer</p><p>London</p></div>
</section></main></body></html>
"""


def test_skills_preserve_each_detail_and_its_link():
    skills = """
    <main><section aria-label="Primary content">
      <div data-testid="profile_SkillDetails_ada">
        <div><h2>Skills</h2></div>
        <div><p>Mathematics</p>
          <a href="/in/ada/details/skills/math/endorsers/">Endorsed by 25 colleagues</a>
          <a href="/in/ada/details/skills/math/endorsers/">99+ endorsements</a>
        </div>
      </div>
    </section></main>
    """

    result = parse_profile_html(
        PROFILE_HTML, {"skills": skills},
        profile_url="https://www.linkedin.com/in/ada/")

    assert result["profile"]["skills"] == [{
        "name": "Mathematics",
        "details": [
            {"text": "Endorsed by 25 colleagues", "url":
             "https://www.linkedin.com/in/ada/details/skills/math/endorsers/"},
            {"text": "99+ endorsements", "url":
             "https://www.linkedin.com/in/ada/details/skills/math/endorsers/"},
        ],
    }]


def test_inline_education_is_split_into_school_rows():
    profile = """
    <html><body><main><section aria-label="Primary content">
      <div><h2>Education</h2></div><div><div>
        <div><a href="/school/1/"><p>University One</p>
          <p>MBA, Finance</p><p>2014 – 2016</p></a></div>
        <div><a href="/school/2/"><p>University Two</p>
          <p>B.E., Computer Science</p><p>2007 – 2011</p></a>
          <p>Grade: First Class</p></div>
      </div></div>
    </section></main></body></html>
    """

    result = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/ada/")

    assert result["section_status"]["education"] == "parsed_inline"
    assert [(row["school"], row["degree"], row["field_of_study"])
            for row in result["profile"]["education"]] == [
        ("University One", "MBA", "Finance"),
        ("University Two", "B.E.", "Computer Science"),
    ]
    assert [
        (row["started_on"], row["ended_on"], row["is_current"])
        for row in result["profile"]["education"]
    ] == [("2014", "2016", False), ("2007", "2011", False)]


def test_experience_emits_parser_owned_date_metadata():
    profile = """
    <html><body><main><section aria-label="Primary content">
      <div><h2>Experience</h2></div><div>
        <div><a href="/company/1/"><p>NVIDIA</p></a>
          <p>Founder and CEO</p><p>1993 – Present</p></div>
      </div>
    </section></main></body></html>
    """

    result = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/ada/")

    role = result["profile"]["experiences"][0]
    assert (role["started_on"], role["ended_on"], role["is_current"]) == (
        "1993", None, True)


def test_recommendations_preserve_party_relationship_and_direction():
    def recommendation(name: str, detail: str) -> str:
        return f"""
        <main><section aria-label="Primary content">
          <div data-testid="profile_RecommendationDetailsSection_ada">
            <div><h2>Recommendations</h2></div><div>
              <a href="/in/person/">{name}</a><p>· 3rd+</p>
              <p>Inventor at Engine Works</p><p>{detail}</p>
              <p>A remarkable collaborator.</p>
            </div>
          </div>
        </section></main>
        """

    result = parse_profile_html(
        PROFILE_HTML,
        {
            "recommendations": recommendation(
                "Charles Babbage", "April 10, 1843, Charles was Ada’s client"),
            "recommendations-given": recommendation(
                "Mary Somerville", "May 2, 1843, Ada worked with Mary"),
        },
        profile_url="https://www.linkedin.com/in/ada/")

    received = result["profile"]["recommendations"]["received"][0]
    given = result["profile"]["recommendations"]["given"][0]
    assert received["from"]["name"] == "Charles Babbage"
    assert received["date"] == "April 10, 1843"
    assert received["relationship"] == "Charles was Ada’s client"
    assert given["to"]["name"] == "Mary Somerville"


def test_reaction_capture_is_routed_to_its_own_bucket():
    reactions = """
    <main><section aria-label="Primary content">
      <div><p>Ada Lovelace</p><p>Reacted to an analytical engine note.</p></div>
    </section></main>
    """

    result = parse_profile_html(
        PROFILE_HTML, {"activity-reactions": reactions},
        profile_url="https://www.linkedin.com/in/ada/")

    assert result["section_status"]["activity-reactions"] == "fallback"
    assert result["profile"]["activity"]["reactions"]["items"] == []
    assert "Reacted to an analytical engine note" in (
        result["profile"]["additional_sections"]["activity-reactions"]["text"])
