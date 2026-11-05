"""The company tab sweep, with the browser stubbed out.

What is checked here is the decisions the sweep makes -- which pages it visits,
what a failure costs, when it stops entirely -- not the markup handling, which
lives in dom.py and was ported whole.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from career_lens.linkedin import company as company_module
from career_lens.linkedin.dom import Company, Tab


class Page:
    """Enough of a Patchright page for the sweep. `url` is the live route,
    which the sweep reads when it captures something it navigated to rather
    than clicked."""

    url = "https://www.linkedin.com/company/stripe/"


class Capture:
    """Enough of a PageCapture for the sweep's decisions."""

    def __init__(self, ok=True, title="T"):
        self.ok = ok
        self.title = title
        self.text_chars = 100
        self.scroll_stopped = "end"
        self.scroll_growing = False
        self.screenshot_error = None
        self.extra = {}


@pytest.fixture
def stubbed(monkeypatch):
    """Every dom call the sweep makes, replaced with something inert.

    Defaults are the quiet path: no events view, no challenge, no job search.
    A test that cares about one of those overrides it.
    """
    def stub(result=None):
        return lambda *args, **kwargs: result

    monkeypatch.setattr(company_module.dom, "open_all_events", stub(False))
    monkeypatch.setattr(company_module.dom, "tabs", stub([]))
    monkeypatch.setattr(company_module.dom, "open_tab", stub(True))
    monkeypatch.setattr(company_module.dom, "open_all_jobs", stub(False))
    monkeypatch.setattr(company_module.dom, "load_people_analytics", stub(None))
    monkeypatch.setattr(company_module.dom, "load_company_carousels", stub(None))
    monkeypatch.setattr(company_module.dom, "company_feed_is_empty", stub(False))
    monkeypatch.setattr(company_module.dom, "company_events_is_empty", stub(False),
                        raising=False)
    monkeypatch.setattr(company_module.dom, "return_to_company_tab", stub(True))
    monkeypatch.setattr(company_module.dom, "prepare_job_results", stub(None))
    monkeypatch.setattr(company_module.dom, "job_results_title", stub("title"))
    monkeypatch.setattr(company_module.overlays, "dismiss", stub([]))
    monkeypatch.setattr(company_module.detect, "detect", stub(None))
    monkeypatch.setattr(company_module.detect, "is_blocking", stub(False))
    monkeypatch.setattr(company_module.sections, "open_clipped_text", stub(0))
    monkeypatch.setattr(company_module, "capture_page",
                        lambda *args, **kwargs: Capture())


def config(**over):
    return company_module.CompanyConfig(scratch_dir=Path("/tmp"), **over)


def sweep(**over):
    return list(company_module.expand(
        Page(), config(**over), Company("company", "stripe", "")))


def offering(monkeypatch, *names):
    monkeypatch.setattr(company_module.dom, "tabs", lambda page, company: [
        Tab(name.title(), f"/company/stripe/{name}/") for name in names
    ])


def test_it_sweeps_the_tabs_the_nav_offers_in_order(monkeypatch, stubbed):
    offering(monkeypatch, "about", "people")

    assert [item.section for item in sweep()] == ["about", "people"]


@pytest.mark.parametrize("requested,landed,expected", [
    ("stripe/", "stripe/", ["company", "about", "people"]),
    ("stripe/", "stripe/home/", ["company", "about", "people"]),
    ("1283/", "stripe/home/", ["company", "about", "people"]),
    ("stripe/about/", "stripe/home/", ["company"]),
    ("stripe/home/", "stripe/home/", ["company"]),
    ("stripe/life/", "stripe/life/team/", ["company"]),
])
@pytest.mark.parametrize("sections", [None, frozenset({"home"}), frozenset({"about"})])
def test_native_company_redirect_preserves_request_scope(
    monkeypatch, stubbed, tmp_path, requested, landed, expected, sections,
):
    from career_lens.capture import driver

    target = f"https://www.linkedin.com/company/{requested}"

    class RedirectPage(Page):
        def goto(self, url, **kwargs):
            self.url = f"https://www.linkedin.com/company/{landed}"
            return None

    offering(monkeypatch, "about", "people")
    monkeypatch.setattr(driver.discovery, "discover", lambda *a, **kw: None)
    monkeypatch.setattr(driver.verification, "wait", lambda page, response, **kw: response)
    monkeypatch.setattr(driver, "capture_arrived_page", lambda *a, **kw: Capture())
    monkeypatch.setattr(driver, "_dwell", lambda *a: None)
    monkeypatch.setattr(driver, "document", lambda capture, section, recorder: section)
    monkeypatch.setattr(driver, "TABS", frozenset())
    output = list(driver._capture_documents(
        "linkedin_company", target, RedirectPage(), tmp_path,
        0, None, None, None, sections, None,
    ))
    if sections is not None:
        expected = [name for name in expected if name == "company" or name in sections]
    assert output == expected


