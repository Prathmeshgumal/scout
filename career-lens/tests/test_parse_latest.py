"""Structured JSON extraction from captured LinkedIn profile HTML."""

from __future__ import annotations

import pytest

from career_lens.linkedin.parse.dom import content_snapshot, parse_document
from career_lens.linkedin.parse.parser import parse_profile_html


PROFILE_URL = "https://www.linkedin.com/in/ada/"
EMPTY_ACTIVITY = {
    "source_url": None,
    "loaded": None,
    "captured": 0,
    "complete": None,
    "items": [],
}

# One activity card as LinkedIn builds it: a repost, whose actor block writes
# every label twice (once for the screen, once for a screen reader), whose
# commentary is clamped behind a "…more" toggle, and which quotes another post
# complete with its own actor. Trimmed to the components the parser reads.
FEED_CARD_HTML = """
<!doctype html>
<html><body><main><section aria-label="Primary content">
  <p>Loaded 3 Posts posts</p>
  <div class="feed-shared-update-v2" role="article"
       data-urn="urn:li:activity:7492325888571658240">
    <div class="feed-shared-update-v2__control-menu-container">
      <span class="visually-hidden">Feed post number 1</span>
    </div>
    <div class="update-components-header">
      <span class="update-components-header__text-view">
        <span aria-hidden="true">Ada Lovelace reposted this</span>
        <span class="visually-hidden">Ada Lovelace reposted this</span>
      </span>
    </div>
    <div class="update-components-actor__container">
      <div class="update-components-actor__avatar">
        <img class="update-components-actor__avatar-image" src="https://media.example/ada.jpg"/>
      </div>
      <a class="update-components-actor__meta-link" href="/in/charles-babbage?miniProfileUrn=urn%3Ali%3Afsd_profile%3AXYZ">
        <span class="update-components-actor__title">
          <span class="update-components-actor__single-line-truncate">
            <span aria-hidden="true">Charles Babbage</span
            ><span class="visually-hidden">Charles Babbage</span>
          </span>
          <span class="update-components-actor__supplementary-actor-info">
            <span aria-hidden="true">
              <svg data-test-icon="verified-small"></svg> &#8226; 2nd</span
            ><span class="visually-hidden">Verified &#8226; 2nd</span>
          </span>
        </span>
        <span class="update-components-actor__description">
          <span aria-hidden="true">Inventor of the difference engine</span
          ><span class="visually-hidden">Inventor of the difference engine</span>
        </span>
        <span class="update-components-actor__sub-description-button-text">Book an appointment</span>
        <span class="update-components-actor__sub-description">
          <span aria-hidden="true">2d &#8226; Edited &#8226;</span
          ><span class="visually-hidden">2 days ago &#8226; Edited &#8226; Visible to anyone on or off LinkedIn</span>
        </span>
      </a>
      <button class="update-components-actor__follow-button">Follow</button>
    </div>
    <div class="feed-shared-inline-show-more-text">
      <div class="update-components-text">
        <span>A milestone for <a href="/company/engines/">Analytical Engines</a>.<br/><a
          href="https://www.linkedin.com/search/results/all/?keywords=%23engines"
          ><span class="visually-hidden">hashtag</span><span>#engines</span></a></span>
      </div>
      <button class="feed-shared-inline-show-more-text__see-more-less-toggle">…more</button>
    </div>
    <div class="feed-shared-update-v2__update-content-wrapper">
      <div class="update-components-actor__container">
        <a class="update-components-actor__meta-link" href="/company/engines/">
          <span class="update-components-actor__title">
            <span aria-hidden="true">Analytical Engines</span
            ><span class="visually-hidden">Analytical Engines</span>
          </span>
          <span class="update-components-actor__description">
            <span aria-hidden="true">1,842 followers</span
            ><span class="visually-hidden">1,842 followers</span>
          </span>
          <span class="update-components-actor__sub-description">
            <span aria-hidden="true">3w &#8226;</span>
          </span>
        </a>
      </div>
      <div class="update-components-text"><span>The engine is finished.</span></div>
      <div class="update-components-image">
        <img src="https://media.example/engine.png" alt="The engine"/>
        <span class="update-components-image__excess-image-count-text">+2</span>
      </div>
    </div>
    <div class="social-details-social-counts">
      <span class="social-details-social-counts__item">
        <span class="social-details-social-counts__reactions-count">59</span>
        <img class="reactions-icon data-test-reactions-icon-type-LIKE" src="https://static.example/like.svg"/>
      </span>
      <span class="social-details-social-counts__item">
        <span class="social-details-social-counts__count-value">3 comments</span>
      </span>
    </div>
    <div class="comments-comments-list">
      <article class="comments-comment-entity">
        <div class="comments-comment-meta__container">
          <a class="comments-comment-meta__image-link" href="/in/mary-somerville/"></a>
          <span class="comments-comment-meta__description-title">Mary Somerville</span>
          <span class="comments-comment-meta__description-subtitle">Scientist</span>
          <span class="comments-comment-meta__info">1d</span>
        </div>
        <div class="comments-comment-item__main-content"><span>Congratulations!</span></div>
        <span class="comments-comment-social-bar__reactions-count--cr">4</span>
        <article class="comments-comment-entity comments-comment-entity--reply">
          <div class="comments-comment-meta__container">
            <a class="comments-comment-meta__image-link" href="/in/ada/"></a>
            <span class="comments-comment-meta__description-title">Ada Lovelace</span>
            <span class="comments-comment-meta__badge">Author</span>
            <span class="comments-comment-meta__info">1d</span>
          </div>
          <div class="comments-comment-item__main-content"><span>Thank you Mary</span></div>
        </article>
      </article>
    </div>
  </div>
</section></main></body></html>
"""

# The Articles tab, which is a list and not a feed: no urn, no actor block and
# no social counts, because the tab renders none of them. The preview sits under
# an obfuscated class between the title and the byline.
ARTICLE_LIST_HTML = """
<!doctype html>
<html><body><main><section aria-label="Primary content">
  <p>Loaded 2 Articles posts</p>
  <ul>
    <li class="profile-creator-shared-article__container"><div>
      <a href="https://www.linkedin.com/pulse/analytical-engine-notes-ada-x7f2c?trackingId=Y3Lp%3D%3D">
        <div class="profile-creator-shared-aspect-ratio__wrapper">
          <img class="profile-creator-shared-article__image" src="https://media.example/cover.png"/>
        </div>
        <div class="profile-creator-shared-article__title"><span>Notes on the Analytical Engine</span></div>
        <div class="qXbYzZ inline-show-more-text--is-collapsed mb1" aria-hidden="true"
          ><span>The engine weaves algebraic patterns as the loom weaves flowers and lea</span></div>
        <p class="text-body-small t-black--light">by Ada Lovelace &#8226; 7 min read</p>
      </a>
    </div></li>
    <li class="profile-creator-shared-article__container"><div>
      <a href="https://www.linkedin.com/pulse/on-bernoulli-numbers-ada">
        <div class="profile-creator-shared-article__title"><span>On Bernoulli Numbers</span></div>
        <p class="text-body-small t-black--light">by Ada Lovelace &#8226; 11 min read</p>
      </a>
    </div></li>
  </ul>
</section></main></body></html>
"""

# What LinkedIn renders in a tab the person has nothing in.
EMPTY_TAB_HTML = """
<!doctype html>
<html><body><main><section aria-label="Primary content">
  <h2>Nothing to see for now</h2>
  <p>Articles, posts, documents, images, videos, or events that Ada adds will appear here.</p>
</section></main></body></html>
"""

# Interests, from LinkedIn's newer application: every class is a build hash, so
# entries are found by the address they point at. The category strip names the
# tab, each entry writes its name twice, the profile's own link opens the page,
# and a newsletter's publisher is an anchor inside the newsletter's own anchor.
INTERESTS_HTML = """
<!doctype html>
<html><body><main><section aria-label="Primary content">
  <div role="radiogroup">
    <div role="radio" aria-checked="false">Top Voices</div>
    <div role="radio" aria-checked="true">Newsletters</div>
    <div role="radio" aria-checked="false">Groups</div>
  </div>
  <a class="_36d0853c" href="https://www.linkedin.com/in/ada/">Ada Lovelace</a>
  <ul>
    <li><a class="d717982c" href="https://www.linkedin.com/newsletters/7183890373289648128/">
      <div>
        <figure><img src="https://media.example/spark.png"/></figure>
        <span>Spark, Newsletter</span><span>Spark</span>
        <span>Notes on engines and the people who build them</span>
        <span>Published monthly</span>
        <div><a href="https://www.linkedin.com/company/engines/">Analytical Engines</a></div>
      </div>
    </a></li>
    <li><a class="d717982c" href="https://www.linkedin.com/newsletters/7183890373289648129/">
      <div><span>Looming, Newsletter</span><span>Looming</span>
      <span>Published weekly</span></div>
    </a></li>
  </ul>
</section></main></body></html>
"""

INTERESTS_GROUPS_HTML = """
<!doctype html>
<html><body><main><section aria-label="Primary content">
  <div role="radiogroup">
    <div role="radio" aria-checked="false">Newsletters</div>
    <div role="radio" aria-checked="true">Groups</div>
  </div>
  <a href="https://www.linkedin.com/in/ada/">Ada Lovelace</a>
  <ul><li><a href="https://www.linkedin.com/groups/3983267/">
    <span>Engine Builders, Group</span><span>Engine Builders</span>
    <span>1,080,143 members</span>
  </a></li></ul>
</section></main></body></html>
"""

PROFILE_HTML = """
<!doctype html>
<html>
  <body>
    <a href="#main">Skip to main content</a>
    <nav>Home My Network Jobs</nav>
    <main id="main">
      <section aria-label="Primary content">
        <h1>Ada Lovelace</h1>
        <p>Analytical engine programmer</p>
        <p>First paragraph.</p>
        <p>Second paragraph nobody classified.</p>
        <a href="/company/engines/">Analytical Engines</a>
        <a href="/company/engines/">Analytical Engines</a>
        <section aria-label="More profiles for you">
          <p>Unrelated Person</p>
        </section>
        <div data-ad-banner>Promoted vacancy</div>
      </section>
    </main>
    <footer>About Accessibility Privacy</footer>
  </body>
</html>
"""

STRUCTURED_PROFILE_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="top-card">
    <h2><a href="/in/ada/">Ada Lovelace</a></h2>
    <p>Analytical engine programmer</p>
    <p>London, England, United Kingdom</p>
    <p>500+ connections</p>
  </div>
  <section>
    <h2>About</h2>
    <p>Wrote the first published algorithm.</p>
    <p>Studied the limits of computing.</p>
  </section>
  <section><h2>Activity</h2><p>1,842 followers</p></section>
