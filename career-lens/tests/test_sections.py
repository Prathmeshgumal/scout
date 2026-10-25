"""Section discovery, and the vocabulary it shares with the parser."""

from __future__ import annotations

from patchright.sync_api import TimeoutError as BrowserTimeoutError

import pytest

from career_lens.linkedin import sections
from career_lens.linkedin.schema import SECTION_NAMES


@pytest.mark.parametrize("stalled", [False, True])
def test_featured_reveals_hidden_link_with_bounded_carousel_clicks(monkeypatch, stalled):
    from unittest.mock import Mock

    from career_lens.capture import pace

    card, target, control = Mock(), Mock(), Mock()
    card.bounding_box.return_value = {"x": 100, "width": 800}
    target.bounding_box.return_value = {"x": 1400, "width": 200}
    card.locator.return_value.first = control
    control.count.return_value = 1
    control.is_visible.return_value = True
    monkeypatch.setattr(sections, "_profile_card", lambda *args: (Mock(), card))
    monkeypatch.setattr(pace, "sleep_with_reason", lambda *args: None)
    clicks = []

    def click(page, locator, **kwargs):
        assert locator is control
        clicks.append(locator)
        if not stalled:
            target.bounding_box.return_value = {"x": 600, "width": 200}

    monkeypatch.setattr(sections.interaction, "click", click)
    if stalled:
        with pytest.raises(RuntimeError, match="outside the carousel viewport"):
            sections._reveal_featured_link(Mock(), target)
        assert len(clicks) == sections.MAX_FEATURED_ADVANCES
    else:
        sections._reveal_featured_link(Mock(), target)
        assert len(clicks) == 1
        sections._reveal_featured_link(Mock(), target)
        assert len(clicks) == 1


@pytest.fixture(autouse=True)
def settled_profile_cards(monkeypatch, request):
    # These orchestration fakes model loaded pages with detail links. Loading
    # and inline-only card behavior are exercised in test_profile_loading.py.
    if request.node.get_closest_marker("browser") is None:
        monkeypatch.setattr(sections, "wait_for_profile_loading", lambda page: None)
        monkeypatch.setattr(sections, "capture_inline_profile_section", lambda *args: None)


@pytest.fixture
def browser_page(tmp_path):
    """A real browser, for the one check here that must run as page script.

    Marked `browser` at the test rather than the module: everything else in
    this file runs against fakes and must stay runnable without a Chrome.
    """
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "profile"), channel="chrome",
            headless=True)
        try:
            yield context.new_page()
        finally:
            context.close()


def test_every_captured_section_name_is_one_the_schema_knows():
    # The capture layer and the parser must agree on these strings: a capture's
    # `role` becomes a key in the parser's `sections` mapping directly, with no
    # translation. A name in one and not the other is a section that silently
    # never reaches extraction.
    captured = set(sections.SECTIONS) | {
        sections.ACTIVITY, "activity-comments", "activity-articles",
        "activity-reactions",
        "about-this-member", "recommendations-given",
        # Captured off the profile page rather than through a link, the way
        # Featured is on a profile that offers no page for it.
        sections.CAUSES,
    }
    assert captured <= set(SECTION_NAMES), (
        f"captured but unknown to the schema: {captured - set(SECTION_NAMES)}")


def test_every_section_the_parser_reports_on_is_one_the_schema_knows():
    # The projection writes a person_section_status row for every key in the
    # parser's section_status, so a name the parser reports and the schema has
    # not heard of lands in the database as an undocumented string. Wider than
    # the capture check above, which only knows about names the capture layer
    # produces -- the parser also reports on sections read off the profile.
    from career_lens.linkedin.parse.parser import CAPTURE_NAMES

    assert set(CAPTURE_NAMES) <= set(SECTION_NAMES), (
        f"reported but unknown to the schema: "
        f"{set(CAPTURE_NAMES) - set(SECTION_NAMES)}")


def test_expand_captures_causes_when_it_is_wanted(monkeypatch, tmp_path):
    class Page:
        url = "https://www.linkedin.com/in/jane/"

    causes = sections.SectionCapture(
        "causes", sections.PageCapture(url_requested=Page.url, ok=True))
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: [])
    monkeypatch.setattr(sections, "capture_about_member",
                        lambda *_args: None)
    monkeypatch.setattr(sections, "capture_featured", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_inline_recommendations",
                        lambda *_args: iter(()))
    monkeypatch.setattr(sections, "capture_causes", lambda _page: causes)
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)

    config = sections.ExpandConfig(
        scratch_dir=tmp_path, requested_sections=frozenset({"causes"}))

    assert [item.section for item in sections.expand(Page(), config, "jane")] \
        == ["causes"]


def test_expand_skips_causes_when_it_was_declined(monkeypatch, tmp_path):
    class Page:
        url = "https://www.linkedin.com/in/jane/"

    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: [])
    monkeypatch.setattr(sections, "capture_about_member", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_contact_info", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_open_to_work", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_featured", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_inline_recommendations",
                        lambda *_args: iter(()))
    monkeypatch.setattr(
        sections, "capture_causes",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("causes were not requested")),
    )
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)

    config = sections.ExpandConfig(
        scratch_dir=tmp_path, requested_sections=frozenset({"experience"}))

    assert list(sections.expand(Page(), config, "jane")) == []


def test_causes_is_captured_as_the_card_it_is(monkeypatch):
    """A capture with its own HTML, screenshot and text -- not a page.

    LinkedIn offers no causes page: the card sits on the profile with no
    "Show all" behind it. That is the Featured case, and Featured is stored as
    the card where it stands rather than left as a field on the profile blob.
    """
    shot = b"png-bytes"

    class Card:
        def inner_text(self): return "Causes\nArts and Culture • Education"
        def evaluate(self, _script): return "<section><h2>Causes</h2></section>"
        def screenshot(self, **_kwargs): return shot

    class Heading:
        first = None
        def count(self): return 1
        def locator(self, _selector): return Card()

    class Page:
        url = "https://www.linkedin.com/in/jane/"
        def title(self): return "Jane Doe | LinkedIn"
        def locator(self, _selector): return self
        def get_by_role(self, _role, **_kwargs):
            heading = Heading()
            heading.first = heading
            return heading

    monkeypatch.setattr(sections.interaction, "scroll_to_target",
                        lambda *_args, **_kwargs: None)

    captured = sections.capture_causes(Page())

    assert captured is not None
    assert captured.section == "causes"
    assert captured.capture.ok
    # The three artifacts an operator opens the capture to see.
    assert captured.capture.html_bytes == b"<section><h2>Causes</h2></section>"
    assert captured.capture.screenshot_bytes == [shot]
    assert "Arts and Culture" in captured.capture.text
    # No navigation happened, so the profile is where it was read from.
    assert captured.capture.url_requested == "https://www.linkedin.com/in/jane/"


def test_a_profile_with_no_causes_card_yields_no_causes_capture(monkeypatch):
    class Heading:
        def count(self): return 0

    class Page:
        url = "https://www.linkedin.com/in/jane/"
        def locator(self, _selector): return self
        def get_by_role(self, _role, **_kwargs): return Heading()

    assert sections.capture_causes(Page()) is None


def test_a_details_url_resolves_to_its_handle_and_section():
    assert sections.parse_details(
        "https://www.linkedin.com/in/jane-doe/details/experience/"
    ) == ("jane-doe", "experience")


def test_new_profile_detail_routes_use_canonical_section_names():
    assert sections.parse_details(
        "https://www.linkedin.com/in/jane-doe/details/volunteering-experiences/"
    ) == ("jane-doe", "volunteering")
    assert sections.parse_details(
        "https://www.linkedin.com/in/jane-doe/details/test-scores/"
    ) == ("jane-doe", "test-scores")


