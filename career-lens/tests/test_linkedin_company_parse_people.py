"""Focused coverage for LinkedIn company People-tab parsing."""

from __future__ import annotations

import json

from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"
PEOPLE_URL = f"{COMPANY_URL}people/"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card">
    <h1 class="org-top-card-summary__title">Acme</h1>
  </section>
</section></main></body></html>
"""

PEOPLE_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="org-people__insights-container">
    <h2>17,030 associated members</h2>
    <section data-career_lens="people-analytics">
      <h2>People analytics</h2>
      <article>
        <h3>Where they live</h3>
        <ul>
          <li><strong>8,952</strong> United States</li>
          <li><strong>2,105</strong> India</li>
        </ul>
      </article>
      <article>
        <h3>What they do</h3>
        <ul><li><strong>5,845</strong> Engineering</li></ul>
      </article>
      <article>
        <h3>How you are connected</h3>
        <ul><li><strong>15,695</strong> 3rd+</li></ul>
      </article>
    </section>
  </div>

  <section class="org-people-profile-card__card-spacing">
    <h2>People you may know</h2>
    <ul>
      <li class="org-people-profile-card__profile-card-spacing">
        <a href="/in/alex-example/?miniProfileUrn=one">
          <img src="https://media.example/alex.jpg" alt="Alex Example"/>
        </a>
        <div class="artdeco-entity-lockup__title">
          <a href="/in/alex-example/?miniProfileUrn=one">Alex Example</a>
        </div>
        <div class="artdeco-entity-lockup__subtitle">Engineer at Acme</div>
      </li>
      <li class="org-people-profile-card__profile-card-spacing">
        <div class="artdeco-entity-lockup__title">
          <a href="/in/sam-example/">Sam Example</a>
        </div>
        <div class="artdeco-entity-lockup__subtitle">Product at Acme</div>
      </li>
    </ul>
    <button>Show more results</button>
  </section>

  <aside><h2>Pages people also viewed</h2>
    <a href="/company/not-acme/">Not Acme</a>
  </aside>
</section></main></body></html>
"""

LEGACY_PEOPLE_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="org-people__insights-container">
    <h2>42 associated members</h2>
    <div class="artdeco-carousel">
      <div class="insight-container">
        <div class="insight-container__title"><h3>Where they studied</h3></div>
        <button class="org-people-bar-graph-element">
          <strong>12</strong>
          <span class="org-people-bar-graph-element__category">Acme University</span>
        </button>
      </div>
    </div>
  </div>
</section></main></body></html>
"""

def test_company_people_parses_complete_analytics_and_rendered_suggestions() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"people": PEOPLE_HTML},
        company_url=COMPANY_URL,
        tab_urls={"people": PEOPLE_URL},
        tab_capture={
            "people": {
                "people_analytics": {
                    "positions_visited": 6,
                    "positions_total": 6,
                }
            }
        },
    )

    assert result["company"]["people"] == {
        "source_url": PEOPLE_URL,
        "analytics": {
            "reached_end": True,
            "cards": [
                {
                    "title": "Where they live",
                    "items": [
                        {"label": "United States", "count": 8952},
                        {"label": "India", "count": 2105},
                    ],
                },
                {
                    "title": "What they do",
                    "items": [{"label": "Engineering", "count": 5845}],
                },
            ],
        },
        "people_you_may_know": [
            {
                "name": "Alex Example",
                "headline": "Engineer at Acme",
                "profile_url": "https://www.linkedin.com/in/alex-example/",
                "image_url": "https://media.example/alex.jpg",
            },
            {
                "name": "Sam Example",
                "headline": "Product at Acme",
                "profile_url": "https://www.linkedin.com/in/sam-example/",
                "image_url": None,
            },
        ],
    }
    assert result["section_status"]["people"] == "parsed"
    assert result["sources"]["people"] == {"artifact": None, "url": PEOPLE_URL}
    assert "Not Acme" not in json.dumps(result["company"]["people"])

def test_company_people_marks_an_interrupted_carousel_incomplete() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"people": PEOPLE_HTML},
        company_url=COMPANY_URL,
        tab_capture={
            "people": {
                "people_analytics": {
                    "positions_visited": 3,
                    "positions_total": 6,
                }
            }
        },
    )

    assert result["company"]["people"]["analytics"]["reached_end"] is False

def test_company_people_reads_visible_legacy_cards_with_unknown_completeness() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"people": LEGACY_PEOPLE_HTML},
        company_url=COMPANY_URL,
    )

    people = result["company"]["people"]
    assert "associated_members_count" not in people
    assert people["analytics"] == {
        "reached_end": None,
        "cards": [
            {
                "title": "Where they studied",
                "items": [{"label": "Acme University", "count": 12}],
            }
        ],
    }
    assert people["people_you_may_know"] == []
