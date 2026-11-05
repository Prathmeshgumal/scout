"""Regression coverage for LinkedIn's current company-page markup."""

from career_lens.linkedin.parse.company import parse_company_html


COMPANY_URL = "https://www.linkedin.com/company/acme/"


def test_current_newsletter_uses_heading_and_links_without_legacy_classes():
    from career_lens.linkedin.parse.company_home import extract_home
    from lxml.html import fromstring

    document = fromstring('''<html><body><main><div>
      <div><h2>Newsletter</h2></div>
      <a href="/newsletters/acme-news-123/">
        <img src="https://media.example/news.png">
        <p>Acme News</p><p>Published biweekly · 1,234 subscribers</p>
      </a>
      <a href="/newsletters/acme-news-123/"><p>News from Acme.</p></a>
      <button>Subscribe</button>
    </div></main></body></html>''')
    assert extract_home(document, COMPANY_URL)["newsletters"] == [{
        "name": "Acme News", "url": "https://www.linkedin.com/newsletters/acme-news-123/",
        "publication_frequency": "Published biweekly", "subscriber_count": 1234,
        "description": "News from Acme.", "image_url": "https://media.example/news.png",
    }]


HOME_HTML = """
<html><body><main>
  <section>
    <img alt="Cover photo" src="https://media.example/acme-cover.jpg">
    <img alt="" src="https://media.example/company-logo_100_100/acme.png">
    <div>
      <h2>Acme</h2>
      <a href="https://www.linkedin.com/company/acme/home/">
        <svg role="img" aria-label="Verified page. Click for details."></svg>
      </a>
      <p>Build dependable systems.</p>
      <div>
        <p>Software Development</p><p aria-hidden="true">·</p>
        <p>Workflow Automation</p><p aria-hidden="true">·</p>
        <p>London, England</p><p aria-hidden="true">·</p>
        <p>2M followers</p><p aria-hidden="true">·</p>
        <a href="/search/results/people/?currentCompany=42">5K-10K employees</a>
      </div>
      <a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Facme.example">Learn more</a>
    </div>
    <nav>
      <a href="https://www.linkedin.com/company/acme/home/">Home</a>
      <a href="https://www.linkedin.com/company/acme/about/">About</a>
      <a href="https://www.linkedin.com/company/acme/posts/">Posts</a>
      <a href="https://www.linkedin.com/company/acme/products/">Products</a>
      <a href="https://www.linkedin.com/company/acme/jobs/">Jobs</a>
      <a href="https://www.linkedin.com/company/acme/life/">Life</a>
      <a href="https://www.linkedin.com/company/acme/people/">People</a>
    </nav>
    <aside>
      <h2>Affiliated pages</h2>
      <a href="https://www.linkedin.com/showcase/acme-support/">
        <img src="https://media.example/support.png">
        <p>Acme Support</p><p>Software Development</p><p>Showcase page</p>
      </a>
    </aside>
  </section>
</main></body></html>
"""

ABOUT_HTML = """
<html><body><main><section>
  <div><h2>Overview</h2></div>
  <div>
    <p>Acme makes dependable automation.</p>
    <div><div><p>Website</p></div><div><a href="https://www.linkedin.com/safety/go/?url=https%3A%2F%2Facme.example">acme.example</a></div></div>
    <div><div><p>Industry</p></div><div><p>Software Development</p></div></div>
    <div>
      <div><p>Company size</p></div>
      <div><p>5,001-10,000 employees</p></div>
      <div><a href="/search/results/people/?currentCompany=42">17,103 associated members</a></div>
    </div>
    <div><div><p>Headquarters</p></div><div><p>London, England</p></div></div>
    <div><div><p>Founded</p></div><div><p>2010</p></div></div>
  </div>
</section></main></body></html>
"""