</section></main></body></html>
"""

EXPERIENCE_HTML = """
<main><section aria-label="Primary content">
  <div data-testid="profile_ExperienceDetailsSection_ada">
    <div><h2>Experience</h2></div>
    <div>
      <p>Programmer</p>
      <p><a href="/company/162479/">Analytical Engines Society</a> · Contract</p>
      <p>1842 - 1843 · 1 yr</p>
      <p>London, United Kingdom · Hybrid</p>
      <p>Published the first algorithm.</p>
      <p>Skills: Mathematics, Technical Writing</p>
      <p>Unclassified award context.</p>
    </div>
    <div>
      <p>Translator</p><p>Scientific Review</p><p>1841</p>
    </div>
  </div>
</section></main>
"""

GROUPED_EXPERIENCE_HTML = """
<main><section aria-label="Primary content">
  <div data-testid="profile_ExperienceDetailsSection_ada">
    <div><p>Experience</p></div>
    <div>
      <div>
        <p><a href="/company/1441/">Google</a></p>
        <p>4 yrs 3 mos</p>
      </div>
      <ul>
        <li>
          <p>Startup Success Manager</p>
          <p>Part-time</p>
          <p>Jun 2026 - Present · 3 mos</p>
          <p>Hybrid</p>
          <p>Helping startups build.</p>
        </li>
        <li>
          <p>Senior, Technical Solutions Consultant AI-ML</p>
          <p>Full-time</p>
          <p>Oct 2024 - Present · 1 yr 11 mos</p>
          <p>Pune District, Maharashtra, India</p>
        </li>
        <li>
          <p>Technical Solutions Consultant AI - ML</p>
          <p>Full-time</p>
          <p>Jun 2022 - Oct 2024 · 2 yrs 5 mos</p>
          <p>Pune District, Maharashtra, India</p>
        </li>
      </ul>
    </div>
  </div>
</section></main>
"""

EDUCATION_HTML = """
<main><section aria-label="Primary content">
  <div data-testid="profile_EducationDetailsSection_ada">
    <div><h2>Education</h2></div>
    <div>
      <p><a href="/school/london/">University of London</a></p>
      <p>Independent studies, Mathematics</p>
      <p>1830 - 1835</p>
      <p>Grade: Distinction</p>
      <p>Activities and societies: Scientific salons</p>
      <p>Skills: Mathematics, Logic</p>
    </div>
  </div>
</section></main>
"""


def simple_section_html(
    test_id: str,
    heading: str,
    *entries: tuple[str, ...],
    link: tuple[str, str] | None = None,
) -> str:
    anchor = f'<a href="{link[1]}">{link[0]}</a>' if link else ""
    cards = "".join(
        "<div>" + "".join(f"<p>{line}</p>" for line in entry) + anchor + "</div>"
        for entry in entries
    )
    return (
        '<main><section aria-label="Primary content">'
        f'<div data-testid="{test_id}"><div><h2>{heading}</h2></div>{cards}</div>'
        "</section></main>"
    )


def recommendation_section_html(*entries: tuple[str, ...]) -> str:
    cards = "".join(
        '<div><a href="/in/person-' + str(index) + '/">' + entry[0] + "</a>"
        + "".join(f"<p>{line}</p>" for line in entry[1:])
        + "</div>"
        for index, entry in enumerate(entries, 1)
    )
    return (
        '<main><section aria-label="Primary content">'
        '<div data-testid="profile_RecommendationDetailsSection_ada">'
        "<div><h2>Recommendations</h2></div><div><p>Received</p><p>Given</p></div>"
        f"{cards}</div></section></main>"
    )


INLINE_RECOMMENDATIONS_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="top-card">
    <h2><a href="/in/tushar-kapratwar/">Tushar Kapratwar</a></h2>
    <p>Product Manufacturing Operations | NPI Program Management</p>
    <p>Pune, Maharashtra, India</p>
    <p>500+ connections</p>
  </div>
  <section>
    <div><h2>Recommendations</h2></div>
    <div><p>Received (2)</p><p>Given (1)</p></div>
    <div>
      <a href="/in/james-tsan/">James Tsan</a>
      <p>· 3rd</p>
      <p>NPI Product Engineer</p>
      <p>March 8, 2024, James was Tushar’s client</p>
      <p>Having worked closely with Tushar, I've seen firsthand his exceptional work ethic.</p>
    </div>
    <div>
      <a href="/in/harshala-patel/">Harshala Patel, PMI-PMP, PSM</a>
      <p>· 3rd</p>
      <p>Senior Technical Program Manager at Nvidia</p>
      <p>March 2, 2024, Harshala managed Tushar directly</p>
      <p>I had the pleasure of directly managing Tushar for 1.5 years.</p>
    </div>
  </section>
  <section><h2>Publications</h2><p>Layout Improvement at a Printing Press</p></section>
</section></main></body></html>
"""


def test_content_snapshot_preserves_primary_content_and_excludes_chrome():
    """Dropping prose, retaining chrome, or duplicating links is content loss."""
    document = parse_document(PROFILE_HTML)

    snapshot = content_snapshot(document, PROFILE_URL)

    assert snapshot["text"] == (
        "Ada Lovelace\n\nAnalytical engine programmer\n\nFirst paragraph.\n\n"
        "Second paragraph nobody classified.\n\nAnalytical Engines\n\nAnalytical Engines"
    )
    assert snapshot["links"] == [
        {
            "text": "Analytical Engines",
            "url": "https://www.linkedin.com/company/engines/",
        }
    ]


def test_parse_document_accepts_bytes_and_malformed_html():
    """A partial browser capture must remain parseable instead of losing a profile."""
    document = parse_document(b"<main><section><h1>Ada<p>Profile notes")

    assert content_snapshot(document, PROFILE_URL)["text"] == "Ada\n\nProfile notes"


def test_parse_profile_html_extracts_identity_and_about():
    """Losing top-card or About fields would make semantic JSON unusable."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {},
        profile_url=PROFILE_URL,
    )

    expected = {
        "url": PROFILE_URL,
        "handle": "ada",
        "name": "Ada Lovelace",
        "headline": "Analytical engine programmer",
        "location": "London, England, United Kingdom",
        "about": "Wrote the first published algorithm.\n\nStudied the limits of computing.",
        "connections": 500,
        "followers": 1842,
    }
    assert {key: result["profile"][key] for key in expected} == expected


def test_parse_profile_html_has_stable_skeleton_and_content_snapshot():
    """Consumers must distinguish uncaptured sections and retain unclassified prose."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {},
        profile_url=PROFILE_URL,
    )

    assert result["schema_version"] == "2.2"
    assert result["profile"]["experiences"] == []
    assert result["profile"]["education"] == []
    assert result["profile"]["recommendations"] == {"received": [], "given": []}
    assert result["profile"]["activity"] == {
        "posts": EMPTY_ACTIVITY,
        "comments": EMPTY_ACTIVITY,
        "articles": EMPTY_ACTIVITY,
        "reactions": EMPTY_ACTIVITY,
    }
    assert result["section_status"]["experience"] == "not_captured"
    assert result["section_status"]["certifications"] == "not_captured"
    # The page's own text is no longer copied beside the fields read from it.
    assert result["sources"]["profile"] == {"artifact": None, "url": PROFILE_URL}
    assert result["profile"]["name"] == "Ada Lovelace"
    assert result["profile"]["about"] == (
        "Wrote the first published algorithm.\n\nStudied the limits of computing."
    )
    assert result["warnings"] == []


def test_experience_entries_are_nested_and_retain_unclassified_text():
    """Flattening a role or dropping an unknown line must break the profile contract."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"experience": EXPERIENCE_HTML},
        profile_url=PROFILE_URL,
        section_urls={"experience": f"{PROFILE_URL}details/experience/"},
    )

    assert result["section_status"]["experience"] == "parsed"
    assert len(result["profile"]["experiences"]) == 2
    assert result["profile"]["experiences"][0] == {
        "name": "Analytical Engines Society",
        "title": "Programmer",
        "date_range": "1842 - 1843",
        "duration": "1 yr",
        "started_on": "1842",
        "ended_on": "1843",
        "is_current": False,
        "location": "London, United Kingdom · Hybrid",
        "employment_type": "Contract",
        "description": "Published the first algorithm.\n\nUnclassified award context.",
        "skills": ["Mathematics", "Technical Writing"],
        "url": "https://www.linkedin.com/company/162479/",
        "company_id": "162479",
        "total_duration": None,
    }
    assert result["profile"]["experiences"][1]["company_id"] is None
    # Experience parsed, so its snapshot is dropped rather than kept as a
    # second, flatter copy. The capture itself is still named by `artifact`.
    assert "content" not in result["sources"]["experience"]
    assert set(result["sources"]["experience"]) == {"artifact", "url"}


def test_grouped_experience_preserves_role_boundaries_and_repeated_values():
    """Flattening company groups or deduplicating equal role facts corrupts experience data."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"experience": GROUPED_EXPERIENCE_HTML},
        profile_url=PROFILE_URL,
        section_urls={"experience": f"{PROFILE_URL}details/experience/"},
    )

    experiences = result["profile"]["experiences"]
    assert len(experiences) == 1
    assert experiences[0]["name"] == "Google"
    assert experiences[0]["company_id"] == "1441"
    assert experiences[0]["employment_type"] is None
    assert experiences[0]["total_duration"] == "4 yrs 3 mos"
    assert [role["title"] for role in experiences[0]["roles"]] == [
        "Startup Success Manager",
        "Senior, Technical Solutions Consultant AI-ML",
        "Technical Solutions Consultant AI - ML",
    ]
    assert [role["employment_type"] for role in experiences[0]["roles"]] == [
        "Part-time",
        "Full-time",
        "Full-time",
    ]
    assert [role["location"] for role in experiences[0]["roles"]] == [
        "Hybrid",
        "Pune District, Maharashtra, India",
        "Pune District, Maharashtra, India",
    ]
    assert experiences[0]["roles"][0]["date_range"] == "Jun 2026 - Present"
    assert experiences[0]["roles"][0]["duration"] == "3 mos"
    assert experiences[0]["roles"][0]["started_on"] == "2026-06"
    assert experiences[0]["roles"][0]["ended_on"] is None
    assert experiences[0]["roles"][0]["is_current"] is True
    assert experiences[0]["roles"][2]["started_on"] == "2022-06"
    assert experiences[0]["roles"][2]["ended_on"] == "2024-10"
    assert experiences[0]["roles"][2]["is_current"] is False
    assert experiences[0]["roles"][0]["description"] == "Helping startups build."
    assert all(role["title"] != "Experience" for role in experiences[0]["roles"])