def test_a_services_page_link_is_a_person_services_section():
    links = sections.links_from_hrefs([
        (
            "/services/page/400a9931403891b076/",
            "https://www.linkedin.com/services/page/400a9931403891b076/",
        ),
        (
            "/services/page/400a9931403891b076/request-proposal/",
            "https://www.linkedin.com/services/page/400a9931403891b076/request-proposal/",
        ),
    ])

    assert links == [sections.SectionLink(
        name="services",
        url="https://www.linkedin.com/services/page/400a9931403891b076/",
        href="/services/page/400a9931403891b076/",
    )]


def test_a_services_page_can_recover_to_its_origin_when_history_is_stale():
    origin = "https://www.linkedin.com/in/jane-doe/"

    class Page:
        url = "https://www.linkedin.com/services/page/400a9931403891b076/"

        def __init__(self):
            self.visited = []

        def go_back(self, **_kwargs):
            # A stale SPA history entry leaves the service page in place.
            return None

        def goto(self, url, **_kwargs):
            self.visited.append(url)
            self.url = url

        def wait_for_function(self, *_args, **_kwargs):
            return None

    page = Page()

    sections.back_to_profile(page, origin)

    assert page.visited == [origin]
    assert page.url == origin


def test_a_details_url_for_an_unlisted_section_is_refused():
    # An allowlist, so a details/ route LinkedIn invents cannot enter a run
    # before someone has looked at what it does.
    assert sections.parse_details(
        "https://www.linkedin.com/in/jane-doe/details/volunteering/") is None


def test_the_three_near_miss_urls_are_all_rejected():
    # Each of these appears on a real profile and each defeats a looser test.
    for url in (
        "https://www.linkedin.com/in/p/overlay/1/skill-associations-details/",
        "https://www.linkedin.com/in/p/opportunities/volunteering/details/?profileId=1",
        "https://www.linkedin.com/in/p/overlay/browsemap-recommendations/",
    ):
        assert sections.parse_details(url) is None, url


def test_a_profile_url_is_recognised_and_a_company_url_is_not():
    assert sections.is_profile("https://www.linkedin.com/in/jane-doe/")
    assert not sections.is_profile("https://www.linkedin.com/company/stripe/")


def test_the_handle_comes_off_a_profile_url():
    assert sections.profile_handle(
        "https://www.linkedin.com/in/jane-doe/") == "jane-doe"


def test_reactions_are_an_activity_view_with_a_parser_role():
    url = "https://www.linkedin.com/in/jane-doe/recent-activity/reactions/"
    assert sections.parse_activity(url) == ("jane-doe", "reactions")
    assert "Reactions" in sections.ACTIVITY_VIEWS


def test_section_pacing_uses_the_capture_package_after_the_port(tmp_path):
    """Local imports must not retain the old package layout."""
    config = sections.ExpandConfig(scratch_dir=tmp_path)
    page = type("Page", (), {"url": "https://example.com/"})()

    assert list(sections.expand(page, config, "example")) == []
    # A disabled dwell returns before reading the capture, but only after its
    # local pacing import has resolved.
    sections._dwell(None, config)


def test_selective_capture_opens_only_requested_profile_sections(
    monkeypatch, tmp_path
):
    opened = []
    links = [
        sections.SectionLink(
            name="experience",
            url="https://www.linkedin.com/in/jane/details/experience/",
            href="/in/jane/details/experience/",
        ),
        sections.SectionLink(
            name="education",
            url="https://www.linkedin.com/in/jane/details/education/",
            href="/in/jane/details/education/",
        ),
    ]

    class Page:
        url = "https://www.linkedin.com/in/jane/"

    capture = sections.PageCapture(url_requested=links[0].url, ok=True)
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: links)
    monkeypatch.setattr(
        sections, "capture_about_member",
        lambda *_args: (_ for _ in ()).throw(AssertionError("about was not requested")),
    )
    monkeypatch.setattr(
        sections, "capture_inline_recommendations",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("recommendations were not requested")
        ),
    )
    monkeypatch.setattr(
        sections, "capture_featured",
        lambda *_args: (_ for _ in ()).throw(AssertionError("featured was not requested")),
    )
    monkeypatch.setattr(
        sections, "open_link",
        lambda _page, link: opened.append(link.name),
    )
    monkeypatch.setattr(sections, "wait_for_content", lambda *_args, **_kwargs: None)
    # Loading the list is a real page operation, and this Page is a stand-in
    # with two attributes. What is under test is which sections get opened;
    # tests/test_lazy_lists.py drives exhaust_list itself.
    monkeypatch.setattr(sections, "exhaust_list", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(sections, "capture_page", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(sections, "_capture_tabs", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)
    monkeypatch.setattr(sections, "back_to_profile", lambda _page: None)
    monkeypatch.setattr(
        sections, "_capture_activity",
        lambda *_args: (_ for _ in ()).throw(AssertionError("activity was not requested")),
    )

    config = sections.ExpandConfig(
        scratch_dir=tmp_path, requested_sections=frozenset({"experience"})
    )
    result = list(sections.expand(Page(), config, "jane"))

    assert opened == ["experience"]
    assert [item.section for item in result] == ["experience"]


@pytest.mark.parametrize(
    ("heading", "section_name", "tabs", "details_by_tab", "expected_tab"),
    [
        (
            "Interests",
            "interests",
            ("Top Voices", "Companies", "Creator Programs"),
            {
                "Companies": (
                    "/in/jane/details/interests/"
                    "?initialTabId=interest_companies"
                ),
            },
            "Companies",
        ),
        (
            "Recommendations",
            "recommendations",
            ("Received", "Given"),
            {
                "Given": (
                    "/in/jane/details/recommendations/"
                    "?detailScreenTabIndex=1"
                ),
            },
            "Given",
        ),
    ],
)
def test_tabbed_profile_card_discovers_show_all_on_a_later_tab(
        monkeypatch, heading, section_name, tabs, details_by_tab,
        expected_tab):
    """A short default tab can hide the details link a longer tab offers.

    Labels are discovered from the card rather than listed by the driver, so a
    new category such as Creator Programs does not require capture code.
    """
    page = _TabbedProfilePage(
        heading=heading, tabs=tabs, details_by_tab=details_by_tab)
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    link = sections.find_tabbed_details_link(page, heading, section_name)

    assert link is not None
    assert link.name == section_name
    assert link.href == details_by_tab[expected_tab]
    assert page.clicked_tabs == [expected_tab]


def test_inline_interests_capture_every_discovered_tab_when_none_has_show_all(
        monkeypatch):
    page = _TabbedProfilePage(
        heading="Interests",
        tabs=("Top Voices", "Companies", "Creator Programs"),
        details_by_tab={},
        content={
            "Top Voices": "Ada Lovelace",
            "Companies": "Analytical Engines",
            "Creator Programs": "Engine Builders",
        },
    )
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())
    monkeypatch.setattr(
        sections.interaction, "scroll_to_target",
        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)

    captured = list(sections.capture_inline_interests(page))

    assert [item.section for item in captured] == [
        "interests", "interests-companies", "interests-creator-programs"]
    assert [item.capture.text for item in captured] == [
        "Interests\nTop Voices\nAda Lovelace",
        "Interests\nCompanies\nAnalytical Engines",
        "Interests\nCreator Programs\nEngine Builders",
    ]
    assert page.clicked_tabs == ["Companies", "Creator Programs"]


def test_tabbed_discovery_restores_the_initial_tab_when_no_details_page_exists(
        monkeypatch):
    page = _TabbedProfilePage(
        heading="Interests",
        tabs=("Top Voices", "Companies", "Creator Programs"),
        details_by_tab={},
    )
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    assert sections.find_tabbed_details_link(
        page, "Interests", "interests") is None
    assert page.selected == "Top Voices"
    assert page.clicked_tabs == [
        "Companies", "Creator Programs", "Top Voices"]


def test_expand_uses_a_details_link_revealed_by_an_inline_tab(
        monkeypatch, tmp_path):
    page = _TabbedProfilePage(
        heading="Interests",
        tabs=("Top Voices", "Companies"),
        details_by_tab={
            "Companies": "/in/jane/details/interests/?initialTabId=companies",
        },
    )
    link_capture = sections.PageCapture(
        url_requested=page.details_by_tab["Companies"], ok=True)
    other_capture = sections.PageCapture(
        url_requested=page.details_by_tab["Companies"], ok=True)
    opened = []

    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: [])
    monkeypatch.setattr(sections, "capture_inline_interests", lambda *_args: (
        _ for _ in ()).throw(AssertionError("details page should own capture")))
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    def open_section(_page, link, _config):
        opened.append(link)
        _page.url = link.url
        return link, None

    monkeypatch.setattr(sections, "_open_section", open_section)
    monkeypatch.setattr(sections, "wait_for_content", lambda *_args: None)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        sections, "capture_page", lambda *_args, **_kwargs: link_capture)
    monkeypatch.setattr(
        sections, "_capture_interest_tabs",
        lambda *_args: iter((sections.SectionCapture(
            "interests-top-voices", other_capture),)))
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)
    monkeypatch.setattr(
        sections, "back_to_profile",
        lambda page: setattr(page, "url", _TabbedProfilePage.url))

    result = list(sections.expand(
        page,
        sections.ExpandConfig(
            scratch_dir=tmp_path,
            requested_sections=frozenset({"interests"}),
        ),
        "jane",
    ))

    assert [item.section for item in result] == [
        "interests", "interests-top-voices"]
    assert [link.name for link in opened] == ["interests"]
    assert page.clicked_tabs == ["Companies"]