def test_a_tab_that_will_not_open_is_skipped_rather_than_fatal(
        monkeypatch, stubbed):
    offering(monkeypatch, "about", "people")
    monkeypatch.setattr(company_module.dom, "open_tab",
                        lambda page, tab: tab.path.endswith("people/"))

    # Losing five good tabs to save none is the trade _sections() rejects too.
    assert [item.section for item in sweep()] == ["people"]


def test_a_blocking_challenge_stops_the_sweep_and_keeps_what_it_has(
        monkeypatch, stubbed):
    offering(monkeypatch, "about", "people", "jobs")
    monkeypatch.setattr(company_module.detect, "detect", lambda page: "wall")
    monkeypatch.setattr(company_module.detect, "is_blocking", lambda c: True)

    swept = sweep()

    assert [item.section for item in swept] == ["about"]
    assert swept[-1].stop is True


def test_the_operator_can_narrow_the_sweep(monkeypatch, stubbed):
    offering(monkeypatch, "about", "people")

    assert [item.section for item in sweep(tabs=frozenset({"people"}))] == [
        "people"]


def test_events_are_skipped_when_the_request_does_not_select_them(
        monkeypatch, stubbed):
    offering(monkeypatch, "about")
    opened = []
    monkeypatch.setattr(
        company_module.dom, "open_all_events",
        lambda page, company: opened.append(True) or True,
    )

    assert [item.section for item in sweep(
        tabs=frozenset({"about"}))] == ["about"]
    assert opened == []


def test_none_turns_the_sweep_off_entirely(monkeypatch, stubbed):
    offering(monkeypatch, "about", "people")

    assert sweep(tabs=None) == []


def test_a_url_naming_one_tab_is_a_request_for_that_tab_only(
        monkeypatch, stubbed):
    """`/company/stripe/about/` asked for About; the caller already has it."""
    offering(monkeypatch, "about", "people")

    swept = list(company_module.expand(
        Page(), config(), Company("company", "stripe", "about")))

    assert swept == []


def test_the_events_view_is_captured_before_the_tabs(monkeypatch, stubbed):
    """It is reached from a Home-page link, so it has to come first -- the tab
    sweep reads the live route, and would find no nav from the events page."""
    offering(monkeypatch, "about")
    monkeypatch.setattr(company_module.dom, "open_all_events", lambda p, c: True)

    assert [item.section for item in sweep()] == ["events", "about"]


def test_an_explicitly_empty_events_view_is_captured_without_scrolling(
        monkeypatch, stubbed):
    monkeypatch.setattr(company_module.dom, "open_all_events", lambda p, c: True)
    monkeypatch.setattr(company_module.dom, "company_events_is_empty", lambda p: True,
                        raising=False)
    options = {}
    monkeypatch.setattr(
        company_module, "capture_page",
        lambda *args, **kwargs: options.update(kwargs) or Capture())

    assert [item.section for item in sweep()] == ["events"]
    assert options["scroll"] is False


def test_failing_to_get_back_from_events_stops_before_the_tabs(
        monkeypatch, stubbed):
    offering(monkeypatch, "about")
    monkeypatch.setattr(company_module.dom, "open_all_events", lambda p, c: True)
    monkeypatch.setattr(company_module.dom, "return_to_company_tab",
                        lambda page, tab, **kwargs: False)

    assert [item.section for item in sweep()] == ["events"]


def test_job_pages_are_not_entered_when_the_operator_turns_them_off(
        monkeypatch, stubbed):
    offering(monkeypatch, "jobs")
    entered = []
    monkeypatch.setattr(company_module.dom, "open_all_jobs",
                        lambda page: entered.append(True) or True)

    assert [item.section for item in sweep(job_pages=0)] == ["jobs"]
    assert entered == []