POSTS_HTML = """
<html><body><main><ul>
  <li role="listitem" componentkey="update-card-current-layout">
    <h2>Feed post</h2>
    <a href="/company/acme/"><p>Acme</p></a>
    <svg aria-label="View company: Acme"></svg>
    <img src="https://media.example/company-logo_100_100/acme.png" alt="View company: Acme">
    <svg aria-label="Visibility: Global"></svg>
    <p>1w ·</p>
    <span data-testid="expandable-text-box">A current-layout company post. <a href="https://acme.example/update">Read more</a></span>
    <img src="https://media.example/feedshare-shrink_480/update.png" alt="View image">
    <button aria-label="Reaction button state: no reaction"></button>
    <button aria-label="Comment"></button>
    <button aria-label="Repost"></button>
  </li>
</ul></main></body></html>
"""

PRODUCT_HTML = """
<html><body><main>
  <section><h2>What is Acme Flow?</h2><p>Automates dependable work.</p></section>
  <section><h2>Product media</h2><img src="https://media.example/demo.png" alt="Product demo"></section>
  <section>
    <h2>Top rated features</h2>
    <p>Based on customer ratings.</p>
    <div><h3>Encryption</h3><p>Protects workflow data.</p></div>
  </section>
</main></body></html>
"""

JOBS_HTML = """
<html><body><main><section>
  <h2>Recently posted jobs</h2>
  <a href="https://www.linkedin.com/jobs/search-results/?currentJobId=100&amp;f_C=42">
    <p>Platform Engineer (Verified job)Platform Engineer</p>
    <p>Acme</p><p>London (Hybrid)</p><p>Be an early applicant</p>
    <p>·</p><p>Posted 2 days ago2 days ago</p>
  </a>
  <a href="/jobs/search-results/?f_C=42">Show all</a>
</section></main></body></html>
"""

LIFE_HTML = """
<html><body><main>
  <section><h2>Careers at Acme</h2></section>
  <section>
    <h2>Meet our team</h2>
    <a href="/in/alex-example/"><img src="https://media.example/alex.jpg"><h3>Alex Example</h3><p>VP Engineering</p></a>
  </section>
  <section>
    <h2>Company spotlights</h2>
    <article><h3>Creating access</h3><p>Build products for everyone.</p><a href="https://acme.example/culture">Learn more</a></article>
  </section>
  <section><h2>Company photos</h2><img src="https://media.example/team.jpg" alt="Acme team"></section>
</main></body></html>
"""

PEOPLE_HTML = """
<html><body><main>
  <h2>17,103 associated members</h2>
  <ul>
    <li>
      <h3>Where they live</h3>
      <a role="checkbox" aria-label="Where they live · United Kingdom · 8,166"><p>United Kingdom</p><p>8,166</p></a>
      <a role="checkbox" aria-label="Where they live · India · 2,211"><p>India</p><p>2,211</p></a>
    </li>
    <li>
      <h3>How you are connected</h3>
      <a role="checkbox"><p>2nd</p><p>10</p></a>
    </li>
  </ul>
</main></body></html>
"""

EVENTS_HTML = """
<html><body><main>
  <section>
    <h2>Upcoming events</h2>
    <article>
      <a href="/events/acme-launch-123/"><h3>Acme Launch</h3></a>
      <p>Wed, Sep 9, 2026, 5:00 PM</p>
      <p>42 attendees</p>
      <img src="https://media.example/event.jpg">
    </article>
  </section>
  <section><h2>Past events</h2></section>
</main></body></html>
"""


