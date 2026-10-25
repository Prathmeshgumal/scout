from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree, html

from career_lens.google.browser import (
    RESULT_LINKS,
    results_detail,
    environment,
    open_result,
    read_results,
    return_to_serp,
    search,
)
from career_lens.google.challenge import classify_google


FIXTURES = Path(__file__).parent / "fixtures" / "google"


class One:
    def __init__(self, element=None):
        self.element = element

    @property
    def first(self):
        return self

    def inner_text(self):
        return "" if self.element is None else self.element.text_content()

    def is_visible(self):
        return self.element is not None


class Anchor(One):
    def get_attribute(self, name):
        return self.element.get(name)

    def locator(self, selector):
        if selector == "h3":
            found = self.element.xpath(".//h3")
            return One(found[0] if found else None)
        if selector.startswith("xpath=ancestor::div"):
            found = self.element.xpath("ancestor::div[@data-hveid][1]")
            return One(found[0] if found else None)
        raise AssertionError(selector)


class Many:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return Anchor(self.elements[index])


class FixturePage:
    def __init__(self, fixture, *, url="https://www.google.com/search?q=test"):
        self.root = html.fromstring((FIXTURES / fixture).read_text())
        self.url = url

    def locator(self, selector):
        if selector == RESULT_LINKS:
            return Many(self.root.xpath("//a[@href][.//h3]"))
        raise AssertionError(selector)

    def title(self):
        values = self.root.xpath("//title/text()")
        return values[0] if values else ""

    def content(self):
        return etree.tostring(self.root, encoding="unicode")

    def inner_text(self, selector):
        assert selector == "body"
        return self.root.xpath("//body")[0].text_content()


def test_read_results_keeps_rendered_unique_http_destinations_in_order():
    results = read_results(FixturePage("serp.html"))
    assert [(result.rank, result.normalised_url) for result in results] == [
        (1, "https://example.com/a?utm_source=google"),
        (2, "https://www.linkedin.com/in/jane/"),
    ]
    assert results[0].snippet == "A useful first snippet."


def test_result_deduplication_uses_canonical_urls():
    results = read_results(FixturePage("serp-duplicates.html"))
    assert [(result.rank, result.title) for result in results] == [
        (1, "First spelling"), (2, "Example B"),
    ]


def test_read_results_resolves_opaque_google_redirects_from_page_data():
    results = read_results(FixturePage("serp-redirect.html"))

    assert len(results) == 2
    assert results[0].title == "Opaque destination"
    assert results[0].normalised_url == "https://example.com/destination"
    assert results[0].host == "example.com"
    assert results[0].click_href == "/goto?url=opaque-token"
    assert results[0].opaque_redirect is True
    assert results[1].normalised_url == (
        "https://www.linkedin.com/posts/example-activity-1/")


def test_open_result_matches_an_opaque_candidate_by_its_raw_click_href(
        monkeypatch):
    from career_lens.google import browser

    class Navigation:
        value = "destination-response"
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    class Page(FixturePage):
        def expect_navigation(self, **_kwargs):
            return Navigation()

    page = Page("serp-redirect.html")
    item = read_results(page)[0]
    events = []
    monkeypatch.setattr(
        browser.overlays, "dismiss",
        lambda _page: events.append("dismiss"))
    monkeypatch.setattr(
        browser.interaction, "click",
        lambda _page, target, **_kwargs: events.append(
            ("click", target.inner_text())))

    response = open_result(page, item)

    assert response == "destination-response"
    assert events == ["dismiss", ("click", "Opaque destination")]


def test_open_result_retries_once_after_an_intercepted_google_click(
        monkeypatch):
    from career_lens.google import browser

    class Navigation:
        value = "destination-response"
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    class Page(FixturePage):
        def expect_navigation(self, **_kwargs):
            return Navigation()

    page = Page("serp.html")
    item = read_results(page)[0]
    sweeps = []
    clicks = 0
    monkeypatch.setattr(
        browser.overlays, "dismiss",
        lambda _page: sweeps.append("dismiss"))

    def click(_page, _target, **_kwargs):
        nonlocal clicks
        clicks += 1
        if clicks == 1:
            raise TimeoutError("overlay intercepted pointer events")

    monkeypatch.setattr(browser.interaction, "click", click)

    assert open_result(page, item) == "destination-response"
    assert clicks == 2
    assert sweeps == ["dismiss", "dismiss"]