def test_job_result_pages_are_numbered_from_one(monkeypatch, stubbed):
    offering(monkeypatch, "jobs")
    monkeypatch.setattr(company_module.dom, "open_all_jobs", lambda page: True)
    monkeypatch.setattr(company_module.dom, "open_job_page",
                        lambda page, number: number <= 2)

    # Page three is refused, which means the finite list ended early -- not an
    # error, so pages one and two are kept.
    assert [item.section for item in sweep(job_pages=3)] == [
        "jobs", "jobs-page-1", "jobs-page-2"]


def test_failing_to_get_back_from_the_job_search_stops_the_sweep(
        monkeypatch, stubbed):
    offering(monkeypatch, "jobs", "about")
    monkeypatch.setattr(company_module.dom, "open_all_jobs", lambda page: True)
    monkeypatch.setattr(company_module.dom, "open_job_page",
                        lambda page, number: False)
    monkeypatch.setattr(company_module.dom, "return_to_company_tab",
                        lambda page, tab, **kwargs: False)

    # The browser is somewhere unknown. Capturing About now would file the
    # wrong page under the About role, which is worse than capturing nothing.
    assert [item.section for item in sweep(job_pages=2)] == [
        "jobs", "jobs-page-1"]


def test_a_blocking_challenge_on_a_job_page_stops_the_sweep(
        monkeypatch, stubbed):
    """A wall inside the job search ends the run, not just the job pages.
    Four more tabs after a refusal spend the LinkedIn budget learning the same
    thing four times."""
    offering(monkeypatch, "jobs", "about")
    monkeypatch.setattr(company_module.dom, "open_all_jobs", lambda page: True)
    monkeypatch.setattr(company_module.dom, "open_job_page",
                        lambda page, number: True)

    # The Jobs tab itself is clean; the wall appears once we are in the search.
    looks = iter([None, "wall", "wall", "wall"])
    monkeypatch.setattr(company_module.detect, "detect",
                        lambda page: next(looks, "wall"))
    monkeypatch.setattr(company_module.detect, "is_blocking", lambda c: c is not None)

    swept = sweep(job_pages=3)

    assert [item.section for item in swept] == ["jobs", "jobs-page-1"]
    assert swept[-1].stop is True


def test_the_posts_tab_is_bounded_rather_than_followed(monkeypatch, stubbed):
    """A feed has no end to reach. Every other tab does and is read to it."""
    offering(monkeypatch, "posts", "about")
    budgets = {}
    monkeypatch.setattr(
        company_module, "capture_page",
        lambda page, out, slug, **kwargs: budgets.update(
            {slug: kwargs["scroll_budget"]}) or Capture())

    sweep()

    assert budgets["posts"] == company_module.FEED_SCROLL_PX
    assert budgets["about"] == company_module.MAX_SCROLL_PX


def test_company_posts_pause_on_visible_feed_items(monkeypatch, stubbed):
    offering(monkeypatch, "posts")
    options = {}
    monkeypatch.setattr(
        company_module, "capture_page",
        lambda *args, **kwargs: options.update(kwargs) or Capture())

    sweep()

    assert options["read_selector"] == company_module.sections.ACTIVITY_RESULTS


def test_an_explicitly_empty_company_feed_is_captured_without_scrolling(
        monkeypatch, stubbed):
    offering(monkeypatch, "posts")
    monkeypatch.setattr(company_module.dom, "company_feed_is_empty", lambda page: True)
    options = {}
    monkeypatch.setattr(
        company_module, "capture_page",
        lambda *args, **kwargs: options.update(kwargs) or Capture())

    sweep()

    assert options["scroll"] is False
    assert options["read_selector"] is None


def test_about_capture_keeps_locations_and_virtualized_carousel_cards(
        monkeypatch, stubbed):
    offering(monkeypatch, "about")
    snapshots = {"carousels": [{
        "label": "Commitments",
        "items": ["card"],
        "positions_visited": 20,
        "positions_total": 24,
        "reached_end": False,
    }]}
    monkeypatch.setattr(
        company_module.dom, "load_company_carousels",
        lambda page, section: snapshots if section == "about" else None)
    monkeypatch.setattr(
        company_module, "_locations",
        lambda page, company, tab: type("Responses", (), {
            "locations": lambda self: [{"name": "London"}],
            "close": lambda self: None,
        })())
    seen = {}
    monkeypatch.setattr(
        company_module.dom, "company_about_content",
        lambda page, locations, carousels=None: seen.update(
            locations=locations, carousels=carousels) or ("text", b"html"))
    options = {}
    monkeypatch.setattr(
        company_module, "capture_page",
        lambda *args, **kwargs: options.update(kwargs) or Capture())

    captured = sweep()[0].capture
    options["extract_content"](Page())

    assert seen == {
        "locations": [{"name": "London"}],
        "carousels": snapshots,
    }
    assert captured.extra["carousels"] == [{
        "label": "Commitments",
        "positions_visited": 20,
        "positions_total": 24,
        "reached_end": False,
    }]


