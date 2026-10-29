import pytest

from career_lens.linkedin import dom


def test_current_carousel_traversal_collects_then_restores_and_is_bounded(monkeypatch):
    class Button:
        def __init__(self, carousel, name):
            self.carousel, self.name = carousel, name

        def count(self):
            return int(self.carousel.position < 2 if self.name == "Next" else self.carousel.position > 0)

        def is_disabled(self):
            return False

    class Carousel:
        position = 0
        stuck = False

        def evaluate(self, _script):
            return [["Card", -100 * self.position]]

        def get_by_role(self, role, *, name, exact):
            assert role == "button" and exact
            return Button(self, name)

    def click(_page, button, **_kwargs):
        if not button.carousel.stuck:
            button.carousel.position += 1 if button.name == "Next" else -1

    monkeypatch.setattr(dom.interaction, "click", click)
    monkeypatch.setattr(dom.time, "sleep", lambda _: None)
    carousel = Carousel()
    collected = []
    progress = dom._walk_carousel(None, carousel, None, collected.append)
    assert collected == [1, 2, 3]
    assert carousel.position == 0
    assert progress == {"positions_visited": 3, "positions_total": None, "reached_end": True}
    limited = dom._walk_carousel(None, carousel, None, lambda _: None, max_positions=2)
    assert limited == {"positions_visited": 2, "positions_total": None, "reached_end": False}
    assert carousel.position == 0
    carousel.stuck = True
    assert dom._walk_carousel(None, carousel, None, lambda _: None)["reached_end"] is False


def test_return_home_accepts_both_home_routes_but_not_other_tabs():
    for kind in ("company", "school", "showcase"):
        tab = dom.Tab("Home", f"/{kind}/acme/")
        for path in (f"/{kind}/acme/", f"/{kind}/acme/home/"):
            assert dom._returned_to(path, tab)
        for path in (f"/{kind}/other/home/", f"/{kind}/acme/events/",
                     f"/{kind}/acme/home/other/",
                     f"https://example.com/{kind}/acme/home/"):
            assert not dom._returned_to(path, tab)
        assert dom._returned_to(f"/{kind}/acme/", dom.Tab("Home", f"/{kind}/acme/home/"))


class _Anchor:
    def __init__(self, href, label):
        self.href = href
        self.label = label

    def get_attribute(self, name):
        assert name == "href"
        return self.href

    def inner_text(self):
        return self.label


class _Anchors:
    def __init__(self, anchors):
        self.anchors = anchors

    def count(self):
        return len(self.anchors)

    def nth(self, index):
        return self.anchors[index]


def test_company_tabs_accept_absolute_links_and_skip_explicit_home() -> None:
    class Page:
        def locator(self, selector):
            if selector == 'a[href*="/company/acme/"]':
                return _Anchors([
                    _Anchor("https://www.linkedin.com/company/acme/home/", "Home"),
                    _Anchor("https://www.linkedin.com/company/acme/about/", "About"),
                    _Anchor("https://www.linkedin.com/company/acme/posts/", "Posts"),
                ])
            return _Anchors([])

    assert dom.tabs(Page(), dom.Company("company", "acme", "")) == [
        dom.Tab("About", "/company/acme/about/"),
        dom.Tab("Posts", "/company/acme/posts/"),
    ]


def test_company_jobs_and_events_use_route_links_when_labels_are_generic(
    monkeypatch,
) -> None:
    class Control:
        def __init__(self, page, destination):
            self.page = page
            self.destination = destination

        @property
        def first(self):
            return self

        def count(self):
            return int(bool(self.destination))

        def get_attribute(self, name):
            assert name == "href"
            return self.destination

        def filter(self, *, has_text):
            assert has_text.fullmatch("Show all")
            assert not has_text.fullmatch("Platform Engineer")
            return self

        def locator(self, _selector):
            return self

    class Page:
        def __init__(self):
            self.path = "/company/acme/jobs/"

        def get_by_role(self, *_args, **_kwargs):
            if _kwargs.get("name") == "Recently posted jobs":
                return Control(self, "/jobs/search-results/?f_C=42")
            return Control(self, "")

        def locator(self, selector):
            if selector == 'a[href*="/jobs/search-results/"]':
                return Control(self, "/jobs/search-results/?f_C=42")
            if selector == 'a[href*="/company/acme/events"]':
                return Control(self, "/company/acme/events/")
            if selector == 'a[href*="/jobs/view/"]' and self.path == dom.JOBS_RESULTS_PATH:
                return Control(self, "/jobs/view/100/")
            return Control(self, "")

        def evaluate(self, script):
            assert script == dom.LOCATION
            return self.path

        def inner_text(self, selector):
            assert selector == "body"
            return "events"

    page = Page()

    def click(_page, control, timeout):
        assert timeout == 5000
        page.path = control.destination.split("?", 1)[0]

    monkeypatch.setattr(dom.interaction, "click", click)
    monkeypatch.setattr(dom.time, "sleep", lambda _seconds: None)

    assert dom.open_all_jobs(page) is True
    page.path = "/company/acme/"
    assert dom.open_all_events(page, dom.Company("company", "acme", "")) is True