def test_recommendations_details_return_to_received_when_given_revealed_link(
        monkeypatch):
    page = _DetailTabsPage(("Received", "Given"), selected="Given")
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())
    monkeypatch.setattr(sections, "wait_for_content", lambda *_args, **_kwargs: None)

    switched, unrendered = sections.select_primary_detail_tab(
        page, "recommendations")

    assert switched
    assert unrendered is None
    assert page.selected == "Received"
    assert page.clicked_tabs == ["Received"]


class _TabbedProfilePage:
    url = "https://www.linkedin.com/in/jane/"

    def __init__(self, *, heading, tabs, details_by_tab, content=None):
        self.heading = heading
        self.tabs = list(tabs)
        self.details_by_tab = details_by_tab
        self.content = content or {tab: tab for tab in tabs}
        self.selected = self.tabs[0]
        self.clicked_tabs = []

    def locator(self, selector):
        assert selector == "main", selector
        return _TabbedMain(self)

    def title(self):
        return "Jane | LinkedIn"


class _TabbedMain:
    def __init__(self, page):
        self.page = page

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("heading", self.page.heading, True)
        return _TabbedHeading(self.page)


class _TabbedHeading:
    def __init__(self, page):
        self.page = page

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def locator(self, selector):
        assert selector == sections.PROFILE_CARD, selector
        return _TabbedCard(self.page)


class _TabbedCard:
    def __init__(self, page):
        self.page = page

    def locator(self, selector):
        if selector == "[role='radio']":
            return _TabbedLocators([
                _TabbedRadio(self.page, label) for label in self.page.tabs
            ])
        if selector == "a[href*='/details/']":
            href = self.page.details_by_tab.get(self.page.selected)
            return _TabbedLocators([_TabbedAnchor(href)] if href else [])
        raise AssertionError(selector)

    def get_by_role(self, role, *, name, exact):
        assert role == "radio" and exact
        return _TabbedRadio(self.page, name, present=name in self.page.tabs)

    def inner_text(self):
        return (
            f"Interests\n{self.page.selected}\n"
            f"{self.page.content[self.page.selected]}"
        )

    def evaluate(self, _script):
        return f"<section>{self.inner_text()}</section>"

    def screenshot(self, **_kwargs):
        return b"png"


class _TabbedLocators:
    def __init__(self, items):
        self.items = items

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]


class _TabbedRadio:
    def __init__(self, page, label, *, present=True):
        self.page = page
        self.label = label
        self.present = present

    @property
    def first(self):
        return self

    def count(self):
        return int(self.present)

    def inner_text(self):
        return self.label

    def get_attribute(self, name):
        assert name == "aria-checked"
        return str(self.page.selected == self.label).lower()

    def click(self):
        self.page.clicked_tabs.append(self.label)
        self.page.selected = self.label


class _TabbedAnchor:
    def __init__(self, href):
        self.href = href

    def get_attribute(self, name):
        assert name == "href"
        return self.href


class _DetailTabsPage:
    def __init__(self, tabs, *, selected):
        self.tabs = list(tabs)
        self.selected = selected
        self.clicked_tabs = []

    def get_by_role(self, role, *, name, exact):
        assert role == "radio" and exact
        return _TabbedRadio(self, name, present=name in self.tabs)

    def evaluate(self, _script):
        return len(self.selected)


def test_opening_a_section_recovers_the_linkedin_error_before_returning(monkeypatch):
    order = []

    class Target:
        first = None

        def __init__(self):
            self.first = self

        def scroll_into_view_if_needed(self):
            pass

        def click(self):
            order.append("click")

    class Navigation:
        value = type("Response", (), {"status": 200})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    class Page:
        def locator(self, _selector):
            return Target()

        def expect_navigation(self, **_kwargs):
            return Navigation()

    monkeypatch.setattr(
        sections, "recover_transient_error",
        lambda _page: order.append("recover"))
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    link = sections.SectionLink(
        name="experience",
        url="https://www.linkedin.com/in/jane/details/experience/",
        href="/in/jane/details/experience/",
    )
    assert sections.open_link(Page(), link).status == 200
    assert order == ["recover", "click", "recover"]


def test_opening_a_section_accepts_a_client_side_route_after_navigation_timeout(
        monkeypatch):
    link = sections.SectionLink(
        name="experience",
        url="https://www.linkedin.com/in/jane/details/experience/",
        href="/in/jane/details/experience/",
    )

    class Target:
        first = None

        def __init__(self, page):
            self.first = self
            self.page = page

        def click(self):
            self.page.url = link.url

    class Navigation:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            raise BrowserTimeoutError("navigation event was not observed")

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def locator(self, _selector):
            return Target(self)

        def expect_navigation(self, **_kwargs):
            return Navigation()

    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    assert sections.open_link(Page(), link) is None


def test_opening_activity_retries_when_a_click_does_not_leave_the_profile(
        monkeypatch):
    link = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/",
        href="/in/jane/recent-activity/all/",
    )

    class Target:
        first = None

        def __init__(self, page):
            self.first = self
            self.page = page

        def click(self):
            self.page.clicks += 1
            if self.page.clicks == 2:
                self.page.url = link.url

    class Navigation:
        value = type("Response", (), {"status": 200})()

        def __init__(self, page):
            self.page = page

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            if self.page.clicks == 1:
                raise BrowserTimeoutError("first click did not navigate")

    class Page:
        url = "https://www.linkedin.com/in/jane/"
        clicks = 0

        def locator(self, _selector):
            return Target(self)

        def expect_navigation(self, **_kwargs):
            return Navigation(self)

    page = Page()
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda _page, target, **_kwargs: target.click())

    response = sections.open_link(page, link, attempts=2)

    assert response.status == 200
    assert page.clicks == 2
    assert page.url == link.url


