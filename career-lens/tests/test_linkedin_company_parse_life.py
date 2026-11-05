"""Focused coverage for LinkedIn company Life-tab parsing."""

from __future__ import annotations

import json

from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"
LIFE_URL = f"{COMPANY_URL}life/engineering/"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card">
    <h1 class="org-top-card-summary__title">Acme</h1>
  </section>
</section></main></body></html>
"""

LIFE_HTML = """
<html><body><main><section aria-label="Primary content">
  <div role="region" aria-label="Life at Acme">
    <div class="org-life-target-selection-tabs-v2">
      <h3 id="target-selection-tabs-title">Careers at Acme</h3>
      <ul role="tablist" aria-labelledby="target-selection-tabs-title">
        <li><a role="tab" aria-current="true" href="/company/acme/life/engineering/">
          Engineering
        </a></li>
        <li><a role="tab" aria-current="false" href="/company/acme/life/sales/">
          Sales
        </a></li>
      </ul>
    </div>

    <section class="hero-media-module">
      <div data-view-name="org-member-life-hero-image">
        <img src="https://media.example/life-hero.jpg" alt="Acme office"/>
      </div>
    </section>

    <section data-view-name="org-member-life-trending-employee-content">
      <h3>Trending employee content</h3>
      <div class="feed-shared-update-v2"
           data-urn="urn:li:activity:7492325888571658250">
        <div class="feed-shared-update-v2__control-menu-container"></div>
        <div class="update-components-actor__container">
          <a class="update-components-actor__meta-link" href="/in/alex-example/">
            <span class="update-components-actor__title">
              <span aria-hidden="true">Alex Example</span>
            </span>
            <span class="update-components-actor__description">
              <span aria-hidden="true">Engineer at Acme</span>
            </span>
            <span class="update-components-actor__sub-description">
              <span aria-hidden="true">3d</span>
            </span>
          </a>
        </div>
        <div class="update-components-text">
          <span>Building dependable systems at Acme.</span>
        </div>
      </div>
    </section>

    <section data-view-name="org-member-life-company-spotlight">
      <div class="org-life-custom-modules-module__media">
        <iframe src="https://www.youtube-nocookie.com/embed/demo"
                title="Engineering at Acme"></iframe>
      </div>
      <h4 class="inline-title">Engineering culture</h4>
      <p class="org-life-custom-modules-module__inline-body">
        How Acme builds reliable products.
      </p>
      <ul class="media-section-list">
        <li><a href="https://acme.example/culture">Learn more</a></li>
      </ul>
    </section>

    <section data-view-name="org-member-life-interest-pipeline">
      <h3>Interested in working with us in the future?</h3>
      <button>I'm interested</button>
    </section>

    <section data-view-name="org-member-life-page-featured-members">
      <h2 class="org-module__heading-title">Meet our engineering leaders</h2>
      <a data-view-name="org-member-life-page-featured-members-profile-link"
         href="/in/jamie-example/">
        <img class="profile-background-image__image"
             src="https://media.example/jamie-cover.jpg"/>
        <img class="org-profile-card-content__profile-picture"
             src="https://media.example/jamie.jpg"/>
        <dl class="org-profile-card-content__info">
          <dt><h3>Jamie Example</h3></dt><dd>VP of Engineering</dd>
        </dl>
      </a>
    </section>

    <section data-view-name="org-member-life-company-photos">
      <h3>Company photos</h3>
      <button class="org-life-company-photos-module__photo-button">
        <img src="https://media.example/company-photo.jpg"
             alt="Acme company photo 1"/>
        <div class="org-life-company-photos-module__photo-overlay">
          <p></p><p>January 2, 2026</p>
        </div>
      </button>
    </section>

    <section data-view-name="org-member-life-page-employee-testimonials">
      <h2 class="org-module__heading-title">Employee testimonials</h2>
      <ul><li class="org-life-testimonials-module__testimonial">
        <a data-view-name="org-member-life-page-employee-testimonials-profile-link"
           href="/in/taylor-example/">
          <img class="org-profile-card-content__profile-picture"
               src="https://media.example/taylor.jpg"/>
          <dl class="org-profile-card-content__info">
            <dt><h3>Taylor Example</h3></dt><dd>Staff Engineer</dd>
          </dl>
        </a>
        <p class="org-life-testimonials-module__text">
          Acme gives engineers room to grow.
        </p>
      </li></ul>
    </section>
  </div>