def test_job_results_scroller_is_selected_by_component_identity():
    script = dom.MARK_JOB_RESULTS_SCROLLER

    assert 'componentkey="SearchResultsMainContent"' in script
    assert 'job-card-component-ref-' in script
    assert "window.innerWidth *" not in script
    assert "window.innerHeight *" not in script


def test_company_alumni_rail_is_viewer_specific_furniture():
    assert "school alumni" in dom.IRRELEVANT_RAILS


def test_job_results_content_excludes_viewer_specific_alumni_insights():
    class Page:
        def evaluate(self, script):
            assert script == dom.EXTRACT_JOB_RESULTS
            return [{
                "id": "42",
                "title": "Senior Engineer",
                "details": [
                    "Acme",
                    "London (Hybrid)",
                    "5 school alumni work here",
                    "67 company alumni work here",
                    "Be an early applicant",
                ],
            }]

    text, html = dom.job_results_content(Page())

    assert "school alumni work here" not in text
    assert b"school alumni work here" not in html
    assert "company alumni work here" not in text
    assert b"company alumni work here" not in html
    assert "Be an early applicant" in text


def test_company_events_are_empty_only_after_event_groups_render():
    class Locator:
        def __init__(self, count):
            self._count = count

        def count(self):
            return self._count

    class EventPage:
        def __init__(self, groups, cards):
            self.groups = groups
            self.cards = cards

        def locator(self, selector):
            return Locator(
                self.groups if selector == dom.COMPANY_EVENT_GROUPS
                else self.cards)

    assert dom.company_events_is_empty(EventPage(groups=2, cards=0)) is True
    assert dom.company_events_is_empty(EventPage(groups=2, cards=1)) is False
    assert dom.company_events_is_empty(EventPage(groups=0, cards=0)) is False


class State:
    def __init__(self):
        self.reads = 0

    @property
    def first(self):
        return self

    def inner_text(self):
        self.reads += 1
        return "" if self.reads == 1 else "Page 1 of 6"

    def count(self):
        return 1


class Carousel:
    def __init__(self):
        self.state = State()

    def locator(self, selector):
        assert selector == dom.CAROUSEL_STATE
        return self.state


class Carousels:
    def __init__(self, carousel, present):
        self.carousel = carousel
        self.present = present

    @property
    def first(self):
        return self.carousel

    def count(self):
        return int(self.present)


class Page:
    def __init__(self):
        self.carousel = Carousel()
        self.probes = 0

    def locator(self, selector):
        assert selector == dom.PEOPLE_CAROUSEL
        self.probes += 1
        return Carousels(self.carousel, present=self.probes > 1)


def test_people_carousel_waits_for_late_mount_and_readable_state(monkeypatch):
    page = Page()
    monkeypatch.setattr(dom.time, "sleep", lambda _seconds: None)

    carousel, state = dom._wait_for_people_carousel(page)

    assert carousel is page.carousel
    assert state == (1, 6)
    assert page.probes == 3


class PeopleContentPage:
    def __init__(self):
        self.cards = None

    def evaluate(self, script, cards):
        assert script == dom.PEOPLE_ANALYTICS_DOCUMENT
        self.cards = cards
        return {
            "html": "<html>consolidated</html>",
            "text": "People page\nPeople analytics\nWhere they live\n10 India",
        }

    def inner_text(self, selector):
        assert selector == "body"
        return "People page"


def test_people_artifact_excludes_viewer_specific_connection_analytics():
    page = PeopleContentPage()
    analytics = {
        "cards": [
            {
                "title": "Where they live",
                "values": [{"count": "10", "label": "India"}],
            },
            {
                "title": "How you are connected",
                "values": [{"count": "8", "label": "3rd+"}],
            },
        ]
    }

    captured_text, captured_html = dom.company_people_content(page, analytics)

    assert page.cards == [analytics["cards"][0]]
    assert captured_text == (
        "People page\nPeople analytics\nWhere they live\n10 India"
    )
    assert "How you are connected" not in captured_text
    assert captured_html == b"<html>consolidated</html>"


def test_people_artifact_removes_carousel_when_only_viewer_analytics_exist():
    page = PeopleContentPage()
    analytics = {
        "cards": [
            {
                "title": "How you are connected",
                "values": [{"count": "8", "label": "3rd+"}],
            },
        ]
    }

    dom.company_people_content(page, analytics)

    assert page.cards == []