@pytest.mark.parametrize(
    "first_failure",
    [
        BrowserTimeoutError(
            "saved section control disappeared after returning · matches=0"),
        RuntimeError(
            "wheel scrolling did not move the target · matches=1"),
    ],
    ids=("control-remounted", "target-scroll-stalled"),
)
def test_a_failed_section_open_refreshes_and_reacquires_the_live_control(
        monkeypatch, tmp_path, first_failure):
    """The initial capture proves a section exists, but LinkedIn can remount
    the profile after Back without that anchor or with a broken scroll owner.
    Recovery must refresh the profile and use the control it serves now, whose
    query parameters may differ from the originally discovered href.
    """
    original = sections.SectionLink(
        name="skills",
        url="https://www.linkedin.com/in/jane/details/skills/?old=1",
        href="/in/jane/details/skills/?old=1",
    )
    current = sections.SectionLink(
        name="skills",
        url="https://www.linkedin.com/in/jane/details/skills/?current=1",
        href="/in/jane/details/skills/?current=1",
    )
    calls = []

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def reload(self, **kwargs):
            calls.append(("reload", kwargs))

        def wait_for_function(self, _script, **_kwargs):
            return None

    page = Page()
    discoveries = iter(([original], [current]))

    def open_link(_page, link):
        calls.append(("open", link.href))
        if link is original:
            raise first_failure
        return None

    def hydrate(_page, *_args, **kwargs):
        calls.append(("hydrate", kwargs))
        return type("Sweep", (), {"pixels": 2400, "stopped": "bottom"})()

    capture = sections.PageCapture(url_requested=current.url, ok=True)
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: next(discoveries))
    monkeypatch.setattr(sections, "open_link", open_link)
    monkeypatch.setattr(sections, "wait_for_content", lambda *_args: None)
    monkeypatch.setattr(sections, "scroll_through", hydrate)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(sections, "exhaust_list", lambda *_args: 0)
    monkeypatch.setattr(
        sections, "capture_page", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(sections, "capture_about_member", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_contact_info", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_open_to_work", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_causes", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_featured", lambda *_args: None)
    monkeypatch.setattr(
        sections, "capture_inline_recommendations", lambda *_args: iter(()))
    monkeypatch.setattr(
        sections, "find_tabbed_details_link", lambda *_args: None)
    monkeypatch.setattr(
        sections, "capture_inline_interests", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_capture_tabs", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_capture_activity", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)

    result = list(sections.expand(
        page, sections.ExpandConfig(scratch_dir=tmp_path), "jane"))

    assert [item.section for item in result] == ["skills"]
    assert result[0].capture.ok
    assert [(kind, value) for kind, value in calls if kind == "open"] == [
        ("open", original.href), ("open", current.href)]
    assert [kind for kind, _value in calls].count("reload") == 1
    assert [kind for kind, _value in calls].count("hydrate") == 1


def test_a_failed_refresh_reports_both_section_open_causes(monkeypatch, tmp_path):
    link = sections.SectionLink(
        name="skills",
        url="https://www.linkedin.com/in/jane/details/skills/",
        href="/in/jane/details/skills/",
    )
    page = type("Page", (), {
        "url": "https://www.linkedin.com/in/jane/",
        "is_closed": lambda self: False,
    })()
    monkeypatch.setattr(
        sections, "open_link",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("first click failed")))
    monkeypatch.setattr(
        sections, "_refresh_section_control",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("refresh failed")))

    with pytest.raises(RuntimeError) as raised:
        sections._open_section(
            page, link, sections.ExpandConfig(scratch_dir=tmp_path))

    assert "first click failed" in str(raised.value)
    assert "after profile refresh" in str(raised.value)
    assert "refresh failed" in str(raised.value)


def test_activity_offered_initially_survives_a_late_profile_remount(
        monkeypatch, tmp_path):
    """The profile capture proved Activity existed, but returning from the
    final finite section remounted a profile with no activity anchors.  The
    remembered section must get the same bounded refresh recovery as a saved
    finite-section control rather than disappearing from the run.
    """
    original = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/?old=1",
        href="/in/jane/recent-activity/all/?old=1",
    )
    current = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/?current=1",
        href="/in/jane/recent-activity/all/?current=1",
    )

    class Page:
        url = "https://www.linkedin.com/in/jane/"
        remounted = False

    page = Page()

    def find_activity(_page):
        return None if page.remounted else original

    def remount_profile(*_args):
        page.remounted = True
        return None

    def refresh(_page, link, _config, _profile_url=None):
        assert link is original
        page.remounted = False
        return current

    def open_activity(_page, link, *, attempts):
        assert link is current
        assert attempts == 2
        page.url = current.url
        return None

    capture = sections.PageCapture(url_requested=current.url, ok=True)
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(sections, "find_links", lambda _page: [])
    monkeypatch.setattr(sections, "find_activity", find_activity)
    monkeypatch.setattr(sections, "capture_about_member", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_contact_info", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_open_to_work", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_causes", lambda *_args: None)
    monkeypatch.setattr(sections, "capture_featured", remount_profile)
    monkeypatch.setattr(
        sections, "capture_inline_recommendations", lambda *_args: iter(()))
    monkeypatch.setattr(
        sections, "find_tabbed_details_link", lambda *_args: None)
    monkeypatch.setattr(
        sections, "capture_inline_interests", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_refresh_section_control", refresh)
    monkeypatch.setattr(sections, "open_link", open_activity)
    monkeypatch.setattr(sections, "wait_for_feed", lambda *_args: None)
    monkeypatch.setattr(
        sections, "_capture_feed",
        lambda *_args, **_kwargs: sections.SectionCapture(
            sections.ACTIVITY, capture))
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)
    monkeypatch.setattr(sections, "_back_from_activity", lambda *_args: None)
    monkeypatch.setattr(sections, "ACTIVITY_TABS", {})

    result = list(sections.expand(
        page, sections.ExpandConfig(scratch_dir=tmp_path), "jane"))

    assert [item.section for item in result] == [sections.ACTIVITY]


def test_refreshing_an_activity_control_rediscovers_activity_not_details(
        monkeypatch, tmp_path):
    original = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/?old=1",
        href="/in/jane/recent-activity/all/?old=1",
    )
    current = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/?current=1",
        href="/in/jane/recent-activity/all/?current=1",
    )

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def reload(self, **_kwargs):
            return None

        def wait_for_function(self, *_args, **_kwargs):
            return None

    sweep = type("Sweep", (), {"pixels": 2400, "stopped": "bottom"})()
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(
        sections, "scroll_through", lambda *_args, **_kwargs: sweep)
    monkeypatch.setattr(sections, "find_activity", lambda _page: current)
    monkeypatch.setattr(sections, "find_links", lambda _page: [])

    recovered = sections._refresh_section_control(
        Page(), original, sections.ExpandConfig(scratch_dir=tmp_path))

    assert recovered is current