def test_grouped_experience_states_no_company_level_date_range():
    """A company's tenure is `total_duration`; borrowing the first role's dates understates it.

    Google here spans 4 yrs 3 mos, while the newest role began 3 months ago.
    Copying that role's range up to the company would claim the whole tenure
    was 3 months. The roles carry the exact dates, so the company entry states
    only what LinkedIn renders for it.
    """
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"experience": GROUPED_EXPERIENCE_HTML},
        profile_url=PROFILE_URL,
        section_urls={"experience": f"{PROFILE_URL}details/experience/"},
    )

    entry = result["profile"]["experiences"][0]
    assert entry["total_duration"] == "4 yrs 3 mos"
    assert "date_range" not in entry
    assert "duration" not in entry


def test_education_entries_have_semantic_fields():
    """School details must remain attached to the education entry they describe."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"education": EDUCATION_HTML},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["education"] == "parsed"
    expected = {
        "school": "University of London",
        "degree": "Independent studies",
        "field_of_study": "Mathematics",
        "date_range": "1830 - 1835",
        "started_on": "1830",
        "ended_on": "1835",
        "is_current": False,
        "grade": "Distinction",
        "activities": "Scientific salons",
        "skills": ["Mathematics", "Logic"],
        "url": "https://www.linkedin.com/school/london/",
    }
    assert {
        key: result["profile"]["education"][0][key] for key in expected
    } == expected


def test_inline_education_splits_rows_wrapped_in_one_container():
    """Inline education may wrap several school rows in one div; keep them separate."""
    html = """
    <html><body><main><section aria-label="Primary content">
      <div><h2>Education</h2></div>
      <div>
        <div>
          <div>
            <a href="/school/417042/">
              <p>SVKM's Narsee Monjee Institute of Management Studies (NMIMS)</p>
              <p>Master of Business Administration (MBA)</p>
              <p>2014 – 2016</p>
            </a>
          </div>
          <div>
            <a href="/school/15094398/">
              <p>Savitribai Phule Pune University</p>
              <p>B.E., Computer Science</p>
              <p>2007 – 2011</p>
            </a>
            <p>Grade: First Class</p>
          </div>
        </div>
      </div>
    </section></main></body></html>
    """

    result = parse_profile_html(
        html,
        {},
        profile_url="https://www.linkedin.com/in/aditya-rane/",
    )

    assert result["section_status"]["education"] == "parsed_inline"
    assert [
        {
            "school": item["school"],
            "degree": item["degree"],
            "field_of_study": item["field_of_study"],
            "date_range": item["date_range"],
            "started_on": item["started_on"],
            "ended_on": item["ended_on"],
            "is_current": item["is_current"],
            "grade": item["grade"],
            "url": item["url"],
            "description": item["description"],
        }
        for item in result["profile"]["education"]
    ] == [
        {
            "school": "SVKM's Narsee Monjee Institute of Management Studies (NMIMS)",
            "degree": "Master of Business Administration (MBA)",
            "field_of_study": None,
            "date_range": "2014 – 2016",
            "started_on": "2014",
            "ended_on": "2016",
            "is_current": False,
            "grade": None,
            "url": "https://www.linkedin.com/school/417042/",
            "description": None,
        },
        {
            "school": "Savitribai Phule Pune University",
            "degree": "B.E.",
            "field_of_study": "Computer Science",
            "date_range": "2007 – 2011",
            "started_on": "2007",
            "ended_on": "2011",
            "is_current": False,
            "grade": "First Class",
            "url": "https://www.linkedin.com/school/15094398/",
            "description": None,
        },
    ]


def test_inline_education_splits_school_rows_without_dates():
    """School rows without date ranges are still separate education entries."""
    html = """
    <html><body><main><section aria-label="Primary content">
      <div><h2>Education</h2></div>
      <div>
        <div>
          <div>
            <a href="/school/15112156/">
              <p>BUNDELKHAND INSTITUTE OF ENGINEERING AND TECHNOLOGY, JHANSI</p>
              <p>Bachelor of Technology - BTech, Computer Science and Engineering</p>
              <p>2018 – 2022</p>
            </a>
            <p>Grade: 8.1</p>
          </div>
          <div>
            <a href="/school/7808340/">
              <p>Delhi Public School - India</p>
              <p>Computer</p>
            </a>
            <p>Grade: A</p>
            <p>Activities and societies: Badminton</p>
            <p>Participated in various debates, creative writing and art competitions.</p>
            <p>Volunteered in various events of Non-Profit Organisation Seva Sankalp</p>
          </div>
        </div>
      </div>
    </section></main></body></html>
    """

    result = parse_profile_html(
        html,
        {},
        profile_url="https://www.linkedin.com/in/deekshapandey16/",
    )

    assert result["section_status"]["education"] == "parsed_inline"
    assert [
        {
            "school": item["school"],
            "degree": item["degree"],
            "field_of_study": item["field_of_study"],
            "date_range": item["date_range"],
            "started_on": item["started_on"],
            "ended_on": item["ended_on"],
            "is_current": item["is_current"],
            "grade": item["grade"],
            "activities": item["activities"],
            "description": item["description"],
            "url": item["url"],
        }
        for item in result["profile"]["education"]
    ] == [
        {
            "school": "BUNDELKHAND INSTITUTE OF ENGINEERING AND TECHNOLOGY, JHANSI",
            "degree": "Bachelor of Technology - BTech",
            "field_of_study": "Computer Science and Engineering",
            "date_range": "2018 – 2022",
            "started_on": "2018",
            "ended_on": "2022",
            "is_current": False,
            "grade": "8.1",
            "activities": None,
            "description": None,
            "url": "https://www.linkedin.com/school/15112156/",
        },
        {
            "school": "Delhi Public School - India",
            "degree": "Computer",
            "field_of_study": None,
            "date_range": None,
            "started_on": None,
            "ended_on": None,
            "is_current": False,
            "grade": "A",
            "activities": "Badminton",
            "description": (
                "Participated in various debates, creative writing and art competitions.\n\n"
                "Volunteered in various events of Non-Profit Organisation Seva Sankalp"
            ),
            "url": "https://www.linkedin.com/school/7808340/",
        },
    ]


def test_honors_name_their_issuer_and_date():
    """"Issued by IDDBA · Jun 2022" is an awarding body and a date, not a line of prose."""
    section = simple_section_html(
        "profile_HonorDetailsSection_ada", "Honors & awards",
        ("Computing Medal", "Issued by Royal Society · Jun 2022",
         "Associated with Analytical Engines", "For the first published algorithm."),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"honors": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["honors"] == "parsed"
    assert result["profile"]["honors"] == [{
        "name": "Computing Medal",
        "issuer": "Royal Society",
        "issued_label": "Jun 2022",
        "issued_on": "2022-06",
        "associated_with": "Analytical Engines",
        "description": "For the first published algorithm.",
    }]


def test_an_honor_without_an_issuer_keeps_its_date():
    """Some awards name a date and no body; the date must not be read as prose."""
    section = simple_section_html(
        "profile_HonorDetailsSection_ada", "Honors & awards",
        ("Customer Obsession Spotlight", "Feb 2024", "Associated with Intuit"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"honors": section}, profile_url=PROFILE_URL
    )

    assert result["profile"]["honors"] == [{
        "name": "Customer Obsession Spotlight",
        "issued_label": "Feb 2024",
        "issued_on": "2024-02",
        "associated_with": "Intuit",
    }]


def test_organizations_split_the_role_from_the_years_it_was_held():
    """"Board Member · Feb 2022 – Present" is a role and a span sharing one line."""
    section = simple_section_html(
        "profile_OrganizationDetailsSection_ada", "Organizations",
        ("Royal Society", "Board Member · Feb 2022 – Present",
         "https://www.royalsociety.org", "Advancing the sciences."),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"organizations": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["organizations"] == "parsed"
    assert result["profile"]["organizations"] == [{
        "name": "Royal Society",
        "role": "Board Member",
        "date_range": "Feb 2022 – Present",
        "started_on": "2022-02",
        "ended_on": None,
        "is_current": True,
        "url": "https://www.royalsociety.org",
        "description": "Advancing the sciences.",
    }]


def test_certifications_name_their_issuer_dates_and_credential():
    """"Issued Apr 2021" and "Credential ID …" are facts, not lines of a blob."""
    section = simple_section_html(
        "profile_CertificationDetailsSection_ada",
        "Licenses & certifications",
        ("Cloud Architecture", "Udemy", "Issued Jan 2026", "Credential ID ABC-123",
         "Skills:", "Systems Design, Networking, +2 skills"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"certifications": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["certifications"] == "parsed"
    assert result["profile"]["certifications"] == [{
        "name": "Cloud Architecture",
        "issued_label": "Issued Jan 2026",
        "issued_on": "2026-01",
        "credential_id": "ABC-123",
        "issuer": {"name": "Udemy"},
        "skills": ["Systems Design", "Networking"],
        # "+2 skills" says the list is partial; silence would imply it is not.
        "skills_more": 2,
    }]


def test_certifications_split_combined_issue_and_expiry_metadata():
    """LinkedIn also serves both labelled dates in one middle-dot text run."""
    section = simple_section_html(
        "profile_CertificationDetailsSection_ada",
        "Licenses & certifications",
        ("Cloud Architecture", "Udemy", "Issued Jan 2026 · Expires Jan 2029"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"certifications": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["certifications"] == "parsed"
    assert result["drift"] == []
    assert result["profile"]["certifications"] == [{
        "name": "Cloud Architecture",
        "issued_label": "Issued Jan 2026",
        "issued_on": "2026-01",
        "expiry_label": "Expires Jan 2029",
        "expires_on": "2029-01",
        "issuer": {"name": "Udemy"},
    }]


def test_certifications_may_omit_dates_entirely():
    """An undated credential is valid, not evidence that a date was lost."""
    section = simple_section_html(
        "profile_CertificationDetailsSection_ada",
        "Licenses & certifications",
        ("Cloud One", "Org One"),
        ("Cloud Two", "Org Two"),
        ("Cloud Three", "Org Three"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"certifications": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["certifications"] == "parsed"
    assert result["drift"] == []
    assert all("issued_on" not in row for row in result["profile"]["certifications"])


def test_courses_separate_their_number_from_their_name():
    """"ENMGT 5930" is a course code and "Associated with X" names a school."""
    section = simple_section_html(
        "profile_CourseDetailsSection_ada",
        "Courses",
        ("Calculus", "MATH 101", "Associated with University of London"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"courses": section}, profile_url=PROFILE_URL
    )

    assert result["profile"]["courses"] == [
        {"name": "Calculus", "number": "MATH 101", "associated_with": "University of London"}
    ]


def test_courses_may_omit_an_association_entirely():
    """LinkedIn permits standalone course names with no school relationship."""
    section = simple_section_html(
        "profile_CourseDetailsSection_ada",
        "Courses",
        ("Business Intelligence",),
        ("Database Management Systems",),
        ("Enterprise Networking",),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"courses": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["courses"] == "parsed"
    assert result["drift"] == []
    assert result["profile"]["courses"] == [
        {"name": "Business Intelligence"},
        {"name": "Database Management Systems"},
        {"name": "Enterprise Networking"},
    ]


def test_languages_keep_proficiency_only_when_it_is_stated():
    """Some profiles list a language and nothing else; a null would claim otherwise."""
    section = simple_section_html(
        "profile_LanguageDetailsSection_ada", "Languages",
        ("French", "Professional working proficiency"),
    )
    bare = simple_section_html("profile_LanguageDetailsSection_ada", "Languages", ("Marathi",))

    stated = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"languages": section}, profile_url=PROFILE_URL
    )
    unstated = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"languages": bare}, profile_url=PROFILE_URL
    )

    assert stated["profile"]["languages"] == [
        {"name": "French", "proficiency": "Professional working proficiency"}
    ]
    assert unstated["profile"]["languages"] == [{"name": "Marathi"}]


@pytest.mark.parametrize(("section", "heading", "collection"), (
    ("volunteering", "Volunteer Experience", "volunteering"),
    ("test-scores", "Test scores", "test_scores"),
    ("services", "Services", "services"),
    ("connected-apps", "Connected apps", "connected_apps"),
))
def test_new_profile_collections_keep_generic_entries(
    section: str, heading: str, collection: str,
):
    captured = simple_section_html(
        f"profile_{section}_ada", heading,
        ("First entry", "Supporting detail"),
        link=("Evidence", "https://example.com/evidence"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {section: captured}, profile_url=PROFILE_URL,
    )

    assert result["schema_version"] == "2.2"
    assert result["section_status"][section] == "parsed"
    assert result["profile"][collection] == [{
        "name": "First entry",
        "details": ["Supporting detail"],
        "text": "First entry\n\nSupporting detail\n\nEvidence",
        "links": [{"text": "Evidence", "url": "https://example.com/evidence"}],
    }]
    assert result["sources"][section]["content"] is not None


@pytest.mark.parametrize(("section", "heading", "collection"), (
    ("volunteering", "Volunteering", "volunteering"),
    ("test-scores", "Test scores", "test_scores"),
    ("services", "Services", "services"),
    ("connected-apps", "Connected apps", "connected_apps"),
))
def test_new_profile_collections_are_read_from_inline_cards(
    section: str, heading: str, collection: str,
):
    profile = STRUCTURED_PROFILE_HTML.replace(
        "</section></main>",
        f"<section><div><h2>{heading}</h2></div>"
        "<div><div><p>First entry</p><p>Supporting detail</p></div></div>"
        "</section></section></main>",
    )

    result = parse_profile_html(profile, {}, profile_url=PROFILE_URL)

    assert result["section_status"][section] == "parsed_inline"
    assert result["profile"][collection][0]["name"] == "First entry"


def test_projects_split_their_dates_and_keep_the_write_up_whole():
    """A project write-up is bulleted and sectioned, so its line breaks are content."""
    section = simple_section_html(
        "profile_ProjectDetailsSection_ada",
        "Projects",
        ("Engine notes", "Jan 2025 – Mar 2025", "Built a thing.", "Key features:", "It works."),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"projects": section}, profile_url=PROFILE_URL
    )

    assert result["profile"]["projects"] == [{
        "name": "Engine notes",
        "date_range": "Jan 2025 – Mar 2025",
        "started_on": "2025-01",
        "ended_on": "2025-03",
        "is_current": False,
        "description": "Built a thing.\nKey features:\nIt works.",
    }]


def test_a_details_section_does_not_store_its_own_heading_as_an_entry():
    """"Licenses & certifications" is the heading; storing it made every profile hold a fake entry."""
    section = simple_section_html(
        "profile_LanguageDetailsSection_ada", "Languages", ("Hindi",)
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"languages": section}, profile_url=PROFILE_URL
    )

    assert [entry["name"] for entry in result["profile"]["languages"]] == ["Hindi"]


def test_patents_split_their_office_number_and_grant_date():
    """"US 20130018674 · Issued Jan 17, 2013" is four facts on one line."""
    section = simple_section_html(
        "profile_PatentDetailsSection_ada", "Patents",
        ("Engine method", "US 20130018674 · Issued Jan 17, 2013", "Show patent",
         "A method for weaving algebraic patterns."),
        link=("Show patent",
              "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fwww%2Egoogle%2Ecom%2Fpatents%2FUS20130018674"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"patents": section}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["patents"] == "parsed"
    assert result["profile"]["patents"] == [{
        "name": "Engine method",
        "office": "US",
        "number": "20130018674",
        "status": "Issued",
        "issued_label": "Issued Jan 17, 2013",
        # These two sections date to the day, where the rest give a month.
        "issued_on": "2013-01-17",
        "description": "A method for weaving algebraic patterns.",
        # Past LinkedIn's interstitial, which carries the real address in its
        # query -- the one place a query string is the content.
        "url": "https://www.google.com/patents/US20130018674",
    }]


def test_publications_name_their_publisher_and_date():
    """A journal and a date share one line, separated by a dot."""
    section = simple_section_html(
        "profile_PublicationDetailsSection_ada", "Publications",
        ("Notes on the Engine", "Scientific Memoirs · Oct 26, 2020", "Show publication",
         "An account of the engine.", "Other authors", "+2"),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"publications": section}, profile_url=PROFILE_URL
    )

    assert result["profile"]["publications"] == [{
        "name": "Notes on the Engine",
        "publisher": "Scientific Memoirs",
        "published_label": "Oct 26, 2020",
        "published_on": "2020-10-26",
        "description": "An account of the engine.",
        # The names sit behind an overlay this parser never opens, so the count
        # is the whole of what the page states.
        "other_authors": 2,
    }]


def test_skills_embed_detail_links_without_duplicate_text_or_links():
    """Skills should mirror the card: name plus endorsement lines, no repeated blobs."""
    html = """
    <main><section aria-label="Primary content">
      <div data-testid="profile_SkillDetails_ada">
        <div><h2>Skills</h2></div>
        <div>
          <p>Grocery</p>
          <a href="/in/ada/details/skills/grocery/endorsers/">
            Endorsed by Bill Sanders and 14 others who are highly skilled at this
          </a>
          <a href="/in/ada/details/skills/grocery/endorsers/">
            Endorsed by 25 colleagues at Analytical Engines
          </a>
          <a href="/in/ada/details/skills/grocery/endorsers/">99+ endorsements</a>
        </div>
      </div>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"skills": html},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["skills"] == "parsed"
    assert result["profile"]["skills"] == [
        {
            "name": "Grocery",
            "details": [
                {
                    "text": "Endorsed by Bill Sanders and 14 others who are highly skilled at this",
                    "url": "https://www.linkedin.com/in/ada/details/skills/grocery/endorsers/",
                },
                {
                    "text": "Endorsed by 25 colleagues at Analytical Engines",
                    "url": "https://www.linkedin.com/in/ada/details/skills/grocery/endorsers/",
                },
                {
                    "text": "99+ endorsements",
                    "url": "https://www.linkedin.com/in/ada/details/skills/grocery/endorsers/",
                },
            ],
        }
    ]