class PagingState:
    def __init__(self, carousel):
        self.carousel = carousel

    @property
    def first(self):
        return self

    def inner_text(self):
        return f"Page {self.carousel.page} of {self.carousel.total}"

    def count(self):
        return 1


class PagingControl:
    def __init__(self, carousel, direction):
        self.carousel = carousel
        self.direction = direction

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_disabled(self):
        return (
            self.carousel.page >= self.carousel.total
            if self.direction == "Next"
            else self.carousel.page <= 1
        )


class PagingCarousel:
    def __init__(self, total):
        self.page = 1
        self.total = total

    def locator(self, selector):
        assert selector == dom.CAROUSEL_STATE
        return PagingState(self)

    def get_by_role(self, role, name, exact):
        assert role == "button" and exact is True
        return PagingControl(self, name)


def test_carousel_walk_is_capped_and_restores_first_position(monkeypatch):
    carousel = PagingCarousel(total=50)

    def click(_page, control, timeout):
        assert timeout == 5000
        carousel.page += 1 if control.direction == "Next" else -1

    monkeypatch.setattr(dom.interaction, "click", click)
    monkeypatch.setattr(dom.time, "sleep", lambda _seconds: None)

    seen = []
    result = dom._walk_carousel(object(), carousel, (1, 50), seen.append,
                                max_positions=20)

    assert seen == list(range(1, 21))
    assert result == {
        "positions_visited": 20,
        "positions_total": 50,
        "reached_end": False,
    }
    assert carousel.page == 1


def test_carousel_walk_reports_natural_end(monkeypatch):
    carousel = PagingCarousel(total=3)

    def click(_page, control, timeout):
        carousel.page += 1 if control.direction == "Next" else -1

    monkeypatch.setattr(dom.interaction, "click", click)
    monkeypatch.setattr(dom.time, "sleep", lambda _seconds: None)

    result = dom._walk_carousel(object(), carousel, (1, 3), lambda _page: None,
                                max_positions=20)

    assert result["positions_visited"] == 3
    assert result["positions_total"] == 3
    assert result["reached_end"] is True
    assert carousel.page == 1


class CarouselContentPage:
    def content(self):
        return """<html><body><section><h2>Featured</h2>
          <div class="artdeco-carousel"><article id="visible">Visible</article></div>
        </section></body></html>"""

    def inner_text(self, selector):
        assert selector == "body"
        return "Featured\nVisible"


def test_company_carousel_artifact_replaces_virtualized_visible_cards():
    snapshots = {
        "carousels": [{
            "label": "Featured",
            "items": [
                '<div class="artdeco-carousel__item"><article id="first">First</article></div>',
                '<div class="artdeco-carousel__item"><article id="second">Second</article></div>',
            ],
        }],
    }

    text, document = dom.company_carousel_content(
        CarouselContentPage(), snapshots)

    saved = document.decode("utf-8")
    assert "Visible" not in text
    assert "Visible" not in saved
    assert text.count("First") == 1
    assert text.count("Second") == 1
    assert saved.count('id="first"') == 1
    assert saved.count('id="second"') == 1


def test_tab_anchor_finds_absolute_navigation_links_from_the_current_layout() -> None:
    """LinkedIn's member navigation now carries absolute links and hashed classes."""
    from patchright.sync_api import sync_playwright

    html = """
      <a href="https://www.linkedin.com/company/acme/jobs/">Show all jobs</a>
      <nav><a data-view-name="pages-member-nav-jobs-tab"
              href="https://www.linkedin.com/company/acme/jobs/">Jobs</a></nav>
    """
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            anchor = dom._tab_anchor(page, dom.Tab("Jobs", "/company/acme/jobs/"))
            assert anchor is not None
            assert anchor.inner_text() == "Jobs"
            assert anchor.get_attribute("data-view-name") == "pages-member-nav-jobs-tab"
        finally:
            browser.close()


class _NoCardsPage:
    def __init__(self, says_empty):
        self.says_empty = says_empty

    def evaluate(self, script):
        if script == dom.EXTRACT_JOB_RESULTS:
            return []
        assert script == dom.JOB_RESULTS_SAY_EMPTY
        return self.says_empty


def test_job_results_that_say_none_match_are_an_empty_list():
    text, html = dom.job_results_content(_NoCardsPage(says_empty=True))

    assert text == ""
    assert b"<article" not in html
    assert b"data-career-lens-no-results" in html


def test_job_results_without_cards_or_an_empty_notice_still_fail():
    with pytest.raises(RuntimeError):
        dom.job_results_content(_NoCardsPage(says_empty=False))