def test_section_discovery_recovers_a_late_profile_error(monkeypatch, tmp_path):
    order = []
    page = type("Page", (), {"url": "https://www.linkedin.com/in/jane/"})()
    config = sections.ExpandConfig(scratch_dir=tmp_path)

    monkeypatch.setattr(
        sections, "recover_transient_error",
        lambda _page: order.append("recover"))
    monkeypatch.setattr(
        sections, "find_links",
        lambda _page: order.append("find") or [])
    monkeypatch.setattr(
        sections, "capture_about_member", lambda _page, _config=None: None)
    monkeypatch.setattr(
        sections, "capture_contact_info", lambda _page, _config=None: None)
    monkeypatch.setattr(
        sections, "capture_open_to_work", lambda _page, _config=None: None)
    monkeypatch.setattr(sections, "capture_featured", lambda _page: None)
    monkeypatch.setattr(sections, "capture_causes", lambda _page: None)
    monkeypatch.setattr(
        sections, "capture_inline_recommendations", lambda _page: iter(()))
    monkeypatch.setattr(
        sections, "find_tabbed_details_link", lambda *_args: None)
    monkeypatch.setattr(
        sections, "capture_inline_interests", lambda *_args: iter(()))
    monkeypatch.setattr(
        sections, "_capture_activity", lambda *_args: iter(()))

    assert list(sections.expand(page, config, "jane")) == []
    assert order == ["recover", "find"]


def test_activity_delegates_target_scrolling_to_the_shared_click(monkeypatch, tmp_path):
    order = []
    link = sections.SectionLink(
        name=sections.ACTIVITY,
        url="https://www.linkedin.com/in/jane/recent-activity/all/",
        href="/in/jane/recent-activity/all/",
    )

    class Locator:
        first = None

        def __init__(self):
            self.first = self

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def locator(self, selector):
            order.append(("locate", selector))
            return Locator()

    capture = sections.PageCapture(url_requested=link.url, ok=True)
    monkeypatch.setattr(sections, "find_activity", lambda _page: link)
    monkeypatch.setattr(
        sections, "open_link",
        lambda _page, _link, *, attempts: (
            order.append(("open", attempts)) or None))
    monkeypatch.setattr(sections, "wait_for_feed", lambda *_args: None)
    monkeypatch.setattr(
        sections, "_capture_feed",
        lambda *_args, **_kwargs: sections.SectionCapture(link.name, capture))
    monkeypatch.setattr(sections, "_capture_tabs", lambda *_args: iter(()))
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)
    monkeypatch.setattr(sections, "_back_from_activity", lambda *_args: None)
    monkeypatch.setattr(sections, "ACTIVITY_TABS", {})

    config = sections.ExpandConfig(scratch_dir=tmp_path)
    list(sections._capture_activity(Page(), tmp_path, "jane", config))

    assert order == [("open", 2)]


def test_activity_capture_uses_structural_readiness_and_feed_reading(monkeypatch, tmp_path):
    calls = []

    class Page:
        url = "https://www.linkedin.com/in/jane/recent-activity/all/"

        def wait_for_function(self, script, *, arg, timeout):
            calls.append(("wait", script, arg, timeout))

    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        sections, "capture_page",
        lambda _page, _scratch, _slug, **kwargs: (
            calls.append(("capture", kwargs)) or sections.PageCapture(
                url_requested=kwargs["url_requested"], ok=True)))

    result = sections._capture_feed(
        Page(), tmp_path, "jane", "activity-posts", Page.url,
        tempo=object())

    wait = calls[0]
    assert wait[0] == "wait"
    assert wait[1] == sections.ACTIVITY_CONTENT_READY
    capture = calls[1][1]
    assert capture["read_selector"] == sections.ACTIVITY_RESULTS
    assert capture["dismiss"] is not None
    assert result.capture.ok


# Both shapes are lifted from real captures: the anchor from a profile whose
# About panel opened, the div from one where it silently did not.
ABOUT_TRIGGER_AS_ANCHOR = """
<main><section>
  <a aria-haspopup="dialog" href="/in/afreen-munshi/"><h2>Afreen Munshi, PMP</h2></a>
</section></main>
"""
ABOUT_TRIGGER_AS_DIV = """
<main><section>
  <div aria-haspopup="dialog"><h2>Jensen Huang</h2></div>
</section></main>
"""
# The other "More" on the page. Outside `main`, which is what separates them.
ABOUT_TRIGGER_DECOY = """
<main><section><h2>Jensen Huang</h2></section></main>
<nav><div aria-haspopup="dialog"><h2>Me</h2></div></nav>
"""


def _matches(markup: str) -> int:
    import soupsieve
    from bs4 import BeautifulSoup

    return len(soupsieve.select(sections.ABOUT_TRIGGER,
                                BeautifulSoup(markup, "html.parser")))


def test_the_about_panel_opens_whichever_tag_linkedin_used():
    """LinkedIn renders this control as an anchor on some profiles and a plain
    div on others -- same attribute, same h2 child, only the tag differs. An
    `a` in the selector reported "no verification trigger" on a profile whose
    verification badge was plainly there, and the whole section went missing
    with nothing saying it had been looked for."""
    assert _matches(ABOUT_TRIGGER_AS_ANCHOR) == 1
    assert _matches(ABOUT_TRIGGER_AS_DIV) == 1


def test_the_navigation_menu_is_not_mistaken_for_the_about_panel():
    """`main` is the whole of what separates them, so dropping the tag
    constraint must not have widened this."""
    assert _matches(ABOUT_TRIGGER_DECOY) == 0


# The overflow button, lifted from tanushree-waratkar's profile: an unverified
# member has no `aria-haspopup="dialog"` anywhere on the page, and this is the
# only control that reaches the same dialog.
ABOUT_OVERFLOW_MARKUP = """
<main><section>
  <div data-view-name="profile-overflow-button">
    <button type="button" aria-label="More" aria-expanded="false"></button>
  </div>
</section></main>
"""
# LinkedIn's August 24 SDUI build removed the stable wrapper attribute while
# leaving the profile action itself unchanged. This is the exact relevant
# shape from rq-06g37vl8eu979q3bb6vap8e438.
ABOUT_OVERFLOW_WITHOUT_WRAPPER = """
<main><section>
  <div data-display-contents="true">
    <button type="button" aria-label="More" aria-expanded="false"></button>
  </div>
</section></main>
"""
# The identity switcher in the global nav carries the same label and is not it.
ABOUT_OVERFLOW_DECOY = """
<main><section><h2>Tanushree Waratkar</h2></section></main>
<nav><div data-view-name="profile-overflow-button">
  <button aria-label="More"></button></div></nav>
"""


def _overflow_matches(markup: str) -> int:
    import soupsieve
    from bs4 import BeautifulSoup

    return len(soupsieve.select(sections.ABOUT_OVERFLOW,
                                BeautifulSoup(markup, "html.parser")))


def test_the_more_button_is_found_on_an_unverified_profile():
    assert _overflow_matches(ABOUT_OVERFLOW_MARKUP) == 1


def test_the_more_button_survives_linkedins_removed_wrapper_identity():
    assert _overflow_matches(ABOUT_OVERFLOW_WITHOUT_WRAPPER) == 1


def test_the_navigation_more_is_not_mistaken_for_the_profiles_own():
    assert _overflow_matches(ABOUT_OVERFLOW_DECOY) == 0


class _AboutPage:
    """A profile page that answers only the selectors it was told to have."""

    url = "https://www.linkedin.com/in/tanushree/"

    def __init__(self, *, badge=False, more=False, menu_item=False,
                 close=True, offered=()):
        self.badge, self.more = badge, more
        self.menu_item, self.close, self.offered = menu_item, close, list(offered)
        self.clicked, self.keys = [], []
        self.keyboard = self

    def press(self, key):
        self.keys.append(key)

    def locator(self, selector):
        if selector == sections.ABOUT_TRIGGER:
            return _AboutLocator(self, "badge", self.badge)
        if selector == sections.ABOUT_OVERFLOW:
            return _AboutLocator(self, "more", self.more)
        if selector == sections.ABOUT_MENU_ITEMS:
            return _AboutLocator(self, "menu", bool(self.offered),
                                 texts=self.offered)
        if selector == sections.ABOUT_CLOSE:
            return _AboutLocator(
                self, "close", self.close and (self.badge or self.menu_item))
        return _AboutLocator(self, "dialog", self.badge or self.menu_item)

    def get_by_role(self, role, *, name=None, exact=False):
        if role == "button" and name == "More" and exact:
            return _AboutLocator(self, "more", self.more)
        return _AboutLocator(self, f"item:{role}",
                             self.menu_item and role == "menuitem")

    def title(self):
        return "Tanushree Waratkar | LinkedIn"