def test_recommendations_are_split_between_received_and_given():
    """A recommendation written by the member must not be labeled as received."""
    received = recommendation_section_html(
        (
            "Charles Babbage",
            "· 3rd+",
            "Inventor at Difference Engine Works",
            "April 10, 1843, Charles was Ada’s client",
            "Ada explained the engine clearly.",
        ),
    )
    given = recommendation_section_html(
        (
            "Mary Somerville",
            "· 2nd",
            "Scientist",
            "May 2, 1843, Ada worked with Mary on the same team",
            "A remarkable scientific writer.",
        ),
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"recommendations": received, "recommendations-given": given},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["recommendations"] == "parsed"
    assert result["section_status"]["recommendations-given"] == "parsed"
    assert result["profile"]["recommendations"]["received"][0] == {
        "from": {
            "name": "Charles Babbage",
            "headline": "Inventor at Difference Engine Works",
            "company": "Difference Engine Works",
            "profile_url": "https://www.linkedin.com/in/person-1/",
        },
        "date": "April 10, 1843",
        "relationship": "Charles was Ada’s client",
        "text": "Ada explained the engine clearly.",
    }
    assert result["profile"]["recommendations"]["given"][0] == {
        "to": {
            "name": "Mary Somerville",
            "headline": "Scientist",
            "company": None,
            "profile_url": "https://www.linkedin.com/in/person-1/",
        },
        "date": "May 2, 1843",
        "relationship": "Ada worked with Mary on the same team",
        "text": "A remarkable scientific writer.",
    }


def test_inline_recommendations_parse_visible_received_cards():
    """Inline recommendations should read like the visible main-profile cards."""
    result = parse_profile_html(
        INLINE_RECOMMENDATIONS_HTML,
        {},
        profile_url="https://www.linkedin.com/in/tushar-kapratwar/",
    )

    assert result["section_status"]["recommendations"] == "parsed_inline"
    assert result["section_status"]["recommendations-given"] == "not_captured"
    assert result["profile"]["recommendations"]["received"] == [
        {
            "from": {
                "name": "James Tsan",
                "headline": "NPI Product Engineer",
                "company": None,
                "profile_url": "https://www.linkedin.com/in/james-tsan/",
            },
            "date": "March 8, 2024",
            "relationship": "James was Tushar’s client",
            "text": (
                "Having worked closely with Tushar, I've seen firsthand his "
                "exceptional work ethic."
            ),
        },
        {
            "from": {
                "name": "Harshala Patel, PMI-PMP, PSM",
                "headline": "Senior Technical Program Manager at Nvidia",
                "company": "Nvidia",
                "profile_url": "https://www.linkedin.com/in/harshala-patel/",
            },
            "date": "March 2, 2024",
            "relationship": "Harshala managed Tushar directly",
            "text": "I had the pleasure of directly managing Tushar for 1.5 years.",
        },
    ]