</section></main></body></html>
"""

def test_company_life_preserves_rendered_modules() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"life": LIFE_HTML},
        company_url=COMPANY_URL,
        tab_urls={"life": LIFE_URL},
    )

    life = result["company"]["life"]
    assert life["source_url"] == LIFE_URL
    assert life["title"] == "Careers at Acme"
    assert life["available_pages"] == [
        {"name": "Engineering", "url": LIFE_URL, "selected": True},
        {
            "name": "Sales",
            "url": f"{COMPANY_URL}life/sales/",
            "selected": False,
        },
    ]
    assert life["hero"] == {
        "type": "image",
        "url": "https://media.example/life-hero.jpg",
        "alt_text": "Acme office",
    }
    assert life["employee_content"]["title"] == "Trending employee content"
    assert len(life["employee_content"]["items"]) == 1
    assert life["employee_content"]["items"][0]["post"]["author"]["name"] == (
        "Alex Example"
    )
    assert life["spotlights"] == [
        {
            "title": "Engineering culture",
            "description": "How Acme builds reliable products.",
            "media": {
                "type": "video",
                "url": "https://www.youtube-nocookie.com/embed/demo",
                "title": "Engineering at Acme",
            },
            "links": [
                {"label": "Learn more", "url": "https://acme.example/culture"}
            ],
        }
    ]
    assert life["featured_members"] == {
        "title": "Meet our engineering leaders",
        "items": [
            {
                "name": "Jamie Example",
                "headline": "VP of Engineering",
                "profile_url": "https://www.linkedin.com/in/jamie-example/",
                "image_url": "https://media.example/jamie.jpg",
                "background_image_url": "https://media.example/jamie-cover.jpg",
            }
        ],
    }
    assert life["photos"] == {
        "title": "Company photos",
        "items": [
            {
                "image_url": "https://media.example/company-photo.jpg",
                "alt_text": "Acme company photo 1",
                "posted_on": "January 2, 2026",
            }
        ],
    }
    assert life["testimonials"] == {
        "title": "Employee testimonials",
        "items": [
            {
                "text": "Acme gives engineers room to grow.",
                "author": {
                    "name": "Taylor Example",
                    "headline": "Staff Engineer",
                    "profile_url": "https://www.linkedin.com/in/taylor-example/",
                    "image_url": "https://media.example/taylor.jpg",
                },
            }
        ],
    }
    serialized = json.dumps(life)
    assert "Interested in working" not in serialized
    assert "loaded" not in life["employee_content"]
    assert "captured" not in life["employee_content"]
    assert "complete" not in life["employee_content"]
    assert result["section_status"]["life"] == "parsed"
    assert result["sources"]["life"] == {"artifact": None, "url": LIFE_URL}

def test_company_life_allows_optional_modules_to_be_absent() -> None:
    minimal_life = """
    <html><body><main><section aria-label="Primary content">
      <h3 id="target-selection-tabs-title">Life at Acme</h3>
    </section></main></body></html>
    """
    result = parse_company_html(
        HOME_HTML,
        {"life": minimal_life},
        company_url=COMPANY_URL,
        tab_urls={"life": f"{COMPANY_URL}life/"},
    )

    assert result["company"]["life"] == {
        "source_url": f"{COMPANY_URL}life/",
        "title": "Life at Acme",
        "available_pages": [],
        "hero": None,
        "employee_content": {"title": None, "items": []},
        "spotlights": [],
        "featured_members": {"title": None, "items": []},
        "photos": {"title": None, "items": []},
        "testimonials": {"title": None, "items": []},
    }
