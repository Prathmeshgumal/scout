"""Google discovery stays optional, exact, and outside profile extraction."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from career_lens.capture.page import PageCapture
from career_lens.capture.writer import Document


def test_google_search_initializes_the_pointer_before_typing(monkeypatch):
    from career_lens.capture import discovery
    from career_lens.google import browser

    calls = []

    class Navigation:
        value = object()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class Page:
        url = "about:blank"

        def goto(self, *_args, **_kwargs):
            calls.append("goto")

        def expect_navigation(self, **_kwargs):
            return Navigation()

        def wait_for_selector(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        browser.interaction, "ensure_pointer_in_viewport",
        lambda page, **kwargs: calls.append("pointer") or True,
        raising=False)
    monkeypatch.setattr(
        browser.overlays, "dismiss", lambda page: calls.append("overlays"))
    monkeypatch.setattr(
        browser, "type_query",
        lambda page, query, **kwargs: calls.append("type"))
    monkeypatch.setattr(
        browser, "_submit_query",
        lambda page, host, callback: calls.append("submit"))

    discovery.search(Page(), "acme LinkedIn", before_submit=lambda _host: None)

    assert calls == ["goto", "pointer", "overlays", "type", "submit"]


def test_only_linkedin_person_and_company_roots_are_eligible():
    from career_lens.capture.discovery import discovery_profile

    assert discovery_profile("https://www.linkedin.com/in/jane-doe/").slug == "jane-doe"
    assert discovery_profile("https://uk.linkedin.com/company/acme/?trk=x").kind == "company"
    assert discovery_profile("https://www.linkedin.com/in/jane-doe/details/experience/") is None
    assert discovery_profile("https://www.linkedin.com/company/acme/jobs/") is None
    assert discovery_profile("https://www.linkedin.com/company/search/") is None
    assert discovery_profile("https://example.com/in/jane-doe/") is None


def test_discovery_matches_country_hosts_but_not_a_child_route():
    from career_lens.capture.discovery import (
        SearchResult,
        discovery_profile,
        matching_result,
    )

    profile = discovery_profile("https://www.linkedin.com/in/jane-doe/")
    results = [
        SearchResult(
            1, "https://www.linkedin.com/in/jane-doe/posts/",
            "https://www.linkedin.com/in/jane-doe/posts/", "linkedin.com",
            "Posts"),
        SearchResult(
            2, "https://in.linkedin.com/in/jane-doe?trk=public",
            "https://www.linkedin.com/in/jane-doe/", "in.linkedin.com",
            "Jane"),
    ]

    assert matching_result(profile, results).rank == 2


def test_probability_boundaries_skip_or_always_select():
    from career_lens.capture.discovery import selected_profile

    class Rng:
        def __init__(self):
            self.calls = 0

        def random(self):
            self.calls += 1
            return 0.99

    rng = Rng()
    target = "https://www.linkedin.com/in/jane-doe/"
    assert selected_profile(target, 0, rng=rng) is None
    assert selected_profile(target, 1, rng=rng).slug == "jane-doe"
    assert rng.calls == 0


def _driver_stubs(monkeypatch, page, main_capture):
    from career_lens.capture import driver

    @contextmanager
    def browser_session(*, profile):
        assert profile == "test"
        yield page

    monkeypatch.setattr(driver, "session", browser_session)
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(driver, "capture_page", lambda *_args, **_kwargs: main_capture)
    monkeypatch.setattr(driver, "_sections", lambda *_args, **_kwargs: [])
    return driver


def test_successful_discovery_keeps_the_serp_and_avoids_direct_navigation(
        monkeypatch, tmp_path):
    from career_lens.capture import discovery

    target = "https://www.linkedin.com/in/jane-doe/"

    class Page:
        url = target

        def __init__(self):
            self.gotos = []

        def goto(self, url, **_kwargs):
            self.gotos.append(url)
            return type("Response", (), {"status": 200})()

    page = Page()
    serp = PageCapture(
        url_requested="https://www.google.com/search?q=jane",
        final_url="https://www.google.com/search?q=jane", ok=True,
        html_bytes=b"<html>results</html>")
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            serp=serp, arrived=True,
            response=type("Response", (), {"status": 200})()))

    documents = list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert [document.section for document in documents] == ["discovery-serp", "profile"]
    assert page.gotos == []


def test_a_country_host_result_is_left_before_anything_is_captured(
        monkeypatch, tmp_path):
    """LinkedIn usually redirects in.linkedin.com to www when the session is
    carried, and sometimes serves a login wall instead. Waiting to find out
    which costs a whole run, so the host is corrected on arrival -- and the
    capture must happen after that, never on the regional page.
    """
    from career_lens.capture import discovery

    target = "https://www.linkedin.com/in/alice/"
    order = []

    class Page:
        def __init__(self):
            self.url = "https://in.linkedin.com/in/alice/"
            self.gotos = []

        def goto(self, url, **_kwargs):
            self.gotos.append(url)
            order.append(("goto", url))
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *_args, **_kwargs: order.append(("capture", page.url)) or main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200})()))

    documents = list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert page.gotos == [target]
    assert order == [("goto", target), ("capture", target)]
    assert [document.section for document in documents] == ["profile"]
    assert documents[0].host == "linkedin.com"


def test_a_result_that_redirects_itself_to_www_is_not_navigated_again(
        monkeypatch, tmp_path):
    """The correction is for the host we are actually on, so a click LinkedIn
    already redirected costs no extra navigation."""
    from career_lens.capture import discovery

    target = "https://www.linkedin.com/in/alice/"

    class Page:
        def __init__(self):
            self.url = target
            self.gotos = []

        def goto(self, url, **_kwargs):
            self.gotos.append(url)
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200})()))

    list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert page.gotos == []


def test_a_generic_target_is_never_host_corrected(monkeypatch, tmp_path):
    """Only LinkedIn folds country hosts. Any other site's subdomain is a
    different page, and moving to it would be a bug."""
    from career_lens.capture import discovery

    target = "https://example.com/a/"

    class Page:
        def __init__(self):
            self.url = "https://de.example.com/a/"
            self.gotos = []

        def goto(self, url, **_kwargs):
            self.gotos.append(url)
            self.url = url
            return type("Response", (), {"status": 200, "url": url})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=page.url, ok=True,
                       html_bytes=b"<html>page</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200, "url": page.url})()))

    list(driver.capture_request(
        "generic", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert page.gotos == []


def test_missing_google_result_falls_back_to_direct_navigation(
        monkeypatch, tmp_path):
    from career_lens.capture import discovery

    target = "https://www.linkedin.com/in/jane-doe/"

    class Page:
        url = target

        def __init__(self):
            self.gotos = []

        def goto(self, url, **_kwargs):
            self.gotos.append(url)
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    serp = PageCapture(
        url_requested="https://www.google.com/search?q=jane",
        final_url="https://www.google.com/search?q=jane", ok=True,
        html_bytes=b"<html>no match</html>")
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(serp=serp))

    documents = list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert page.gotos == [target]
    assert [document.section for document in documents] == ["discovery-serp", "profile"]


def test_discovery_capture_is_not_sent_to_the_parser_or_projection(monkeypatch):
    from career_lens.queue import tasks

    def document(section: str, html: bytes, url: str, *, metadata=None) -> Document:
        return Document(
            section=section, url_requested=url, url_final=url,
            host="example.test", http_status=200, captured_at=datetime.now(UTC),
            title=None, html=html, metadata=metadata or {})

    provenance = {
        "arrival_method": "google_search",
        "requested_url": "https://www.linkedin.com/in/jane/",
        "search_engine": "google",
        "search_query": "jane LinkedIn",
        "search_result_rank": 1,
    }
    documents = [
        document("discovery-serp", b"<html>google</html>",
                 "https://www.google.com/search?q=jane"),
        document("profile", b"<html>profile</html>",
                 "https://www.linkedin.com/in/jane/", metadata={
                     "provenance": provenance,
                 }),
        document("education", b"<html>education</html>",
                 "https://www.linkedin.com/in/jane/details/education/"),
    ]
    seen = {}

    def parse(_profile, sections, **kwargs):
        seen["sections"] = sections
        seen["section_urls"] = kwargs["section_urls"]
        return {"schema_version": "1.1", "profile": {}}

    monkeypatch.setattr(
        tasks, "parse_profile_html", parse)
    monkeypatch.setattr(
        tasks, "store_extraction",
        lambda _url, **kwargs: seen.update(
            capture_ids=kwargs["capture_ids"], document=kwargs["document"]
        ))

    tasks._extract(
        "rq-test", "https://www.linkedin.com/in/jane/", documents,
        {"discovery-serp": "cap-google", "profile": "cap-profile",
         "education": "cap-education"},
        store=object())

    assert seen["sections"] == {"education": "<html>education</html>"}
    assert seen["section_urls"] == {
        "education": "https://www.linkedin.com/in/jane/details/education/"}
    assert seen["capture_ids"] == {
        "profile": "cap-profile", "education": "cap-education"}
    assert seen["document"]["provenance"] == provenance


def test_a_discovery_that_gave_up_says_why(monkeypatch, tmp_path):
    """discover() computes a reason on each of its six give-up paths, and the
    driver used to discard every one of them.

    The operator then saw only whatever the fallback navigation happened to
    raise -- `browser_error: ERR_ABORTED at linkedin.com` -- attributed to
    LinkedIn, with nothing anywhere naming Google as the cause.
    """
    from career_lens.capture import discovery, trace

    target = "https://www.linkedin.com/in/jane-doe/"

    class Page:
        url = target

        def goto(self, url, **_kwargs):
            return type("Response", (), {"status": 200})()

    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, Page(), main)

    notes: list[str] = []
    monkeypatch.setattr(trace, "note", notes.append)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            detail="search failed: TimeoutError: navigation never settled"))

    documents = list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    # The run still succeeds -- a Google failure is never fatal to the target.
    assert [document.section for document in documents] == ["profile"]
    assert notes == [
        "discovery: search failed: TimeoutError: navigation never settled"
        " · navigating directly"]


def test_a_discovery_that_worked_records_how_it_arrived(monkeypatch, tmp_path):
    """Success used to be silent, which made "did this run go through Google?"
    answerable only from the capture's provenance column. It is a question an
    operator asks of the journal, so the journal answers it -- without the
    query, which is a person's name and belongs only in provenance.
    """
    from career_lens.capture import discovery, trace

    target = "https://www.linkedin.com/in/jane-doe/"

    class Page:
        url = target

        def goto(self, url, **_kwargs):
            return type("Response", (), {"status": 200})()

    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, Page(), main)

    notes: list[str] = []
    monkeypatch.setattr(trace, "note", notes.append)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200})(),
            provenance={
                "arrival_method": "google_search", "requested_url": target,
                "search_engine": "google", "search_query": "jane doe LinkedIn",
                "search_result_rank": 1,
            }))

    documents = list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert notes == ["discovery: arrived through Google (rank 1)"]
    assert "jane doe" not in " ".join(notes)
    assert documents[-1].metadata["provenance"] == {
        "arrival_method": "google_search", "requested_url": target,
        "search_engine": "google", "search_query": "jane doe LinkedIn",
        "search_result_rank": 1,
    }


def test_discovery_forwards_the_search_budget_callback(monkeypatch, tmp_path):
    from career_lens.capture import discovery, referral

    charged = []

    def search(_page, _query, *, before_submit, **_kwargs):
        before_submit("www.google.com")
        raise RuntimeError("stop after charge")

    monkeypatch.setattr(referral, "search", search)
    attempt = discovery.discover(
        object(), "https://www.linkedin.com/in/jane-doe/", tmp_path,
        chance=1, before_search=charged.append)

    assert charged == ["www.google.com"]
    assert attempt.detail.startswith("search failed")


def test_exhausted_search_budget_falls_back_with_a_stable_reason(
        monkeypatch, tmp_path):
    from career_lens.capture import discovery, referral
    from career_lens.google.budget import SearchBudgetExhausted

    monkeypatch.setattr(
        referral, "search",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SearchBudgetExhausted("hourly limit")))
    attempt = discovery.discover(
        object(), "https://www.linkedin.com/in/jane-doe/", tmp_path, chance=1)

    assert attempt.detail == "google search budget exhausted"


def test_typing_reports_the_rate_and_the_think_pauses_it_took(monkeypatch):
    """The two numbers that separate "our own sleeps were slow" from "the
    browser was". Both existed upstream in flow.py and were dropped in the
    port, which is why a 40-second type of 22 characters could not be
    explained from the journal alone.
    """
    import random

    from career_lens.capture import discovery, trace
    from career_lens.google import browser

    class Keyboard:
        def type(self, _char): pass
        def press(self, _keys): pass

    class Page:
        keyboard = Keyboard()

        def locator(self, _selector):
            return type("Locator", (), {"first": None})()

    monkeypatch.setattr(browser.interaction, "click", lambda *_a, **_k: None)
    # The delays are the thing under test, not something to sit through.
    monkeypatch.setattr(browser.time, "sleep", lambda _seconds: None)

    recorded: list[str] = []
    trace.activate(["test"], sink=lambda **event: recorded.append(event["detail"]))
    try:
        # Seeded, so the think-pause count is deterministic rather than flaky.
        discovery.type_query(Page(), "afreen munshi LinkedIn",
                             rng=random.Random(7))
    finally:
        trace.close()

    detail = next(entry for entry in recorded if "22 chars" in entry)
    assert "afreen" not in detail
    assert "22 chars" in detail
    assert "@ ~110ms" in detail, detail
    assert "think-pause(s)" in detail, detail


def _traced(fn):
    from career_lens.capture import trace

    events: list[dict] = []
    trace.activate(["test"], sink=lambda **event: events.append(event))
    try:
        fn()
    finally:
        trace.close()
    return [event["phase"] for event in events if event["kind"] == "note"]


def _run(monkeypatch, tmp_path, attempt, *, target="https://www.linkedin.com/in/jane/"):
    from career_lens.capture import discovery

    class Page:
        def __init__(self):
            self.url = target

        def goto(self, url, **_kwargs):
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(
        discovery, "discover", lambda *_args, **_kwargs: attempt)
    return lambda: list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))


def test_a_run_that_arrived_through_google_says_so_in_the_journal(
        monkeypatch, tmp_path):
    """Whether a run took the detour was only recoverable from the capture's
    provenance column. The journal is where an operator looks."""
    from career_lens.capture import discovery

    notes = _traced(_run(monkeypatch, tmp_path, discovery.DiscoveryAttempt(
        arrived=True, response=type("Response", (), {"status": 200})(),
        provenance={"arrival_method": "google_search",
                    "search_result_rank": 2})))

    assert any("arrived through Google" in note and "rank 2" in note
               for note in notes), notes


def test_a_detour_that_gave_up_names_the_reason_and_the_fallback(
        monkeypatch, tmp_path):
    from career_lens.capture import discovery

    notes = _traced(_run(monkeypatch, tmp_path, discovery.DiscoveryAttempt(
        detail="exact profile result not found")))

    note = next(n for n in notes if n.startswith("discovery:"))
    assert "exact profile result not found" in note
    assert "directly" in note


def test_a_run_that_never_tried_google_still_says_which_way_it_went(
        monkeypatch, tmp_path):
    notes = _traced(_run(monkeypatch, tmp_path, None))

    assert any("discovery:" in note and "directly" in note
               for note in notes), notes


def test_the_arrived_page_is_settled_before_it_is_photographed(
        monkeypatch, tmp_path):
    """The main profile and company pages had no readiness gate at all -- only
    detail sections and the activity feed did -- so the first screen was shot
    over whatever LinkedIn had mounted by then."""
    from career_lens.capture import discovery, driver as driver_module

    target = "https://www.linkedin.com/in/jane/"
    order = []

    class Page:
        def __init__(self):
            self.url = target

        def goto(self, url, **_kwargs):
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>profile</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    visual_scopes = []
    monkeypatch.setattr(
        driver_module, "wait_for_visuals",
        lambda _page, **kwargs: visual_scopes.append(kwargs.get("scope")) or order.append("settle") or 0)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *_args, **_kwargs: order.append("capture") or main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200})()))

    list(driver.capture_request(
        "linkedin_person", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert order == ["settle", "capture"]
    assert visual_scopes == [driver_module.linkedin_sectioning.PRIMARY_COLUMN_SELECTORS]


def test_a_company_page_gets_the_same_gate(monkeypatch, tmp_path):
    from career_lens.capture import discovery, driver as driver_module

    target = "https://www.linkedin.com/company/stripe/"
    order = []

    class Page:
        def __init__(self):
            self.url = target

        def goto(self, url, **_kwargs):
            self.url = url
            return type("Response", (), {"status": 200})()

    page = Page()
    main = PageCapture(url_requested=target, final_url=target, ok=True,
                       html_bytes=b"<html>company</html>")
    driver = _driver_stubs(monkeypatch, page, main)
    monkeypatch.setattr(driver.linkedin_dom, "load_company_carousels",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(driver.linkedin_dom, "company_carousel_progress",
                        lambda *_a, **_k: None)
    monkeypatch.setattr(driver.linkedin_dom, "collect_profile_images",
                        lambda *_a, **_k: ({}, None))
    monkeypatch.setattr(driver.linkedin_dom, "strip_irrelevant",
                        lambda *_a, **_k: 0)
    monkeypatch.setattr(driver.company_module, "expand",
                        lambda *_a, **_k: iter(()))
    visual_scopes = []
    monkeypatch.setattr(
        driver_module, "wait_for_visuals",
        lambda _page, **kwargs: visual_scopes.append(kwargs.get("scope")) or order.append("settle") or 0)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *_args, **_kwargs: order.append("capture") or main)
    monkeypatch.setattr(
        discovery, "discover",
        lambda *_args, **_kwargs: discovery.DiscoveryAttempt(
            arrived=True, response=type("Response", (), {"status": 200})()))

    list(driver.capture_request(
        "linkedin_company", target, browser_profile="test", out_dir=tmp_path,
        discovery_chance=1))

    assert order == ["settle", "capture"]
    assert visual_scopes == [driver_module.linkedin_sectioning.PRIMARY_COLUMN_SELECTORS]