def test_recommendations_ignore_expanded_anchor_text_duplicates():
    """LinkedIn may put the whole card in an anchor; visible fields still win."""
    html = """
    <main><section aria-label="Primary content">
      <div data-testid="profile_RecommendationDetailsSection_ada">
        <div><h2>Recommendations</h2></div>
        <div>
          <a href="/in/arun-rk/">Arun R. · 3rd+ Information Security Lead April 10, 2018, Ada was Arun’s client</a>
          <p>Arun R.</p>
          <p>· 3rd+</p>
          <p>Information Security Lead</p>
          <p>April 10, 2018, Ada was Arun’s client</p>
          <p>Ada was thorough and optimistic.</p>
        </div>
      </div>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"recommendations": html},
        profile_url=PROFILE_URL,
    )

    recommendation = result["profile"]["recommendations"]["received"][0]
    assert recommendation["from"] == {
        "name": "Arun R.",
        "headline": "Information Security Lead",
        "company": None,
        "profile_url": "https://www.linkedin.com/in/arun-rk/",
    }
    assert "links" not in recommendation


@pytest.mark.parametrize(
    ("section", "bucket"),
    [
        ("activity-posts", "posts"),
        ("activity-comments", "comments"),
    ],
)
def test_activity_capture_is_routed_to_its_named_bucket(section, bucket):
    """Posts, comments, and articles are different claims and cannot be merged."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {section: FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    assert result["section_status"][section] == "parsed"
    items = result["profile"]["activity"][bucket]["items"]
    assert items[0]["post"]["author"]["name"] == "Charles Babbage"
    assert result["profile"]["activity"][bucket]["source_url"] == PROFILE_URL


def test_activity_card_reads_the_components_the_feed_renders():
    """Every field a card shows is in a named component; none is inferred from line order."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    post = result["profile"]["activity"]["posts"]["items"][0]

    assert post["position"] == 1
    assert post["id"] == "urn:li:activity:7492325888571658240"
    assert post["permalink"] == (
        "https://www.linkedin.com/feed/update/urn:li:activity:7492325888571658240/"
    )
    assert post["relation"] == "reposted"
    assert post["relation_text"] == "Ada Lovelace reposted this"
    # The tracking query on an actor link identifies the viewer, not the author.
    assert post["post"]["author"] == {
        "name": "Charles Babbage",
        "headline": "Inventor of the difference engine",
        "profile_url": "https://www.linkedin.com/in/charles-babbage",
        "avatar_url": "https://media.example/ada.jpg",
        "badges": ["Verified"],
        "followers": None,
        "is_organization": False,
    }
    assert post["post"]["posted"] == {
        "relative": "2d",
        "edited": True,
        "visibility": "anyone on or off LinkedIn",
    }
    assert post["post"]["engagement"] == {
        "reactions": {"count": 59, "types": ["like"]},
        "comments": 3,
        # Rendered counts omit a zero rather than showing one.
        "reposts": 0,
    }


def test_activity_card_drops_the_screen_reader_copy_of_every_label():
    """LinkedIn writes each label twice; keeping both yields "Charles BabbageCharles Babbage"."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    post = result["profile"]["activity"]["posts"]["items"][0]

    assert post["post"]["author"]["name"] == "Charles Babbage"
    assert post["post"]["commentary"]["text"] == "A milestone for Analytical Engines.\n#engines"
    assert post["post"]["commentary"]["truncated"] is True
    assert post["post"]["commentary"]["mentions"] == [
        {"name": "Analytical Engines", "url": "https://www.linkedin.com/company/engines/"}
    ]
    assert post["post"]["commentary"]["hashtags"] == ["#engines"]


def test_activity_keeps_a_reshared_post_separate_from_its_commentary():
    """A repost is two posts; merging them attributes the original to the reposter."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    post = result["profile"]["activity"]["posts"]["items"][0]
    shared = post["post"]["shared_post"]

    assert post["post"]["media"] == []
    assert shared["author"]["name"] == "Analytical Engines"
    assert shared["author"]["is_organization"] is True
    assert shared["author"]["followers"] == 1842
    assert shared["posted"]["relative"] == "3w"
    assert shared["commentary"]["text"] == "The engine is finished."
    assert shared["media"] == [
        {
            "type": "image",
            "url": "https://media.example/engine.png",
            "alt": "The engine",
            "more_count": 2,
        }
    ]


def test_activity_comments_nest_replies_under_the_comment_they_answer():
    """A reply lives inside its parent, so a flat read appends it to the wrong author."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-comments": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    comments = result["profile"]["activity"]["comments"]["items"][0]["thread"]

    assert len(comments) == 1
    assert comments[0]["author"]["name"] == "Mary Somerville"
    assert comments[0]["author"]["profile_url"] == "https://www.linkedin.com/in/mary-somerville/"
    assert comments[0]["text"] == "Congratulations!"
    assert comments[0]["reactions"] == 4
    assert [reply["text"] for reply in comments[0]["replies"]] == ["Thank you Mary"]


def test_activity_lifts_this_profiles_own_comment_out_of_the_thread():
    """The Comments tab answers "what did they say"; the answer must not need a search.

    Their contribution is a root comment when they commented on someone else's
    post and a reply when they answered on their own, so a consumer reading
    only one level finds it about two thirds of the time.
    """
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-comments": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    item = result["profile"]["activity"]["comments"]["items"][0]

    assert item["comments"] == [
        {
            "text": "Thank you Mary",
            "relative": "1d",
            "reactions": None,
            "truncated": False,
            "mentions": [],
            "is_reply": True,
            "in_reply_to": {
                "name": "Mary Somerville",
                "profile_url": "https://www.linkedin.com/in/mary-somerville/",
            },
        }
    ]
    # The post is context, kept under its own key: on this tab its author is
    # someone else, which `item["author"]` would have read as this profile.
    assert item["post"]["author"]["name"] == "Charles Babbage"


def test_activity_owner_is_matched_by_handle_not_by_name():
    """Names collide and are not links; the handle in the tab's address is exact."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"activity-comments": FEED_CARD_HTML},
        profile_url=PROFILE_URL,
        section_urls={"activity-comments": "https://www.linkedin.com/in/mary-somerville/recent-activity/comments/"},
    )

    thread = result["profile"]["activity"]["comments"]["items"][0]["thread"]

    assert thread[0]["author"]["name"] == "Mary Somerville"
    assert thread[0]["is_profile_owner"] is True
    assert thread[0]["replies"][0]["is_profile_owner"] is False
    assert [comment["text"] for comment in
            result["profile"]["activity"]["comments"]["items"][0]["comments"]] == ["Congratulations!"]


def test_activity_posts_and_comments_share_one_item_shape():
    """One shape for both tabs: a card is a post, plus whatever was said under it."""
    posts = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )["profile"]["activity"]["posts"]["items"][0]
    comments = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-comments": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )["profile"]["activity"]["comments"]["items"][0]

    assert set(posts) == set(comments)
    assert set(posts["post"]) == {
        "author", "posted", "commentary", "media", "shared_post", "engagement"
    }


def test_activity_marks_the_comment_written_by_the_posts_author():
    """"Author" beside a comment says the poster answered in their own thread."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-comments": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    thread = result["profile"]["activity"]["comments"]["items"][0]["thread"]

    assert thread[0]["is_post_author"] is False
    assert thread[0]["replies"][0]["is_post_author"] is True


def test_articles_are_read_as_articles_not_as_feed_cards():
    """The Articles tab renders a list, so a card reader finds nothing on it."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-articles": ARTICLE_LIST_HTML}, profile_url=PROFILE_URL
    )

    articles = result["profile"]["activity"]["articles"]

    assert result["section_status"]["activity-articles"] == "parsed"
    assert (articles["loaded"], articles["captured"], articles["complete"]) == (2, 2, True)
    assert articles["items"][0] == {
        "position": 1,
        # The slug is the identity: no urn exists on this tab, and the tracking
        # id names the session that saw the card rather than the article.
        "slug": "analytical-engine-notes-ada-x7f2c",
        "url": "https://www.linkedin.com/pulse/analytical-engine-notes-ada-x7f2c",
        "title": "Notes on the Analytical Engine",
        "preview_text": "The engine weaves algebraic patterns as the loom weaves flowers and lea",
        "truncated": True,
        "cover_image_url": "https://media.example/cover.png",
        "author_name": "Ada Lovelace",
        "read_time_minutes": 7,
        "byline": "by Ada Lovelace • 7 min read",
    }
    assert articles["items"][1]["cover_image_url"] is None
    assert articles["items"][1]["read_time_minutes"] == 11


def test_articles_stay_out_of_the_unclassified_fallback():
    """Parsed sections belong under activity; the fallback is for what we cannot read."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-articles": ARTICLE_LIST_HTML}, profile_url=PROFILE_URL
    )

    assert "activity-articles" not in result["profile"]["additional_sections"]


def test_an_empty_tab_is_parsed_and_empty_not_a_failure():
    """"Nothing to see for now" is an answer; reporting it as fallback loses that."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-comments": EMPTY_TAB_HTML}, profile_url=PROFILE_URL
    )

    comments = result["profile"]["activity"]["comments"]

    assert result["section_status"]["activity-comments"] == "parsed"
    assert comments["items"] == []
    # Captured-and-empty keeps its address; never-captured has none.
    assert comments["source_url"] == PROFILE_URL
    assert "activity-comments" not in result["profile"]["additional_sections"]


@pytest.mark.parametrize(
    ("wording", "reaction"),
    [
        ("Ada Lovelace likes this", "like"),
        ("Ada Lovelace loves this", "love"),
        ("Ada Lovelace celebrates this", "celebrate"),
        ("Ada Lovelace finds this insightful", "insightful"),
        ("Ada Lovelace supports this", "support"),
    ],
)
def test_a_reaction_records_which_one_was_given(wording, reaction):
    """The header names the reaction; "reacted" alone loses which one it was."""
    activity = FEED_CARD_HTML.replace("Ada Lovelace reposted this", wording)

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-reactions": activity}, profile_url=PROFILE_URL
    )

    item = result["profile"]["activity"]["reactions"]["items"][0]

    assert result["section_status"]["activity-reactions"] == "parsed"
    assert item["relation"] == "reacted"
    assert item["relation_text"] == wording
    assert item["reaction"] == reaction
    # What this person gave, beside what everyone gave: adjacent, not the same.
    assert item["post"]["engagement"]["reactions"]["types"] == ["like"]


def test_only_a_reaction_carries_the_reaction_field():
    """A field is present when we have it, and a post is not a reaction."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    assert "reaction" not in result["profile"]["activity"]["posts"]["items"][0]