class _AboutLocator:
    def __init__(self, page, what, present, texts=()):
        self.page, self.what, self.present = page, what, present
        self.texts = list(texts)

    @property
    def first(self):
        return self

    def get_by_role(self, role, *, name=None, exact=False):
        return self.page.get_by_role(role, name=name, exact=exact)

    def count(self):
        return 1 if self.present else 0

    def all_inner_texts(self):
        return self.texts

    def wait_for(self, *, state, timeout):
        if not self.present:
            raise TimeoutError("no dialog")

    def inner_text(self):
        return "About this member\n\nJoined LinkedIn\n\nJune 2022"

    def evaluate(self, _script):
        return "<dialog><h2>About this member</h2></dialog>"

    def screenshot(self, **_kwargs):
        return b"png"


def _no_real_clicking(monkeypatch):
    monkeypatch.setattr(
        sections.interaction, "click",
        lambda page, locator, **_kwargs: page.clicked.append(locator.what))


def test_an_unverified_profile_is_read_through_its_More_menu(monkeypatch):
    """The dialog is the same one; only the way in differs.

    A member LinkedIn has verified nothing about still has an account history,
    and it is the only place on the site that says when the account was made.
    Reading it only off the verification badge meant every unverified profile
    came back with no About this member section at all.
    """
    page = _AboutPage(more=True, menu_item=True)
    _no_real_clicking(monkeypatch)

    result = sections.capture_about_member(page)

    assert result is not None
    assert result.capture.ok
    assert page.clicked == ["more", "item:menuitem", "close"]


def test_a_profile_offering_neither_way_in_yields_no_about_capture(monkeypatch):
    """Absence stays ordinary: the menu differs by who is reading and who is
    read, and a profile that offers nothing is not an error."""
    _no_real_clicking(monkeypatch)

    assert sections.capture_about_member(_AboutPage()) is None


def test_a_More_menu_without_the_item_is_closed_again(monkeypatch):
    """A menu left open holds the scroll lock, so every section below it would
    be looked for on a page that cannot move."""
    page = _AboutPage(more=True, offered=["Follow", "Report or block"])
    _no_real_clicking(monkeypatch)

    assert sections.capture_about_member(page) is None
    assert page.keys == ["Escape"]


def test_the_verification_badge_is_still_the_first_way_in(monkeypatch):
    """One click rather than two, on the profiles that offer it."""
    page = _AboutPage(badge=True, more=True, menu_item=True)
    _no_real_clicking(monkeypatch)

    assert sections.capture_about_member(page) is not None
    assert page.clicked == ["badge", "close"]


def test_about_member_uses_escape_only_when_its_close_control_is_missing(monkeypatch):
    page = _AboutPage(badge=True, close=False)
    _no_real_clicking(monkeypatch)

    assert sections.capture_about_member(page) is not None
    assert page.clicked == ["badge"]
    assert page.keys == ["Escape"]


class _LivePage:
    def is_closed(self) -> bool:
        return False


def test_a_section_that_merely_failed_is_still_skipped():
    """The rule this module is built on: one dead section costs one section."""
    sections.raise_if_gone(
        _LivePage(), BrowserTimeoutError("patents took too long"),
        during="patents")


def test_a_session_lost_between_sections_stops_the_run():
    """A logged-out public profile must not become several skipped sections."""
    from career_lens.capture.failures import CaptureFailed

    class _SignedOutPage(_LivePage):
        url = "https://www.linkedin.com/in/afreen-munshi"

        def title(self):
            return "Afreen Munshi - Apple | LinkedIn"

        def inner_text(self, selector):
            assert selector == "body"
            return (
                "Skip to main content\nLinkedIn\nJoin now\nSign in\n"
                + "the public profile remains rendered " * 200
            )

    try:
        sections.raise_if_gone(
            _SignedOutPage(), BrowserTimeoutError("skills took too long"),
            during="skills")
    except CaptureFailed as failure:
        assert failure.failure_kind == "login_required"
        assert "skills" in failure.detail
    else:
        raise AssertionError("a lost session must not be a skipped section")


def test_a_profile_that_only_comments_still_reaches_its_activity():
    """LinkedIn renders "Show all comments" and no `all` link at all for a
    profile that has commented but never posted. Insisting on `all` threw the
    whole activity feed away for those people, silently."""
    link = sections.activity_from_hrefs([
        ("/in/jane/recent-activity/comments/",
         "https://www.linkedin.com/in/jane/recent-activity/comments/"),
    ])

    assert link is not None
    assert link.name == "activity-comments"
    assert link.url.endswith("/recent-activity/comments/")


def test_the_landing_view_wins_when_a_profile_offers_more_than_one():
    link = sections.activity_from_hrefs([
        ("/in/jane/recent-activity/comments/",
         "https://www.linkedin.com/in/jane/recent-activity/comments/"),
        ("/in/jane/recent-activity/all/",
         "https://www.linkedin.com/in/jane/recent-activity/all/"),
    ])

    assert link.name == sections.ACTIVITY
    assert link.url.endswith("/recent-activity/all/")


def test_an_off_profile_recent_activity_link_is_still_rejected():
    assert sections.activity_from_hrefs([
        ("/feed/update/urn:li:activity:1/",
         "https://www.linkedin.com/feed/update/urn:li:activity:1/"),
    ]) is None


def test_landing_on_comments_still_visits_the_posts_tab(monkeypatch, tmp_path):
    """The tab strip is the same wherever the feed opens, so a profile reached
    through its comments must still be asked for everything else -- Posts
    included, which the `all` landing would have covered."""
    captured: list[str] = []
    opened: list[str] = []
    link = sections.SectionLink(
        name="activity-comments",
        url="https://www.linkedin.com/in/jane/recent-activity/comments/",
        href="/in/jane/recent-activity/comments/",
    )

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def evaluate(self, _script):
            return 0

    monkeypatch.setattr(sections, "find_activity", lambda _page: link)
    monkeypatch.setattr(
        sections, "open_link", lambda _page, _link, *, attempts: None)
    monkeypatch.setattr(sections, "wait_for_feed", lambda *_args: None)
    monkeypatch.setattr(
        sections, "_capture_feed",
        lambda _page, _scratch, _slug, name, _url, **_kwargs: (
            captured.append(name) or sections.SectionCapture(
                name, sections.PageCapture(url_requested=link.url, ok=True))))
    monkeypatch.setattr(
        sections, "open_activity_tab",
        lambda _page, label: opened.append(label) or True)
    monkeypatch.setattr(sections, "_dwell", lambda *_args: None)
    monkeypatch.setattr(sections, "_back_from_activity", lambda *_args: None)

    config = sections.ExpandConfig(scratch_dir=tmp_path)
    list(sections._capture_activity(Page(), tmp_path, "jane", config))

    # The landing is stored under the view it actually is, not as posts.
    assert captured[0] == "activity-comments"
    assert "Posts" in opened
    # And it is never fetched twice.
    assert "Comments" not in opened
    assert captured.count("activity-comments") == 1
    assert "activity-posts" in captured