def test_current_company_header_and_navigation_are_parsed() -> None:
    result = parse_company_html(HOME_HTML, {}, company_url=COMPANY_URL)
    company = result["company"]

    assert company["name"] == "Acme"
    assert company["tagline"] == "Build dependable systems."
    assert company["industry"] == "Software Development"
    assert company["product_categories"] == [{"name": "Workflow Automation", "url": None}]
    assert company["location"] == "London, England"
    assert company["followers"] == "2M"
    assert company["employee_count"] == "5K-10K"
    assert company["logo_url"] == "https://media.example/company-logo_100_100/acme.png"
    assert company["cover_image_url"] == "https://media.example/acme-cover.jpg"
    assert company["verified"] is True
    assert company["external_action"] == {
        "label": "Learn more",
        "url": "https://acme.example",
    }
    assert company["affiliated_pages"] == [{
        "name": "Acme Support",
        "url": "https://www.linkedin.com/showcase/acme-support/",
        "industry": "Software Development",
        "relationship": "Showcase page",
        "image_url": "https://media.example/support.png",
    }]
    assert result["section_status"]["services"] == "not_available"


def test_current_company_sections_keep_visible_content() -> None:
    result = parse_company_html(
        HOME_HTML,
        {
            "about": ABOUT_HTML,
            "posts": POSTS_HTML,
            "product": PRODUCT_HTML,
            "jobs": JOBS_HTML,
            "life": LIFE_HTML,
            "people": PEOPLE_HTML,
            "events": EVENTS_HTML,
        },
        company_url=COMPANY_URL,
    )
    company = result["company"]

    assert company["about"]["overview"] == "Acme makes dependable automation."
    assert company["about"]["website"] == "https://acme.example"
    assert company["about"]["company_size"] == "5,001-10,000"
    assert company["about"]["associated_members"]["count"] == 17103

    assert company["posts"]["items"][0]["post"]["author"]["name"] == "Acme"
    assert company["posts"]["items"][0]["post"]["commentary"]["text"] == (
        "A current-layout company post. Read more"
    )
    assert company["posts"]["items"][0]["post"]["media"] == [{
        "type": "image",
        "url": "https://media.example/feedshare-shrink_480/update.png",
        "alt": "View image",
    }]
    assert company["posts"]["items"][0]["post"]["commentary"]["links"] == [{
        "text": "Read more",
        "url": "https://acme.example/update",
    }]

    assert company["products"]["name"] == "Acme Flow"
    assert company["products"]["description"] == "Automates dependable work."
    assert company["products"]["media"][0]["thumbnail_url"] == (
        "https://media.example/demo.png"
    )
    assert company["products"]["top_rated_features"]["items"] == [
        {"name": "Encryption", "description": "Protects workflow data."}
    ]

    assert company["jobs"]["items"][0]["id"] == "100"
    assert company["jobs"]["items"][0]["title"] == "Platform Engineer"
    assert company["jobs"]["items"][0]["workplace_type"] == "Hybrid"
    assert company["jobs"]["items"][0]["posted_at"] == "2 days ago"

    assert company["life"]["title"] == "Careers at Acme"
    assert company["life"]["featured_members"]["items"][0]["name"] == (
        "Alex Example"
    )
    assert company["life"]["spotlights"][0]["title"] == "Creating access"
    assert company["life"]["photos"]["items"][0]["alt_text"] == "Acme team"

    assert company["people"]["analytics"]["cards"] == [
        {
            "title": "Where they live",
            "items": [
                {"label": "United Kingdom", "count": 8166},
                {"label": "India", "count": 2211},
            ],
        }
    ]
    assert company["events"]["upcoming"] == [{
        "title": "Acme Launch",
        "starts_at": "Wed, Sep 9, 2026, 5:00 PM",
        "url": "https://www.linkedin.com/events/acme-launch-123/",
        "image_url": "https://media.example/event.jpg",
        "attendee_count": 42,
    }]
    assert set(result["section_status"].values()) >= {"parsed", "not_available"}


def test_current_company_content_that_cannot_be_read_is_parse_drift() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"posts": "<html><body><main><h2>Feed post</h2></main></body></html>"},
        company_url=COMPANY_URL,
    )

    assert result["section_status"]["posts"] == "parse_failed"
    assert result["warnings"] == [{
        "code": "company_section_parse_failed",
        "section": "posts",
        "message": "LinkedIn company Posts content could not be read",
    }]