def test_activity_dates_the_card_from_its_urn():
    """The rendered "2d" is a bucket; the urn carries the millisecond it happened."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    assert result["profile"]["activity"]["posts"]["items"][0]["activity_at"] == (
        "2026-08-09T21:08:12Z"
    )


def test_activity_reports_the_cards_lost_to_recycling():
    """A page announcing 20 posts commonly holds fewer: say so rather than imply completeness."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": FEED_CARD_HTML}, profile_url=PROFILE_URL
    )

    posts = result["profile"]["activity"]["posts"]

    assert posts["loaded"] == 3
    assert posts["captured"] == 1
    assert posts["complete"] is False


def test_activity_without_feed_cards_falls_back_with_its_envelope_intact():
    """Chrome that is not a card -- a tab bar, a media modal -- must not become a post."""
    activity = """
    <main><section aria-label="Primary content">
      <div><p>Posts Comments Videos Images More</p></div>
      <div><p>Media player modal window</p></div>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"activity-posts": activity}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["activity-posts"] == "fallback"
    assert result["profile"]["activity"]["posts"]["items"] == []
    assert result["profile"]["activity"]["posts"]["captured"] == 0
    assert result["sources"]["activity-posts"]["content"]["text"] == (
        "Posts Comments Videos Images More\n\nMedia player modal window"
    )


INTERESTS_URL = "https://www.linkedin.com/in/ada/details/interests/"


def test_interests_are_structured_under_the_category_the_page_names():
    """Switching a category changes neither URL nor history, so the strip is the label."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": INTERESTS_HTML},
        profile_url=PROFILE_URL,
        section_urls={"interests": INTERESTS_URL},
    )

    interests = result["profile"]["interests"]

    assert result["section_status"]["interests"] == "parsed"
    # Captured as `interests`, reported as what the checked radio said it is.
    assert interests["captured_categories"] == ["newsletters"]
    newsletters = interests["categories"]["newsletters"]
    assert newsletters["status"] == "parsed"
    assert newsletters["source_url"] == INTERESTS_URL
    assert newsletters["entries"][0] == {
        "name": "Spark",
        "url": "https://www.linkedin.com/newsletters/7183890373289648128/",
        "cadence": "Published monthly",
        "description": "Notes on engines and the people who build them",
        # Nested inside the newsletter's own anchor, so it belongs to it rather
        # than standing as an entry of its own.
        "publisher": {
            "name": "Analytical Engines",
            "url": "https://www.linkedin.com/company/engines/",
        },
        "image_url": "https://media.example/spark.png",
    }


def test_interests_omit_fields_the_page_does_not_carry():
    """A field is present when we have it; a key holding null claims we looked and found nothing."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": INTERESTS_HTML},
        profile_url=PROFILE_URL,
        section_urls={"interests": INTERESTS_URL},
    )

    spare = (
        result["profile"]["interests"]["categories"]["newsletters"]["entries"][1]
    )

    assert spare == {
        "name": "Looming",
        "url": "https://www.linkedin.com/newsletters/7183890373289648129/",
        "cadence": "Published weekly",
    }
    assert "description" not in spare
    assert "publisher" not in spare
    assert "image_url" not in spare


def test_interests_drop_the_profiles_own_header_link():
    """Every category opens with the profile's own link; it is the header, not an interest."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": INTERESTS_HTML},
        profile_url=PROFILE_URL,
        section_urls={"interests": INTERESTS_URL},
    )

    urls = [
        entry["url"]
        for entry in result["profile"]["interests"]["categories"]["newsletters"]["entries"]
    ]

    assert PROFILE_URL not in urls
    assert len(urls) == 2


def test_interests_categories_merge_into_one_object():
    """Five captures of one page; a category absent from the strip is absent here."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": INTERESTS_HTML, "interests-groups": INTERESTS_GROUPS_HTML},
        profile_url=PROFILE_URL,
        section_urls={"interests": INTERESTS_URL, "interests-groups": INTERESTS_URL},
    )

    interests = result["profile"]["interests"]

    assert interests["captured_categories"] == ["newsletters", "groups"]
    assert interests["categories"]["groups"] == {
        "status": "parsed",
        "source_url": INTERESTS_URL,
        "entries": [{
        "name": "Engine Builders",
        "url": "https://www.linkedin.com/groups/3983267/",
        # A group counts members; a company counts followers. Not the same claim.
        "members": 1080143,
        }],
    }
    assert "top_voices" not in interests["categories"]
    assert "companies" not in interests["categories"]


def test_an_inline_interests_card_capture_is_self_describing():
    """The no-Show-all fallback is a dedicated card capture at the profile URL.

    Its checked radio, rather than its URL or capture name, still owns the
    category identity.
    """
    inline_card = """
    <section>
      <button role="radio" aria-checked="true">Creator Programs</button>
      <a href="/in/grace-hopper/">
        <span>Grace Hopper</span><span>Computer pioneer</span>
      </a>
    </section>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": inline_card},
        profile_url=PROFILE_URL,
        section_urls={"interests": PROFILE_URL},
    )

    interests = result["profile"]["interests"]
    assert interests["captured_categories"] == ["creator_programs"]
    assert interests["categories"]["creator_programs"]["entries"] == [{
        "name": "Grace Hopper",
        "url": "https://www.linkedin.com/in/grace-hopper/",
        "headline": "Computer pioneer",
    }]


def test_interests_leave_the_unclassified_fallback():
    """Parsed interests belong under the profile, not in the raw-content bucket."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"interests": INTERESTS_HTML},
        profile_url=PROFILE_URL,
        section_urls={"interests": INTERESTS_URL},
    )

    assert "interests" not in result["profile"]["additional_sections"]


def test_a_section_that_did_not_parse_keeps_its_text():
    """The snapshot is a safety net for what no reader understands, not a copy of what one did."""
    unknown = """
    <main><section aria-label="Primary content">
      <div><p>Something no extractor knows about.</p></div>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"experience": EXPERIENCE_HTML, "unmapped-section": unknown},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["experience"] == "parsed"
    assert "content" not in result["sources"]["experience"]
    # Nothing reads this future section, so its text is the only copy there is.
    assert result["section_status"]["unmapped-section"] == "fallback"
    assert "Something no extractor knows about." in (
        result["sources"]["unmapped-section"]["content"]["text"]
    )
    assert "unmapped-section" in result["profile"]["additional_sections"]


# One featured item, as the section builds it: a single anchor holding the
# kind, the body, its hashtags, its thumbnail and its counts. The drawn count
# and the accessible one say the same shortened thing.
FEATURED_HTML = """
<main><section aria-label="Primary content">
  <h2>Featured</h2>
  <div><a href="https://www.linkedin.com/feed/update/urn:li:activity:7322961226836123648/">
    <div>
      <span>Post</span>
      <span>From 3.3 LPA to Times Square.</span>
      <span>Not from a Tier 1 college.</span>
      <div><a href="https://www.linkedin.com/search/results/all/?keywords=%23WomenInTech">#WomenInTech</a></div>
      <img src="https://media.example/times-square.png"/>
      <span>2K, number of reactions</span><span>2K</span>
      <span>132 comments · 4 reposts</span>
    </div>
  </a></div>
  <div><a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fexample%2Ecom%2Ftalk">
    <div><span>Link</span><span>My conference talk</span></div>
  </a></div>
</section></main>
"""


def test_featured_items_are_read_from_what_each_one_links_to():
    """The section pins posts and links; the generic reader returned its heading."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"featured": FEATURED_HTML}, profile_url=PROFILE_URL
    )

    featured = result["profile"]["featured"]

    assert result["section_status"]["featured"] == "parsed"
    assert featured[0] == {
        "position": 1,
        "kind": "post",
        "url": "https://www.linkedin.com/feed/update/urn:li:activity:7322961226836123648/",
        "id": "urn:li:activity:7322961226836123648",
        "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:7322961226836123648/",
        "posted_at": "2025-04-29T12:33:30Z",
        "text": "From 3.3 LPA to Times Square.\nNot from a Tier 1 college.",
        "hashtags": ["#WomenInTech"],
        # LinkedIn shortens what it draws, so the label is kept as written
        # rather than guessed into an integer.
        "engagement": {"reactions_label": "2K", "comments": 132, "reposts": 4},
        "image_url": "https://media.example/times-square.png",
    }


def test_a_featured_link_keeps_its_destination():
    """An outside address is featured too, and arrives behind the interstitial."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"featured": FEATURED_HTML}, profile_url=PROFILE_URL
    )

    assert result["profile"]["featured"][1] == {
        "position": 2,
        "kind": "link",
        "url": "https://example.com/talk",
        "text": "My conference talk",
    }


def test_a_structured_featured_section_gives_up_its_snapshot():
    """It was half of every retained snapshot; structured, it is duplication."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"featured": FEATURED_HTML}, profile_url=PROFILE_URL
    )

    assert "content" not in result["sources"]["featured"]


def test_about_member_is_structured():
    """Non-list profile captures must remain addressable by stable keys."""
    about_member = """
    <main><section aria-label="Primary content">
      <h2>About this member</h2><p>Joined LinkedIn in 2012.</p><p>Identity verified.</p>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": about_member},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["about_this_member"] == {
        # A dialog with neither heading is read as one that says nothing about
        # either, rather than as one that could not be read at all.
        "account_history": {"rows": []},
        "verifications": [],
        "links": [],
    }


# The top card as the newer application builds it: the name, the pronouns and
# connection degree beside it, the headline, then where this person is now, the
# location, and the link to contact details.
TOP_CARD_HTML = """
<main><section aria-label="Primary content">
  <div>
    <h1><a href="/in/ada/">Ada Lovelace</a></h1>
    <span>She/Her</span><span>Third</span>
    <span>Analytical engine programmer | Mathematician</span>
    <span>Analytical Engines · University of London</span>
    <span>London, England, United Kingdom</span><span>·</span>
    <a href="https://www.linkedin.com/in/ada/overlay/contact-info/">Contact info</a>
    <span>1,842 followers</span><span>500+</span><span>connections</span>
  </div>
  <div><p>Top skills</p>Mathematics • Technical Writing • Logic</div>
  <div><h2>Causes</h2></div>
  <div>Science and Technology • Education</div>
</section></main>
"""