PROFILE = "https://www.linkedin.com/in/jensen-huang/"
OVERLAY = "https://www.linkedin.com/in/jensen-huang/overlay/contact-info/"


class _ContactLocator:
    def __init__(self, page, what, present):
        self.page, self.what, self.present = page, what, present

    @property
    def first(self):
        return self

    def count(self):
        return 1 if self.present else 0

    def wait_for(self, *, state, timeout):
        if state == "visible" and not self.present:
            raise TimeoutError("no dialog")

    def inner_text(self):
        return "Contact info\n\nEmail\n\njensen@nvidia.com"

    def evaluate(self, _script):
        return '<div role="dialog"><h1>Contact info</h1></div>'

    def screenshot(self, **_kwargs):
        return b"png"


class _ContactPage:
    """A profile offering the Contact info link, or not.

    `navigates` is the build that treats the link as an address rather than
    intercepting it: the click lands on the overlay URL instead of opening a
    dialog over the profile.
    """

    def __init__(self, link=True, dialog=True, navigates=False, renders=True):
        self.url = PROFILE
        self.link, self.dialog, self.navigates = link, dialog, navigates
        self.renders = renders
        self.clicked, self.keys, self.visited, self.waited = [], [], [], []
        self.keyboard = self

    def wait_for_function(self, script, **kwargs):
        self.waited.append(script)
        if not self.renders:
            raise TimeoutError("dialog never filled")

    def press(self, key):
        self.keys.append(key)

    def locator(self, selector):
        if "contact-info" in selector and "dialog" not in selector:
            return _ContactLocator(self, "link", self.link)
        return _ContactLocator(self, "dialog", self.dialog)

    def goto(self, url, **_kwargs):
        self.visited.append(url)
        self.url = url

    def title(self):
        return "Jensen Huang | LinkedIn"


def _contact_click(monkeypatch):
    def click(page, locator, **_kwargs):
        page.clicked.append(locator.what)
        if page.navigates:
            page.url = OVERLAY
    monkeypatch.setattr(sections.interaction, "click", click)


def test_the_contact_info_dialog_is_captured_on_its_own(monkeypatch):
    """The panel, not the profile behind it. Opening it dims and freezes the
    page, so capturing the page would store a greyed-out profile with a box on
    top and a second copy of the profile's HTML."""
    page = _ContactPage()
    _contact_click(monkeypatch)

    result = sections.capture_contact_info(page)

    assert result is not None
    assert result.section == "contact-info"
    assert result.capture.ok
    assert b"Contact info" in result.capture.html_bytes
    assert result.capture.screenshot_bytes == [b"png"]
    assert page.clicked == ["link"]


def test_a_profile_with_no_contact_link_yields_no_capture(monkeypatch):
    """Absence is ordinary. Not every profile exposes contact details, and one
    that does not is not a failed read."""
    _contact_click(monkeypatch)

    assert sections.capture_contact_info(_ContactPage(link=False)) is None


def test_the_contact_dialog_is_closed_so_the_page_can_still_scroll(monkeypatch):
    """A dialog left open holds the scroll lock, so every section below it
    would be looked for on a page that cannot move."""
    page = _ContactPage()
    _contact_click(monkeypatch)

    sections.capture_contact_info(page)

    assert page.keys == ["Escape"]


def test_a_click_that_navigated_puts_the_profile_back(monkeypatch):
    """On builds serving the link as an address, the click leaves the overlay
    URL on screen. Every section after this one is found from the profile, so
    the profile is where this has to leave the page."""
    page = _ContactPage(navigates=True)
    _contact_click(monkeypatch)

    sections.capture_contact_info(page)

    assert page.visited == [PROFILE]
    assert page.url == PROFILE


def test_the_dialog_is_read_after_its_contents_arrive(monkeypatch):
    """Visible is not ready. LinkedIn renders the dialog frame with a loader
    inside and fills it from a second request, so reading on visible stored a
    spinner and nothing else."""
    page = _ContactPage()
    _contact_click(monkeypatch)

    result = sections.capture_contact_info(page)

    assert page.waited, "read the dialog without waiting for it to fill"
    assert result.unrendered is None


def test_a_dialog_that_never_fills_is_captured_and_said_so(monkeypatch):
    """Not fatal, for the reason `wait_for_content` is not: whatever did
    render is still captured, and the reason is recorded so an empty capture
    says so rather than looking like a member who published nothing."""
    page = _ContactPage(renders=False)
    _contact_click(monkeypatch)

    result = sections.capture_contact_info(page)

    assert result is not None
    assert result.capture.ok
    assert result.unrendered


@pytest.mark.browser
def test_the_dialog_predicate_runs_as_page_script(browser_page):
    """It is evaluated by the browser, so it may use only what the browser
    understands. Built on CONTACT_DIALOG it was never true -- `:has-text()` is
    Playwright's and `document.querySelector` refuses it -- so every capture
    waited out the timeout and stored the spinner it was meant to avoid.

    The markup is the shape LinkedIn actually served: the dialog carries its
    own heading, a second heading for the toast region, and a loader inside
    the content area until the details arrive.
    """
    browser_page.set_content("""
      <dialog open data-testid="dialog">
        <header><h2>Contact info</h2></header>
        <div data-testid="dialog-content">
          <div data-testid="loader"><svg role="progressbar"></svg></div>
        </div>
        <section><h2>0 notifications</h2></section>
      </dialog>
    """)
    ready = f"(arg) => ({sections.DIALOG_READY})(arg)"

    assert browser_page.evaluate(ready, [sections.CONTACT_HEADING]) is False

    browser_page.evaluate("""() => {
      document.querySelector('[data-testid="loader"]').remove();
      document.querySelector('[data-testid="dialog-content"]').innerHTML =
        '<h3>Email</h3><a href="mailto:jane@example.com">jane@example.com</a>';
    }""")

    assert browser_page.evaluate(ready, [sections.CONTACT_HEADING]) is True


class _OpenToWorkPage(_ContactPage):
    """A profile showing the Open to work card, or not."""

    def locator(self, selector):
        if selector == sections.OPEN_TO_WORK_CARD_TEXT:
            return _OpenToWorkCardLocator(self, self.link)
        return _ContactLocator(self, "dialog", self.dialog)


class _OpenToWorkCardLocator(_ContactLocator):
    def __init__(self, page, present):
        super().__init__(page, "open-to-work-card", present)

    def locator(self, selector):
        if selector == sections.PROFILE_CARD:
            return self
        assert "Show details" in selector
        return _ContactLocator(self.page, "show-details", self.present)


def test_the_open_to_work_dialog_is_captured_from_the_card(monkeypatch):
    """The card names one place and "+4 more"; the whole answer is only in the
    dialog behind Show details."""
    page = _OpenToWorkPage()
    _contact_click(monkeypatch)

    result = sections.capture_open_to_work(page)

    assert result is not None
    assert result.section == "open-to-work"
    assert result.capture.ok
    assert page.clicked == ["show-details"]


def test_a_profile_not_open_to_work_yields_no_capture(monkeypatch):
    """Most members are not looking, and that is not a failed read."""
    _contact_click(monkeypatch)

    assert sections.capture_open_to_work(_OpenToWorkPage(link=False)) is None


def test_the_open_to_work_dialog_is_read_after_it_fills(monkeypatch):
    """Same race the Contact info dialog lost: visible before it holds
    anything, so reading on visible stores the spinner."""
    page = _OpenToWorkPage()
    _contact_click(monkeypatch)

    result = sections.capture_open_to_work(page)

    assert page.waited
    assert result.unrendered is None


