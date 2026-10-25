"""Focused contracts for optional LinkedIn company Events and Services pages."""

from career_lens.linkedin.parse.company import parse_company_html


COMPANY_URL = "https://www.linkedin.com/company/acme/"
HOME = """
<html><body><nav>
  <a href="/company/acme/">Home</a>
  <a href="/company/acme/services/">Services</a>
</nav><section class="org-top-card"><h1>Acme</h1></section></body></html>
"""
EVENTS = """
<html><body><main>
  <section><h2>Upcoming events</h2>
    <div class="artdeco-empty-state">No upcoming events</div>
  </section>
  <section><h2>Past events</h2>
    <article class="org-events__list-item--event-card">
      <a class="events-components-shared-event-card" href="/events/acme-launch-42/">
        <img class="events-components-shared-event-card__background-image"
             src="https://media.example/event.jpg"/>
        <time class="events-components-shared-event-card__text-single-line">Fri, Nov 28, 2025, 7:30 PM</time>
        <h3 class="events-components-shared-event-card__event-title">Acme launch</h3>
        <div class="events-components-shared-event-card__insights">
          <span class="events-components-shared-social-proof__copy-text">132 attendees</span>
        </div>
      </a>
    </article>
  </section>
</main></body></html>
"""
SERVICES = """
<html><body><main class="organization-service-pages__main-content">
  <section><h2>About us</h2><p>Acme builds dependable brands.</p></section>
  <a class="service-pages-about-services-card-primary-cta__trigger-js"
     href="/company/acme/services/request-proposal/?isSecondary=false">
    <span aria-hidden="true">Request services</span>
    <span class="visually-hidden">Request services</span>
  </a>
  <section>
    <p><span aria-hidden="true">Available</span><span class="visually-hidden">Available</span></p>
    <p><span aria-hidden="true">Remote</span><span class="visually-hidden">Remote</span></p>
  </section>
  <section>
    <p><span aria-hidden="true">Pricing</span><span class="visually-hidden">Pricing</span></p>
    <p><span aria-hidden="true">Contact for pricing</span><span class="visually-hidden">Contact for pricing</span></p>
  </section>
  <section><h2>Services provided</h2>
    <div class="service-pages-services-provided-card__service-pills">
      <a class="service-pages-services-provided-card__service-pills-link-light"
         href="/search/results/services/?serviceCategory=1">Brand Marketing</a>
      <a class="service-pages-services-provided-card__service-pills-link-light"
         href="/search/results/services/?serviceCategory=2">Content Strategy</a>
    </div>
  </section>
</main></body></html>
"""


def test_company_events_preserve_upcoming_and_past_groups() -> None:
    result = parse_company_html(
        HOME, {"events": EVENTS}, company_url=COMPANY_URL,
        tab_urls={"events": f"{COMPANY_URL}events/"},
    )

    assert result["section_status"]["events"] == "parsed"
    assert result["company"]["events"] == {
        "upcoming": [],
        "past": [{
            "title": "Acme launch",
            "starts_at": "Fri, Nov 28, 2025, 7:30 PM",
            "url": "https://www.linkedin.com/events/acme-launch-42/",
            "image_url": "https://media.example/event.jpg",
            "attendee_count": 132,
        }],
    }


def test_current_events_preserve_nested_cards_and_group_boundaries():
    events = '''<html><body><main><section>
      <div><h2>No upcoming events planned</h2><a href="/events/">Explore events</a></div>
      <div><div><h2>Past</h2></div>
        <a href="/events/100/">
          <div><a href="/events/100/"><img src="https://media.example/event.jpg"></a>
            <div><p>Fri, Nov 28, 7:30 PM</p>
              <a href="/events/100/"><div><p>Acme launch</p></div></a>
              <p>Online</p><p>132 attendees</p>
            </div>
          </div>
        </a>
        <a href="/events/200/"><div><p>Wed, May 7, 9:25 PM</p>
          <a href="/events/200/"><p>Product keynote</p></a><p>366 attendees</p>
        </div></a>
      </div>
    </section></main></body></html>'''
    parsed = parse_company_html(HOME, {"events": events}, company_url=COMPANY_URL)
    value = parsed["company"]["events"]
    assert value["upcoming"] == []
    assert [item["title"] for item in value["past"]] == ["Acme launch", "Product keynote"]
    assert [item["attendee_count"] for item in value["past"]] == [132, 366]
    assert value["past"][0]["starts_at"] == "Fri, Nov 28, 7:30 PM"
    assert value["past"][0]["image_url"] == "https://media.example/event.jpg"


def test_unreadable_event_card_reports_drift():
    value = parse_company_html(HOME, {"events": '''<main><div><h2>Past</h2>
        <a href="/events/100/"><img src="https://media.example/cover.jpg"></a>
        </div></main>'''}, company_url=COMPANY_URL)
    assert value["section_status"]["events"] == "parse_failed"


def test_explicitly_empty_events_are_not_parse_drift():
    value = parse_company_html(HOME, {"events": '''<main>
        <h2>No upcoming events planned</h2><a href="/events/">Explore events</a>
        </main>'''}, company_url=COMPANY_URL)
    assert value["company"]["events"] == {"upcoming": [], "past": []}
    assert value["section_status"]["events"] == "parsed"


def test_company_services_parse_only_the_service_page_content() -> None:
    result = parse_company_html(
        HOME, {"services": SERVICES}, company_url=COMPANY_URL,
        tab_urls={"services": f"{COMPANY_URL}services/"},
    )

    assert result["section_status"]["services"] == "parsed"
    assert result["company"]["services"] == {
        "overview": "Acme builds dependable brands.",
        "request_action": {
            "label": "Request services",
            "url": (
                "https://www.linkedin.com/company/acme/services/"
                "request-proposal/?isSecondary=false"
            ),
        },
        "availability": "Remote",
        "pricing": "Contact for pricing",
        "services": [
            {
                "name": "Brand Marketing",
                "url": "https://www.linkedin.com/search/results/services/?serviceCategory=1",
            },
            {
                "name": "Content Strategy",
                "url": "https://www.linkedin.com/search/results/services/?serviceCategory=2",
            },
        ],
    }