def test_open_result_recovers_when_google_rotates_an_opaque_click_token(
        monkeypatch):
    from career_lens.google import browser

    class Navigation:
        value = "destination-response"
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    class Page(FixturePage):
        def expect_navigation(self, **_kwargs):
            return Navigation()

    page = Page("serp-redirect.html")
    item = read_results(page)[0]
    page.root.xpath("//a[.//h3]")[0].set(
        "href", "/goto?url=rotated-token")
    clicked = []
    monkeypatch.setattr(browser.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(
        browser.interaction, "click",
        lambda _page, target, **_kwargs: clicked.append(target.inner_text()))

    response = open_result(page, item)

    assert response == "destination-response"
    assert clicked == ["Opaque destination"]


def test_opaque_recovery_uses_snippet_identity_when_results_reorder(
        monkeypatch):
    from career_lens.google import browser

    class Navigation:
        value = "destination-response"
        def __enter__(self): return self
        def __exit__(self, *_args): return False

    class Page(FixturePage):
        def expect_navigation(self, **_kwargs):
            return Navigation()

    page = Page("serp-redirect-two.html")
    item = read_results(page)[1]
    search_root = page.root.xpath("//*[@id='search']")[0]
    first, second = search_root.xpath("./div")
    search_root.remove(second)
    search_root.insert(0, second)
    anchors = search_root.xpath(".//a[.//h3]")
    anchors[0].set("href", "/goto?url=rotated-b")
    anchors[1].set("href", "/goto?url=rotated-a")
    clicked = []
    monkeypatch.setattr(browser.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(
        browser.interaction, "click",
        lambda _page, target, **_kwargs: clicked.append(
            target.element.getparent().get("href")))

    open_result(page, item)

    assert clicked == ["/goto?url=rotated-b"]


def test_snippets_are_whitespace_normalised_without_legacy_truncation():
    page = FixturePage("serp.html")
    long_text = "x" * 700
    root = html.fromstring(
        f'<div data-hveid="long"><a href="https://example.com/long">'
        f'<h3>Long result</h3></a><div>{long_text}</div></div>')
    page.locator = lambda _selector: Many(root.xpath(".//a"))
    assert read_results(page)[0].snippet == long_text


def test_result_limit_is_hard_capped_at_sixty():
    page = FixturePage("serp.html")
    anchors = [html.fromstring(
        f'<a href="https://example.com/{index}"><h3>Result {index}</h3></a>')
        for index in range(65)]
    page.locator = lambda _selector: Many(anchors)
    results = read_results(page, limit=100)
    assert len(results) == 60
    assert results[-1].normalised_url == "https://example.com/59"


@pytest.mark.parametrize("navigation_seconds", [1, 31])
def test_search_typing_and_budget_do_not_consume_navigation_deadline(
        monkeypatch, navigation_seconds):
    from career_lens.google import browser

    calls = []
    elapsed = 0

    class Navigation:
        value = object()

        def __enter__(self):
            self.deadline = elapsed + 30
            calls.append("watch")
            return self

        def __exit__(self, *_args):
            if elapsed > self.deadline:
                raise TimeoutError("navigation deadline expired")
            return False

    class Keyboard:
        def press(self, key):
            nonlocal elapsed
            calls.append(key)
            elapsed += navigation_seconds

    class Page:
        url = "https://www.google.com/"
        keyboard = Keyboard()
        def expect_navigation(self, **_kwargs): return Navigation()
        def wait_for_selector(self, *_args, **_kwargs): pass

    def type_query(page, query, **kwargs):
        nonlocal elapsed
        elapsed += 33
        calls.append("type")

    def charge(host):
        nonlocal elapsed
        elapsed += 5
        calls.append(("charge", host))

    monkeypatch.setattr(browser.interaction, "ensure_pointer_in_viewport",
                        lambda *a, **k: None)
    monkeypatch.setattr(browser.overlays, "dismiss", lambda _page: None)
    monkeypatch.setattr(browser, "type_query", type_query)

    if navigation_seconds > 30:
        with pytest.raises(TimeoutError, match="navigation deadline expired"):
            search(Page(), "enterprise AI", before_submit=charge)
    else:
        assert search(Page(), "enterprise AI", before_submit=charge) is Navigation.value
    assert calls == ["type", ("charge", "www.google.com"), "watch", "Enter"]


def test_return_to_serp_verifies_the_original_location():
    class Page:
        url = "https://example.com/a"
        def go_back(self, **_kwargs):
            self.url = "https://www.google.com/search?q=test#fragment"

    assert return_to_serp(
        Page(), "https://www.google.com/search?q=test") is True


def test_return_to_serp_accepts_transient_google_query_parameters():
    class Page:
        url = "https://example.com/a"
        def go_back(self, **_kwargs):
            self.url = (
                "https://www.google.com/search?q=test&sourceid=chrome&ie=UTF-8")

    assert return_to_serp(
        Page(), "https://www.google.com/search?q=test") is True


def test_return_to_serp_uses_the_saved_url_without_retyping():
    class Page:
        url = "https://example.com/a"
        navigated = []
        def go_back(self, **_kwargs):
            self.url = "https://example.com/intermediate"
        def goto(self, url, **kwargs):
            self.navigated.append((url, kwargs))
            self.url = url

    page = Page()
    serp = "https://www.google.com/search?q=test"
    assert return_to_serp(page, serp) is True
    assert page.navigated == [(
        serp,
        {"wait_until": "domcontentloaded", "timeout": 30_000},
    )]


def test_return_to_serp_uses_saved_url_when_history_navigation_times_out():
    class Page:
        url = "https://www.linkedin.com/in/example/"
        navigated = []

        def go_back(self, **_kwargs):
            raise TimeoutError("history navigation did not settle")

        def goto(self, url, **kwargs):
            self.navigated.append((url, kwargs))
            self.url = url

    page = Page()
    serp = "https://www.google.com/search?q=test"
    assert return_to_serp(page, serp) is True
    assert page.navigated == [(
        serp,
        {"wait_until": "domcontentloaded", "timeout": 30_000},
    )]


def test_browser_environment_is_observed_not_overridden():
    class Page:
        def evaluate(self, script):
            assert "navigator.language" in script
            return {"locale": "en-IN", "timezone": "Asia/Kolkata"}

    assert environment(Page()).locale == "en-IN"
    assert environment(Page()).timezone == "Asia/Kolkata"


def test_google_challenge_and_readable_results_are_distinguished():
    challenged = classify_google(
        FixturePage("challenge.html", url="https://www.google.com/sorry/index"),
        None)
    assert challenged.kind == "blocked" and challenged.blocking is True
    assert classify_google(FixturePage("serp.html"), None) is None


def test_google_consent_is_observed_but_not_blocking():
    consent = classify_google(
        FixturePage("challenge.html", url="https://consent.google.com/"), None)
    assert consent.kind == "consent" and consent.blocking is False


def _recorded(fn):
    """Run `fn` under an active trace and return its journal events."""
    from career_lens.capture import trace

    events: list[dict] = []
    trace.activate(["test"], sink=lambda **event: events.append(event))
    try:
        fn()
    finally:
        trace.close()
    return events


def test_reading_the_results_is_journalled_with_ranks_and_hosts():
    """A run that took the detour used to show a SERP capture with a hole
    where the reading and the click were. What was read has to be visible."""
    events = _recorded(lambda: read_results(FixturePage("serp.html")))

    phase = next(e for e in events if e["phase"] == "read results")
    assert "2 results" in phase["detail"]
    assert "example.com" in phase["detail"]
    assert "linkedin.com" in phase["detail"]


def test_a_serp_with_nothing_readable_says_so_rather_than_going_quiet():
    class Empty(FixturePage):
        def locator(self, selector):
            return Many([])

    events = _recorded(lambda: read_results(Empty("serp.html")))

    phase = next(e for e in events if e["phase"] == "read results")
    assert "no results" in phase["detail"]


def test_opening_a_result_journals_which_one_and_where_it_went(monkeypatch):
    from career_lens.google import browser

    page = FixturePage("serp.html")
    results = read_results(page)
    monkeypatch.setattr(browser.interaction, "click", lambda *_a, **_k: None)
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(browser.overlays, "dismiss", lambda *_a, **_k: [])

    class Navigation:
        value = type("Response", (), {"status": 200})()

    class Page(FixturePage):
        def expect_navigation(self, **_kwargs):
            from contextlib import contextmanager

            @contextmanager
            def scope():
                yield Navigation()
            return scope()

    events = _recorded(
        lambda: open_result(Page("serp.html"), results[1]))

    phase = next(e for e in events if e["phase"] == "open result")
    assert "rank 2" in phase["detail"]
    assert "linkedin.com" in phase["detail"]


def test_the_journal_never_carries_the_person_searched_for():
    """The query is a person's name. `type discovery query` already keeps it
    out of the journal and the result phases must not put it back."""
    events = _recorded(lambda: read_results(FixturePage("serp.html")))

    for event in events:
        assert "Jane" not in event["detail"]
        assert "useful first snippet" not in event["detail"]


def test_the_result_phases_survive_the_consoles_default_level():
    """The console's Progress panel filters to `info`, so a phase left in
    trace.ROUTINE is invisible where an operator actually reads it -- which is
    what made the whole Google detour look like a gap in the journal."""
    events = _recorded(lambda: read_results(FixturePage("serp.html")))

    phase = next(e for e in events if e["phase"] == "read results")
    assert phase["level"] == "info"


def test_the_journal_names_the_host_google_actually_offered():
    """`SearchResult.host` comes off the *normalised* URL, and canonical()
    folds LinkedIn country hosts to www -- so the journal reported
    `www.linkedin.com` for a result whose href was `in.linkedin.com`, hiding
    the exact thing the country-host work needs to measure."""
    from career_lens.google.browser import SearchResult, literal_host

    result = SearchResult(
        rank=1,
        url="https://in.linkedin.com/in/jane?trk=public",
        normalised_url="https://www.linkedin.com/in/jane/",
        host="www.linkedin.com",
        title="Jane",
    )

    assert literal_host(result) == "in.linkedin.com"
    assert "in.linkedin.com" in results_detail([result])