def test_people_carousel_progress_is_kept_with_the_capture(
        monkeypatch, stubbed):
    offering(monkeypatch, "people")
    monkeypatch.setattr(company_module.dom, "load_people_analytics", lambda page: {
        "cards": [{"title": "Where they live"}],
        "positions_visited": 3,
        "positions_total": 6,
        "reached_end": False,
    })

    captured = sweep()[0].capture

    assert captured.extra["people_analytics"] == {
        "cards_captured": 1,
        "positions_visited": 3,
        "positions_total": 6,
        "reached_end": False,
    }


def test_job_page_records_whether_another_page_was_available(
        monkeypatch, stubbed):
    offering(monkeypatch, "jobs")
    monkeypatch.setattr(company_module.dom, "open_all_jobs", lambda page: True)
    monkeypatch.setattr(company_module.dom, "open_job_page", lambda page, number: False)
    monkeypatch.setattr(
        company_module.dom, "job_results_has_more",
        lambda page, number: number == 1)

    captured = sweep(job_pages=2)[1].capture

    assert captured.extra["has_more"] is True


def test_a_tab_that_never_opened_does_not_leak_its_response_listener(
        monkeypatch, stubbed):
    """The About listener is attached before the click. A page-level listener
    that outlives its tab keeps firing for every later navigation."""
    offering(monkeypatch, "about")
    closed = []

    class Responses:
        def __init__(self, page, slug):
            pass

        def locations(self):
            return []

        def close(self):
            closed.append(True)

    monkeypatch.setattr(company_module.dom, "CompanyLocationResponses", Responses)
    monkeypatch.setattr(company_module.dom, "open_tab", lambda page, tab: False)

    sweep()

    assert closed == [True]


def test_the_sweep_yields_one_section_per_tab_the_nav_offered(
        monkeypatch, stubbed):
    """Note what this does NOT prove: it builds the nav out of the vocabulary,
    so it cannot catch the vocabulary being wrong about LinkedIn. That is the
    test below, and it is the one that caught `products`."""
    from career_lens.linkedin.schema import COMPANY_TAB_NAMES

    offering(monkeypatch, *COMPANY_TAB_NAMES)

    swept = sweep(job_pages=0)

    assert {item.section for item in swept} <= set(COMPANY_TAB_NAMES)
    assert len(swept) == len(COMPANY_TAB_NAMES)


# Label and path exactly as LinkedIn served them, read off a live Stripe
# capture on 2026-08-13. The pair worth keeping is ("Products", ".../product/"):
# the nav says one thing, the URL says another, and a role is the URL's word.
OBSERVED_NAV = (
    ("About", "/company/stripe/about/"),
    ("Posts", "/company/stripe/posts/"),
    ("Products", "/company/stripe/product/"),
    ("Jobs", "/company/stripe/jobs/"),
    ("Life", "/company/stripe/life/"),
    ("People", "/company/stripe/people/"),
)


def test_the_roles_a_real_nav_produces_are_known_to_the_schema(
        monkeypatch, stubbed):
    """The capture layer and the parser share one vocabulary, so a role the
    schema has never heard of is a tab the parser will silently skip.

    Driven from observed markup rather than from the vocabulary, because a
    round trip through the constant agrees with itself no matter what LinkedIn
    actually serves."""
    from career_lens.linkedin.schema import COMPANY_TAB_NAMES

    monkeypatch.setattr(company_module.dom, "tabs", lambda page, company: [
        Tab(label, path) for label, path in OBSERVED_NAV])

    swept = sweep(job_pages=0)

    assert [item.section for item in swept] == [
        "about", "posts", "product", "jobs", "life", "people"]
    assert {item.section for item in swept} <= set(COMPANY_TAB_NAMES)


def test_the_sweep_survives_a_consumer_that_stops_early(monkeypatch, stubbed):
    """A generator yielding from a finally block raises RuntimeError when the
    consumer closes it, so every stop decision is made in normal flow."""
    offering(monkeypatch, "about", "people", "jobs")

    sweeping = company_module.expand(
        Page(), config(), Company("company", "stripe", ""))
    assert next(sweeping).section == "about"
    sweeping.close()          # must not raise