def test_the_top_card_is_read_by_what_each_line_is_not_by_position():
    """Pronouns were being stored as the headline, and the headline as the location."""
    result = parse_profile_html(TOP_CARD_HTML, {}, profile_url=PROFILE_URL)

    profile = result["profile"]

    assert profile["name"] == "Ada Lovelace"
    assert profile["pronouns"] == "She/Her"
    assert profile["headline"] == "Analytical engine programmer | Mathematician"
    assert profile["location"] == "London, England, United Kingdom"
    assert profile["current_company"] == "Analytical Engines"
    assert profile["current_school"] == "University of London"
    assert profile["contact_info_url"] == (
        "https://www.linkedin.com/in/ada/overlay/contact-info/"
    )


def test_the_top_cards_own_lists_are_structured():
    """Top skills and causes are dot-separated lists, not prose."""
    result = parse_profile_html(TOP_CARD_HTML, {}, profile_url=PROFILE_URL)

    assert result["profile"]["top_skills"] == ["Mathematics", "Technical Writing", "Logic"]
    assert result["profile"]["causes"] == ["Science and Technology", "Education"]


@pytest.mark.parametrize("card", [
    "<div><p>Top skills</p><p>Mathematics • Logic</p></div>",
    "<div><div><h3>Top skills</h3></div><div><p>Mathematics • Logic</p></div></div>",
    "",
])
def test_about_excludes_the_nested_skills_card_without_changing_prose(card):
    html = f"""
    <main><section aria-label="Primary content">
      <h1>Ada Lovelace</h1>
      <section><div><h2>About</h2></div>
        <p>My Top skills include Mathematics and Logic.</p>
        <p>Mathematics • Logic</p>
        {card}
        <p>I also write about computing.</p>
      </section>
    </section></main>
    """
    result = parse_profile_html(html, {}, profile_url=PROFILE_URL)

    assert result["profile"]["about"] == (
        "My Top skills include Mathematics and Logic.\n\n"
        "Mathematics • Logic\n\nI also write about computing."
    )
    assert result["profile"]["top_skills"] == (
        ["Mathematics", "Logic"] if card else []
    )


def test_the_video_players_own_controls_are_not_captured_content():
    """Every embedded video contributed "Stream Type LIVE" and the rest to the text."""
    unknown = """
    <main><section aria-label="Primary content">
      <p>A section with a video.</p>
      <div class="video-js">
        <div class="vjs-control-bar"><span class="vjs-control-text">Stream Type</span>
          <span>LIVE</span><span>Playback Rate</span></div>
      </div>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"unmapped-section": unknown}, profile_url=PROFILE_URL
    )

    text = result["sources"]["unmapped-section"]["content"]["text"]
    assert "A section with a video." in text
    assert "Stream Type" not in text
    assert "Playback Rate" not in text


def test_a_section_that_states_it_is_empty_is_parsed_not_fallback():
    """"Nothing to see for now" is an answer from any section, not only a feed tab."""
    empty = """
    <main><section aria-label="Primary content">
      <h2>Recommendations</h2><p>Received</p><p>Given (1)</p>
      <p>Nothing to see for now</p>
      <p>Recommendations that Ada receives will appear here.</p>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"recommendations": empty}, profile_url=PROFILE_URL
    )

    assert result["section_status"]["recommendations"] == "parsed"
    assert result["profile"]["recommendations"]["received"] == []
    # Nothing was lost, so nothing needs preserving.
    assert "content" not in result["sources"]["recommendations"]
    assert "recommendations" not in result["profile"]["additional_sections"]


def test_unknown_section_is_preserved_as_fallback():
    """A newly captured LinkedIn section must never disappear from JSON."""
    unknown = """
    <main><section aria-label="Primary content">
      <h2>Volunteer causes</h2><p>STEM education</p><a href="/company/stem/">STEM Trust</a>
    </section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"volunteer-causes": unknown},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["volunteer-causes"] == "fallback"
    assert result["profile"]["additional_sections"]["volunteer-causes"] == {
        "text": "Volunteer causes\n\nSTEM education\n\nSTEM Trust",
        "links": [
            {"text": "STEM Trust", "url": "https://www.linkedin.com/company/stem/"}
        ],
    }
    assert result["warnings"] == [
        {
            "code": "unsupported_section",
            "section": "volunteer-causes",
            "message": "No semantic extractor for volunteer-causes; content preserved in fallback",
        }
    ]


def test_one_unusable_section_does_not_discard_the_profile():
    """A broken child capture must produce an error status, not an all-or-nothing failure."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"skills": b""},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["name"] == "Ada Lovelace"
    assert result["profile"]["skills"] == []
    assert result["section_status"]["skills"] == "error"
    assert result["warnings"][0]["code"] == "invalid_html"
    assert result["warnings"][0]["section"] == "skills"


def test_known_section_with_unknown_dom_uses_observable_fallback():
    """A LinkedIn DOM change must preserve data and announce loss of semantic fields."""
    changed = """
    <main><section aria-label="Primary content">Quantum computing</section></main>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"skills": changed},
        profile_url=PROFILE_URL,
    )

    assert result["section_status"]["skills"] == "fallback"
    assert result["profile"]["additional_sections"]["skills"]["text"] == "Quantum computing"
    assert result["warnings"] == [
        {
            "code": "semantic_fallback",
            "section": "skills",
            "message": "Could not identify structured skills entries; content preserved in fallback",
        }
    ]


# Causes is a section, not a scalar the top card happens to carry. LinkedIn
# renders it on the profile page with no "Show all" -- there is no causes page
# to capture -- so `parsed_inline` is the best status it can ever reach, the
# same one Education and Languages get when they are small enough to be served
# inline. Promoting it from a core field is what lets a consumer tell "this
# person listed no causes" from "nobody looked".
def test_causes_is_a_section_with_its_own_status():
    result = parse_profile_html(TOP_CARD_HTML, {}, profile_url=PROFILE_URL)

    assert result["section_status"]["causes"] == "parsed_inline"
    assert result["profile"]["causes"] == ["Science and Technology", "Education"]


def test_a_profile_without_a_causes_card_reports_it_uncaptured():
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {}, profile_url=PROFILE_URL)

    assert result["section_status"]["causes"] == "not_captured"
    assert result["profile"]["causes"] == []


def test_causes_can_be_declined_like_any_other_section():
    result = parse_profile_html(
        TOP_CARD_HTML, {}, profile_url=PROFILE_URL,
        requested_sections=frozenset({"experience"}),
    )

    assert result["section_status"]["causes"] == "not_requested"
    assert result["profile"]["causes"] == []


# One cause has no dot to split on. Read by the separator alone it came back
# empty, which reported a card that is plainly on the page as `not_captured`.
ONE_CAUSE_HTML = """
<main><section aria-label="Primary content">
  <div><h1><a href="/in/ada/">Ada Lovelace</a></h1></div>
  <div><h2>Causes</h2></div>
  <div>Science and Technology</div>
</section></main>
"""


def test_a_single_cause_is_still_a_captured_causes_card():
    result = parse_profile_html(ONE_CAUSE_HTML, {}, profile_url=PROFILE_URL)

    assert result["profile"]["causes"] == ["Science and Technology"]
    assert result["section_status"]["causes"] == "parsed_inline"


# The card as `capture_causes` stores it: the section alone, no <main> and no
# profile around it.
CAUSES_CARD_HTML = """
<section>
  <div><h2>Causes</h2></div>
  <div>Arts and Culture • Education • Environment</div>
</section>
"""


def test_a_supplied_causes_capture_is_read_as_that_capture():
    """`parsed`, not `parsed_inline`: this one has a capture behind it."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML, {"causes": CAUSES_CARD_HTML},
        profile_url=PROFILE_URL,
        section_urls={"causes": PROFILE_URL},
    )

    assert result["section_status"]["causes"] == "parsed"
    assert result["profile"]["causes"] == [
        "Arts and Culture", "Education", "Environment"]


def test_a_causes_capture_wins_over_the_profile_pages_own_card():
    """The capture is the card; the profile page holds a second copy of it."""
    result = parse_profile_html(
        TOP_CARD_HTML, {"causes": CAUSES_CARD_HTML},
        profile_url=PROFILE_URL, section_urls={"causes": PROFILE_URL},
    )

    assert result["section_status"]["causes"] == "parsed"
    assert result["profile"]["causes"] == [
        "Arts and Culture", "Education", "Environment"]


def test_causes_falls_back_to_the_profile_page_when_its_capture_failed():
    """A card that would not screenshot must not lose the list itself."""
    result = parse_profile_html(
        TOP_CARD_HTML, {}, profile_url=PROFILE_URL)

    assert result["section_status"]["causes"] == "parsed_inline"
    assert result["profile"]["causes"] == ["Science and Technology", "Education"]


# The About this member dialog as LinkedIn builds it, trimmed of classes and
# svgs but with its nesting intact: the account-history rows sit directly under
# their own heading, while the verification entries sit in a sibling of the
# wrapper their heading is buried in.
ABOUT_MEMBER_DIALOG_HTML = """
<div data-sdui-screen="...ProfileVerificationDetailsDialog">
 <div>
  <div>
   <div>
    <h3>Account history</h3>
    <div><p>Joined LinkedIn</p><p>June 2022</p></div>
    <div><p>Contact info</p><p>Updated over 1 year ago</p></div>
    <div><p>Profile photo</p><p>Updated less than 1 year ago</p></div>
   </div>
   <div>
    <div><div><div><div><h3>Verifications</h3></div></div></div>
    <div><div>
     <div>
      <figure><img src="https://media.licdn.com/blackrock.png"></figure>
      <div><div><p>BlackRock</p>
        <p>Verified using work email in August 2025</p></div></div>
     </div>
     <div>
      <figure><img src="https://media.licdn.com/vit.png"></figure>
      <div><div><p>Vishwakarma Institute Of Technology</p>
        <p>Verified using educational email in November 2023</p></div></div>
     </div>
    </div></div>
   </div>
  </div>
  <p><a href="https://www.linkedin.com/help/linkedin/answer/a1359065/">Learn more about verifications</a></p>
 </div>
</div>
"""


