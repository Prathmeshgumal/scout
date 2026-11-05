"""Focused company JSON coverage for the Home and About tabs."""

from career_lens.linkedin.company_project import _outcome
from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card artdeco-card">
    <div class="org-cropped-image__cover-image"
         style="background-image: url(https://media.example/acme-cover.jpg);"></div>
    <img class="org-top-card-primary-content__logo" src="https://media.example/acme.png"/>
    <h1 class="org-top-card-summary__title">Acme</h1>
    <a aria-label="Verified" href="/company/acme/about/"></a>
    <p class="org-top-card-summary__tagline">Build dependable systems.</p>
    <div class="org-top-card-summary-info-list">
      <div class="org-top-card-summary-info-list__info-item">Software Development</div>
      <div class="inline-block">
        <div class="org-top-card-summary-info-list__info-item">London, England</div>
        <div class="org-top-card-summary-info-list__info-item">2M followers</div>
        <a class="org-top-card-summary-info-list__info-item"
           href="/search/results/people/?currentCompany=42">5K-10K employees</a>
      </div>
    </div>
    <div class="org-top-card-primary-actions">
      <button class="org-top-card-primary-actions__action">Follow</button>
      <a class="org-top-card-primary-actions__action"
         href="https://acme.example/careers">Learn more</a>
    </div>
  </section>
  <section class="org-newsletters-module__card-spacing">
    <h3>Newsletter</h3>
    <a href="/newsletters/123/">
      <h4>The Acme Update</h4>
      <p><span>Published monthly</span> · <span>12,345 subscribers</span></p>
      <p class="org-newsletters-module__description">What Acme is building.</p>
    </a>
  </section>
</section></main></body></html>
"""

ABOUT_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-page-details-module__card-spacing">
    <h2>Overview</h2>
    <p>Acme makes dependable automation.</p>
    <dl>
      <dt><h3>Website</h3></dt><dd><a href="https://acme.example">acme.example</a></dd>
      <dt><h3>Industry</h3></dt><dd>Software Development</dd>
      <dt><h3>Company size</h3></dt><dd>5,001-10,000 employees</dd>
      <dd><a href="/search/results/people/?currentCompany=42">17,103 associated members</a></dd>
      <dt><h3>Headquarters</h3></dt><dd>London, England</dd>
    </dl>
  </section>
</section></main>
<section data-career_lens="company-locations">
  <h2>Company locations</h2>
  <script type="application/json">[{"name":"London","description":"Headquarters","headquarters":true,"address":{"line1":"1 Main St","city":"London","country":"GB"},"latitude":51.5072,"longitude":-0.1276}]</script>
</section>
</body></html>
"""

def test_company_home_parses_header_and_newsletter_fields() -> None:
    company = parse_company_html(HOME_HTML, {}, company_url=COMPANY_URL)["company"]

    assert company["name"] == "Acme"
    assert company["industry"] == "Software Development"
    assert company["location"] == "London, England"
    assert company["followers"] == "2M"
    assert company["employee_count"] == "5K-10K"
    assert company["external_action"] == {
        "label": "Learn more",
        "url": "https://acme.example/careers",
    }
    assert company["home"]["newsletters"][0] == {
        "name": "The Acme Update",
        "url": "https://www.linkedin.com/newsletters/123/",
        "publication_frequency": "Published monthly",
        "subscriber_count": 12345,
        "description": "What Acme is building.",
        "image_url": None,
    }

def test_company_about_reads_normalized_location_records() -> None:
    company = parse_company_html(
        HOME_HTML,
        {"about": ABOUT_HTML},
        company_url=COMPANY_URL,
    )["company"]

    assert company["about"]["overview"] == "Acme makes dependable automation."
    assert company["about"]["company_size"] == "5,001-10,000"
    assert company["about"]["associated_members"] == {
        "count": 17103,
        "url": (
            "https://www.linkedin.com/search/results/people/"
            "?currentCompany=42"
        ),
        "description": None,
    }
    assert company["about"]["locations"] == [
        {
            "name": "London",
            "description": "Headquarters",
            "headquarters": True,
            "address": {"line1": "1 Main St", "city": "London", "country": "GB"},
            "latitude": 51.5072,
            "longitude": -0.1276,
        }
    ]


def test_company_section_statuses_distinguish_availability_capture_and_parse(
    monkeypatch,
) -> None:
    from career_lens.linkedin.parse import company as company_parser

    home_with_nav = HOME_HTML.replace(
        '<section class="org-newsletters-module__card-spacing">',
        """
        <nav>
          <a href="/company/acme/">Home</a>
          <a href="/company/acme/about/">About</a>
          <a href="/company/acme/posts/">Posts</a>
          <a href="/company/acme/product/">Products</a>
        </nav>
        <section class="org-newsletters-module__card-spacing">
        """,
    )

    def fail_about(*_args, **_kwargs):
        raise ValueError("about markup changed")

    monkeypatch.setattr(company_parser, "extract_about", fail_about)
    result = parse_company_html(
        home_with_nav,
        {
            "about": ABOUT_HTML,
            "posts": "<html><body><main></main></body></html>",
        },
        company_url=COMPANY_URL,
    )

    assert result["section_status"] == {
        "home": "parsed",
        "about": "parse_failed",
        "posts": "parsed",
        "products": "not_captured",
        "jobs": "not_available",
        "life": "not_available",
        "people": "not_available",
        "events": "not_available",
        "services": "not_available",
    }
    assert result["warnings"] == [
        {
            "code": "company_section_parse_failed",
            "section": "about",
            "message": "about markup changed",
        }
    ]


def test_company_without_an_events_link_has_a_complete_outcome() -> None:
    home_without_events = HOME_HTML.replace(
        '<section class="org-newsletters-module__card-spacing">',
        """
        <nav><a href="/company/acme/">Home</a></nav>
        <section class="org-newsletters-module__card-spacing">
        """,
    )

    result = parse_company_html(
        home_without_events,
        {},
        company_url=COMPANY_URL,
    )

    assert result["section_status"]["events"] == "not_available"
    assert _outcome(result) == "ok"


def test_unselected_company_sections_are_not_requested() -> None:
    result = parse_company_html(
        HOME_HTML, {}, company_url=COMPANY_URL,
        requested_sections=frozenset({"people"}),
    )

    assert result["section_status"]["home"] == "not_requested"
    assert result["section_status"]["about"] == "not_requested"
    assert result["section_status"]["people"] == "not_captured"
    assert "home" not in result["company"]
    assert "home" not in result["sources"]
    assert result["sources"]["company"] == {
        "artifact": None, "url": COMPANY_URL,
    }
    assert _outcome(result) == "partial"


def test_captured_but_unrequested_company_tabs_are_ignored() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"about": ABOUT_HTML},
        company_url=COMPANY_URL,
        requested_sections=frozenset({"people"}),
    )

    assert result["section_status"]["about"] == "not_requested"
    assert "about" not in result["company"]
    assert "about" not in result["sources"]


def test_company_events_remain_not_captured_when_home_links_to_events() -> None:
    home_with_events = HOME_HTML.replace(
        '<section class="org-newsletters-module__card-spacing">',
        """
        <nav><a href="/company/acme/">Home</a></nav>
        <a href="/company/acme/events/">Show all events</a>
        <section class="org-newsletters-module__card-spacing">
        """,
    )

    result = parse_company_html(
        home_with_events,
        {},
        company_url=COMPANY_URL,
    )

    assert result["section_status"]["events"] == "not_captured"
    assert _outcome(result) == "partial"