@pytest.mark.browser
def test_a_dialog_that_titles_itself_without_a_heading_is_still_found(
        browser_page):
    """Open to Work carries its own title as a plain run; the only heading in
    it belongs to the toast region. Keyed on headings, the predicate never
    matched and every open-to-work profile paid the full timeout."""
    browser_page.set_content("""
      <dialog open data-testid="dialog">
        <div data-testid="dialog-content">
          <div><p>Open to Work</p></div>
          <div><p>Job titles</p><p>Data Engineer</p></div>
        </div>
        <section><h2>0 notifications</h2></section>
      </dialog>
    """)

    assert browser_page.evaluate(
        f"(arg) => ({sections.DIALOG_READY})(arg)",
        [sections.OPEN_TO_WORK_HEADING]) is True


# One dialog carrying all three kinds of furniture, plus two details that must
# survive every rule: a labelled row and the member's own photograph.
DIALOG_WITH_FURNITURE = """
  <dialog open data-testid="dialog">
    <div data-testid="dialog-content">
      <div data-testid="lazy-column">
        <div><p>Website</p><a href="https://example.com/">example.com</a></div>
        <div>
          <img src="https://media.licdn.com/member.png">
          <p>Afreen Munshi</p><p>is Open to Work</p>
        </div>
        <div>
          <p>Get up to 4.6x replies when you message with InMail</p>
          <a href="https://www.linkedin.com/premium/products/?upsellSlotId=X">
            Try Premium</a>
        </div>
        <div role="button">
          <img src="https://media.licdn.com/reader.png">
          <p>Looking for a new job too?</p>
        </div>
      </div>
    </div>
    <section><h2 data-testid="toasts-title">0 notifications</h2></section>
  </dialog>
"""


@pytest.mark.browser
def test_a_dialog_is_stored_without_linkedins_own_furniture(browser_page):
    """Three things ride along inside these dialogs that are not about the
    member: the account's notification count, what LinkedIn is selling, and a
    pitch card carrying the reader's own photograph. The member's picture and
    every detail stay.
    """
    from career_lens.linkedin import dom

    browser_page.set_content(DIALOG_WITH_FURNITURE)

    gone = browser_page.evaluate(dom.STRIP_DIALOG_NOISE)
    text = browser_page.evaluate("() => document.body.innerText")
    stored = browser_page.locator("dialog").evaluate(sections.OUTER_HTML)

    assert gone == 3
    assert "Website" in text and "example.com" in text
    assert "Afreen Munshi" in text
    assert "0 notifications" not in text
    assert "InMail" not in text
    assert "Looking for a new job too?" not in text
    # The member's photograph is content; the reader's was inside the pitch.
    assert "member.png" in stored and "reader.png" not in stored
    assert "0 notifications" not in stored and "InMail" not in stored


@pytest.mark.browser
def test_stripping_a_dialog_leaves_every_node_the_site_owns(browser_page):
    """Hidden, never removed.

    Removing them worked on the dialog and broke the profile underneath it:
    LinkedIn's own code owns these nodes, and unmounting the dialog over a
    subtree whose children had gone took the profile view down with it. The
    next section then looked for its control on an empty `main` and reported
    the member was not open to work.
    """
    from career_lens.linkedin import dom

    browser_page.set_content(DIALOG_WITH_FURNITURE)
    before = browser_page.evaluate(
        "() => document.querySelectorAll('dialog *').length")

    dom.strip_dialog_noise(browser_page)

    assert browser_page.evaluate(
        "() => document.querySelectorAll('dialog *').length") == before
    assert browser_page.evaluate("() => document.images.length") == 2


def test_closing_a_dialog_waits_for_the_profile_to_render_again(monkeypatch):
    """The section after this one gets one instantaneous look for its control.

    Closing the dialog is a route change on the builds that serve it as an
    address, and the profile view renders again after the pop. Returning before
    it has means the next section counts its trigger on an empty `main` and
    reports the member has no such section -- which is how two profiles that
    were open to work were recorded as not.
    """
    page = _ContactPage()
    _contact_click(monkeypatch)

    sections.capture_contact_info(page)

    assert sections.PROFILE_RENDERED in page.waited


# -- a feed that failed to load, vs one that is simply empty ------------


def test_a_failed_feed_is_retried_and_its_stale_reason_cleared(monkeypatch):
    """Recovery that works must clear the reason, not keep reporting it."""
    from career_lens.linkedin import sections

    waits = []
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: True)
    monkeypatch.setattr(sections, "wait_for_activity_content",
                        lambda page, was=None: waits.append(was) or None)

    assert sections._retry_feed(object(), "activity-comments", was=42,
                                reason="TimeoutError: gave up") is None
    # Re-waits against the previous view's length, not the error page's.
    assert waits == [42]


def test_a_feed_that_will_not_recover_keeps_reporting_it(monkeypatch):
    from career_lens.linkedin import sections

    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: True)
    monkeypatch.setattr(sections, "wait_for_activity_content",
                        lambda page, was=None: "TimeoutError: still nothing")

    assert sections._retry_feed(object(), "activity-comments", was=None,
                                reason="TimeoutError: gave up") == (
        "TimeoutError: still nothing")


def test_a_feed_with_no_error_state_keeps_its_original_reason(monkeypatch):
    """A slow feed is not an errored one: don't relabel why it stalled."""
    from career_lens.linkedin import sections

    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(sections, "wait_for_activity_content",
                        lambda page, was=None: pytest.fail("must not re-wait"))

    assert sections._retry_feed(object(), "activity-posts", was=None,
                                reason="TimeoutError: gave up") == (
        "TimeoutError: gave up")


def test_a_recovery_that_raises_costs_the_view_not_the_profile(monkeypatch):
    from career_lens.linkedin import sections
    from career_lens.linkedin.recovery import LinkedInTransientError

    def explode(_page):
        raise LinkedInTransientError("would not recover")

    monkeypatch.setattr(sections, "recover_transient_error", explode)

    assert sections._retry_feed(object(), "activity-comments", was=None,
                                reason="TimeoutError: gave up") == (
        "TimeoutError: gave up")


def test_a_recovered_feed_is_not_still_marked_unrendered(monkeypatch, tmp_path):
    """The whole point of retrying: a feed that came back reads as rendered.

    Exercised through _capture_feed rather than _retry_feed, because the bug
    this guards lives at the call site -- `_retry_feed(...) or unrendered`
    keeps the stale reason precisely when recovery succeeded.
    """
    from career_lens.linkedin import sections

    waits = iter(["TimeoutError: gave up", None])

    class Page:
        url = "https://www.linkedin.com/in/jane/recent-activity/comments/"

    monkeypatch.setattr(sections, "wait_for_activity_content",
                        lambda page, was=None: next(waits))
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: True)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        sections, "capture_page",
        lambda _page, _scratch, _slug, **kwargs: sections.PageCapture(
            url_requested=kwargs["url_requested"], ok=True))

    result = sections._capture_feed(
        Page(), tmp_path, "jane", "activity-comments", Page.url)

    assert result.unrendered is None


def test_a_feed_that_never_recovers_is_still_marked_unrendered(
        monkeypatch, tmp_path):
    from career_lens.linkedin import sections

    class Page:
        url = "https://www.linkedin.com/in/jane/recent-activity/comments/"

    monkeypatch.setattr(sections, "wait_for_activity_content",
                        lambda page, was=None: "TimeoutError: gave up")
    monkeypatch.setattr(sections, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(sections, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        sections, "capture_page",
        lambda _page, _scratch, _slug, **kwargs: sections.PageCapture(
            url_requested=kwargs["url_requested"], ok=True))

    result = sections._capture_feed(
        Page(), tmp_path, "jane", "activity-comments", Page.url)

    assert result.unrendered == "TimeoutError: gave up"