def test_account_history_says_each_row_once():
    """The three rows LinkedIn shows today were also reported under keys of
    their own, so every value arrived twice. `rows` is the one place they are
    reported now, and the only place a row LinkedIn adds later could be."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": ABOUT_MEMBER_DIALOG_HTML},
        profile_url=PROFILE_URL,
    )

    history = result["profile"]["about_this_member"]["account_history"]
    assert list(history) == ["rows"]


def test_about_this_member_keeps_every_account_history_row_verbatim():
    """A row LinkedIn adds tomorrow has no key here, and must survive anyway."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": ABOUT_MEMBER_DIALOG_HTML},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["about_this_member"]["account_history"]["rows"] == [
        {"label": "Joined LinkedIn", "value": "June 2022"},
        {"label": "Contact info", "value": "Updated over 1 year ago"},
        {"label": "Profile photo", "value": "Updated less than 1 year ago"},
    ]


def test_about_this_member_reads_each_verification_as_an_entry():
    """Who verified what, how, and when -- the four facts the sentence holds."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": ABOUT_MEMBER_DIALOG_HTML},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["about_this_member"]["verifications"] == [
        {
            "entity": "BlackRock",
            "detail": "Verified using work email in August 2025",
            "method": "work email",
            "verified_on": "August 2025",
            "logo_url": "https://media.licdn.com/blackrock.png",
        },
        {
            "entity": "Vishwakarma Institute Of Technology",
            "detail": "Verified using educational email in November 2023",
            "method": "educational email",
            "verified_on": "November 2023",
            "logo_url": "https://media.licdn.com/vit.png",
        },
    ]


def test_a_verification_sentence_it_cannot_split_is_still_reported():
    """The structure is a convenience; the sentence is the record."""
    dialog = ABOUT_MEMBER_DIALOG_HTML.replace(
        "Verified using work email in August 2025", "Verified by a new means")

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": dialog},
        profile_url=PROFILE_URL,
    )

    entry = result["profile"]["about_this_member"]["verifications"][0]
    assert entry["detail"] == "Verified by a new means"
    assert entry["method"] is None
    assert entry["verified_on"] is None


def test_a_member_with_no_verifications_still_has_an_account_history():
    """Unverified is the ordinary case, and it is what this work is for."""
    dialog = """
    <div data-sdui-screen="...ProfileVerificationDetailsDialog">
     <div><div>
      <h3>Account history</h3>
      <div><p>Joined LinkedIn</p><p>June 2022</p></div>
     </div></div>
    </div>
    """

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": dialog},
        profile_url=PROFILE_URL,
    )

    about = result["profile"]["about_this_member"]
    assert about["verifications"] == []
    assert about["account_history"]["rows"] == [
        {"label": "Joined LinkedIn", "value": "June 2022"}]


def test_about_this_member_carries_no_flattened_text():
    """The dialog's text was every row and verification run together, plus the
    dialog chrome -- "Done", the notification count -- which is not about this
    member at all. The two lists say all of it, and say which is which."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": ABOUT_MEMBER_DIALOG_HTML},
        profile_url=PROFILE_URL,
    )

    assert "text" not in result["profile"]["about_this_member"]


def test_linkedins_own_help_page_is_not_reported_as_a_link():
    """Every member's dialog ends with the same link to the help centre, which
    explains verifications rather than saying anything about this member."""
    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": ABOUT_MEMBER_DIALOG_HTML},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["about_this_member"]["links"] == []


def test_a_link_that_is_not_the_help_page_is_still_reported():
    """`links` is dropped down to the ones worth following, not emptied."""
    dialog = ABOUT_MEMBER_DIALOG_HTML.replace(
        '<a href="https://www.linkedin.com/help/linkedin/answer/a1359065/">'
        "Learn more about verifications</a>",
        '<a href="https://blackrock.com/verify">Verify with BlackRock</a>',
    )

    result = parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"about-this-member": dialog},
        profile_url=PROFILE_URL,
    )

    assert result["profile"]["about_this_member"]["links"] == [{
        "text": "Verify with BlackRock",
        "url": "https://blackrock.com/verify",
    }]


CONTACT_INFO_DIALOG_HTML = """
<div role="dialog">
 <header><h2>Contact info</h2></header>
 <div data-testid="dialog-content">
  <div data-testid="lazy-column">
   <div><p>Jensen\u2019s profile</p>
     <a href="https://www.linkedin.com/in/jensen-huang/">linkedin.com/in/jensen-huang</a>
   </div>
   <div><p>Website</p>
     <a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fnvidia%2Ecom%2F&amp;urlhash=9oCK">nvidia.com</a>
     <span>(Company)</span>
   </div>
   <div><p>Email</p>
     <a href="mailto:jensen@nvidia.com">jensen@nvidia.com</a>
   </div>
   <div><p>Phone</p><p>+1 408 555 0100</p><span>(Mobile)</span></div>
   <div><p>Birthday</p><p>February 17</p></div>
   <div><p>Connected</p><p>March 3, 2021</p></div>
   <div><p>Fax</p><p>+1 408 555 0199</p></div>
   <div>
     <p>Get up to 4.6x replies when you message with InMail</p>
     <p>Millions of members use Premium</p>
     <a href="https://www.linkedin.com/premium/products/?upsellSlotId=PROFILE_CONTACT_INFO">Try Premium for \u20b90</a>
   </div>
  </div>
 </div>
</div>
"""


def _contact_info(dialog_html=CONTACT_INFO_DIALOG_HTML):
    return parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"contact-info": dialog_html},
        profile_url=PROFILE_URL,
    )["profile"]["contact_info"]


def test_contact_info_reads_each_labelled_detail_under_its_own_key():
    """The dialog is a list of labelled details, and a caller wanting an email
    should not have to know which position LinkedIn rendered it in."""
    info = _contact_info()

    assert info["email"] == "jensen@nvidia.com"
    assert info["birthday"] == "February 17"
    assert info["connected_on"] == "March 3, 2021"
    assert info["profile_url"] == "https://www.linkedin.com/in/jensen-huang/"


def test_contact_info_keeps_each_website_with_the_label_it_was_given():
    """"nvidia.com (Company)" is two facts, and flattening it to a URL loses
    the half that says what the URL is."""
    assert _contact_info()["websites"] == [
        {"url": "https://nvidia.com/", "label": "Company"},
    ]


def test_contact_info_keeps_a_phone_number_with_its_type():
    assert _contact_info()["phones"] == [
        {"number": "+1 408 555 0100", "type": "Mobile"},
    ]


def test_a_detail_we_have_no_key_for_is_still_reported():
    """LinkedIn adds fields. One we do not name must still reach a caller
    rather than being dropped for want of a place to put it."""
    assert {"label": "Fax", "value": "+1 408 555 0199"} in _contact_info()["other"]


def test_a_detail_the_member_did_not_publish_is_absent_rather_than_null():
    """Nine keys of null said nothing and buried the two that did. Only what
    the member actually published is reported."""
    info = _contact_info()

    assert "address" not in info
    assert "im" not in info
    assert info["email"] == "jensen@nvidia.com"


def test_an_empty_dialog_reports_nothing_rather_than_a_shape():
    assert _contact_info('<div role="dialog"><h2>Contact info</h2></div>') == {}


def test_the_website_address_is_the_site_not_linkedins_interstitial():
    """LinkedIn wraps an outbound link in /safety/go/. Storing that stores a
    redirect through LinkedIn rather than the address the member gave."""
    assert _contact_info()["websites"][0]["url"] == "https://nvidia.com/"


def test_the_premium_advertisement_is_not_a_contact_detail():
    """The dialog sells Premium in a row shaped like every detail above it."""
    info = _contact_info()

    assert "other" not in info or all(
        "Premium" not in entry["label"] for entry in info["other"])
    assert "links" not in info


OPEN_TO_WORK_DIALOG_HTML = """
<dialog data-testid="dialog" open>
 <div data-testid="dialog-content">
  <div data-testid="lazy-column">
   <div>
    <div><p>Afreen Munshi, PMP\u00ae</p><p>is Open to Work</p></div>
    <div><p>Job titles</p>
      <p>Software Engineer \u00b7 Data Engineer \u00b7 Data Analyst</p></div>
    <div><p>Location types</p><p>On-site \u00b7 Hybrid</p></div>
    <div><p>Locations</p>
      <p>California, United States \u00b7 Seattle, WA \u00b7 United States</p></div>
    <div><p>Start date</p><p>Immediately, I am actively applying</p></div>
    <div><p>Employment types</p><p>Full-time \u00b7 Internship</p></div>
    <div><p>Notice period</p><p>2 months</p></div>
    <div><p>Looking for a new job too?</p>
      <p>Get support from the community by joining millions of members.</p></div>
    <a href="https://www.linkedin.com/jobs/">Get started</a>
   </div>
  </div>
 </div>
 <section><h2>0 notifications</h2></section>
</dialog>
"""


def _open_to_work(dialog_html=OPEN_TO_WORK_DIALOG_HTML):
    return parse_profile_html(
        STRUCTURED_PROFILE_HTML,
        {"open-to-work": dialog_html},
        profile_url=PROFILE_URL,
    )["profile"]["open_to_work"]


def test_open_to_work_reads_each_preference_under_its_own_key():
    info = _open_to_work()

    assert info["job_titles"] == [
        "Software Engineer", "Data Engineer", "Data Analyst"]
    assert info["location_types"] == ["On-site", "Hybrid"]
    assert info["employment_types"] == ["Full-time", "Internship"]


def test_a_location_holding_a_comma_survives_the_split():
    """The dialog separates values with a middot and commas fall inside them,
    so splitting on the comma would turn one place into two."""
    assert _open_to_work()["locations"] == [
        "California, United States", "Seattle, WA", "United States"]


def test_a_start_date_is_one_answer_rather_than_a_list():
    assert _open_to_work()["start_date"] == "Immediately, I am actively applying"


def test_a_preference_we_have_no_key_for_is_still_reported():
    assert {"label": "Notice period", "value": "2 months"} in _open_to_work()["other"]


def test_the_dialog_footer_advertisement_is_not_a_preference():
    """LinkedIn sells a job-search product at the bottom of this dialog, in a
    row shaped like every preference above it. A pitch asks a question; a
    field does not."""
    labels = [entry["label"] for entry in _open_to_work()["other"]]

    assert "Looking for a new job too?" not in labels


def test_the_members_own_name_is_not_a_preference():
    """The dialog opens with "<name> is Open to Work", which is the same two
    paragraphs every preference below it is made of."""
    labels = [entry["label"] for entry in _open_to_work()["other"]]

    assert not any("Afreen" in label for label in labels)


def test_a_preference_the_member_did_not_set_is_absent_rather_than_null():
    """Only what the member chose. A shape padded with nulls says nothing
    several ways at once and buries the answers that are real."""
    assert _open_to_work('<div role="dialog"><h2>Open to Work</h2></div>') == {}
