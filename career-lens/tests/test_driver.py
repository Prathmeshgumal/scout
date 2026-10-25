"""Driving a capture, against local fixtures.

What is checked here is the decisions the driver makes -- which pages it
visits, what it does when a page is a challenge, how a failure is classified --
not the browser mechanics, which Task 4 covers.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from career_lens.capture.driver import CaptureFailed, classify_failure


def test_arrival_settles_and_places_a_pointer_like_the_upstream_driver(
        monkeypatch):
    # Still called through `driver._settle_arrival`, which is the same object
    # it always was. The patch targets moved with the code into
    # `capture/arrival.py`; the assertion below is unchanged.
    from career_lens.capture import arrival, driver

    class Page:
        def evaluate(self, script):
            assert script == arrival.interaction.VIEWPORT
            return {"w": 1200, "h": 800}

    class Rng:
        def uniform(self, low, high):
            return (low + high) / 2

    calls = []
    monkeypatch.setattr(arrival.time, "sleep", lambda seconds: calls.append(
        ("sleep", seconds)))
    monkeypatch.setattr(
        arrival.interaction, "ensure_pointer",
        lambda page, x, y, **kwargs: calls.append(("pointer", x, y)) or True)

    driver._settle_arrival(Page(), Rng())

    assert calls == [("sleep", 1.1), ("pointer", 600, 400)]


def test_a_challenge_becomes_blocked():
    from career_lens.capture.detect import Challenge

    # Challenge's fields are kind/evidence/url/status -- there is no `detail`.
    assert classify_failure(
        challenge=Challenge(kind="captcha", evidence="checkpoint",
                            url="https://www.linkedin.com/in/x/"),
        error=None) == "blocked"


def test_a_login_wall_becomes_login_required():
    from career_lens.capture.detect import Challenge

    # linkedin_authwall is the string detect.py actually emits.
    assert classify_failure(
        challenge=Challenge(kind="linkedin_authwall", evidence="authwall",
                            url="https://www.linkedin.com/authwall"),
        error=None) == "login_required"


def test_a_404_becomes_not_found():
    assert classify_failure(challenge=None, error=None, http_status=404) == "not_found"


def test_linkedin_http_200_not_found_page_becomes_not_found(
        monkeypatch, tmp_path):
    """LinkedIn redirects missing profiles to /404/ with an HTTP 200."""
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    requested = "https://www.linkedin.com/in/missing-profile/"

    class Page:
        url = "https://www.linkedin.com/404/"

        def title(self):
            return ""

        def inner_text(self, selector):
            assert selector == "body"
            return "This page doesn't exist\nGo to your feed"

    class Response:
        status = 200

    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver, "wait_for_visuals", lambda _page, **_kwargs: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *_args, **_kwargs: PageCapture(
            url_requested=requested, final_url=Page.url, status=200, ok=True))
    monkeypatch.setattr(
        driver.linkedin_dom, "collect_profile_images",
        lambda *_args, **_kwargs: ({}, None))

    with pytest.raises(CaptureFailed) as raised:
        driver.capture_arrived_page(
            "linkedin_person", requested, Page(), tmp_path,
            response=Response())

    assert raised.value.failure_kind == "not_found"
    assert "/404/" in raised.value.detail


def test_linkedin_not_found_body_is_a_fallback_signal():
    from career_lens.capture import detect

    class Page:
        url = "https://www.linkedin.com/in/missing-profile/"

        def title(self):
            return ""

        def inner_text(self, selector):
            assert selector == "body"
            return "This page doesn't exist\nPlease check your URL."

    challenge = detect.detect(Page())

    assert challenge is not None
    assert challenge.kind == "linkedin_not_found"


def test_not_found_phrase_on_another_site_is_not_a_linkedin_failure():
    from career_lens.capture import detect

    class Page:
        url = "https://example.com/404/"

        def title(self):
            return "Missing article"

        def inner_text(self, _selector):
            return "This page doesn't exist"

    assert detect.detect(Page()) is None


def test_a_visible_login_dialog_blocks_even_when_content_is_behind_it():
    """A login modal owns the interaction despite a substantial page below it."""
    from career_lens.capture import detect

    class Dialogs:
        def all_inner_texts(self):
            return [
                "View Afreen's full profile\nSign in with Email\n"
                "New to LinkedIn? Join now",
            ]

    class Page:
        url = "https://www.linkedin.com/in/afreen-munshi"

        def title(self):
            return "Afreen Munshi - Apple | LinkedIn"

        def inner_text(self, _selector):
            return "the public profile remains rendered " * 200

        def locator(self, selector):
            assert selector == '[role="dialog"]:visible, dialog:visible'
            return Dialogs()

    challenge = detect.detect(Page())

    assert challenge is not None
    assert challenge.kind == "generic_login"


def test_a_timeout_message_becomes_timeout():
    assert classify_failure(
        challenge=None, error="Timeout 30000ms exceeded") == "timeout"


def test_anything_else_becomes_browser_error():
    # The catch-all is deliberately last and deliberately named: an unclassified
    # failure must not silently become one of the specific causes, because v2's
    # retry policy branches on exactly these.
    assert classify_failure(challenge=None, error="target closed") == "browser_error"


def test_a_failure_carries_its_kind_and_a_human_detail():
    failure = CaptureFailed("blocked", "a checkpoint was presented")
    assert failure.failure_kind == "blocked"
    assert "checkpoint" in failure.detail


def test_a_failing_section_is_skipped_not_fatal(monkeypatch, tmp_path):
    # The linkedin_person path's central claim, and until now asserted only by
    # the shape of the code. expand() is stubbed because the alternative is a
    # live profile: what is under test is the driver's response to a failed
    # section, not LinkedIn's DOM.
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture
    from career_lens.linkedin import sections as sectioning

    def ok(name):
        return sectioning.SectionCapture(
            section=name,
            capture=PageCapture(url_requested=f"https://x/{name}/", ok=True,
                                html_bytes=b"<html><body>x</body></html>"))

    def broken(name):
        return sectioning.SectionCapture(
            section=name,
            capture=PageCapture(url_requested=f"https://x/{name}/", ok=False,
                                error="Timeout 30000ms exceeded"))

    monkeypatch.setattr(sectioning, "expand", lambda page, config, slug: iter(
        [ok("experience"), broken("patents"), ok("education")]))

    documents = driver._sections(page=None, out_dir=tmp_path)
    sections = [d.section for d in documents]
    assert sections == ["experience", "education"], "a failed section aborted the rest"
    assert "patents" not in sections


def test_nothing_is_fetched_until_the_generator_is_consumed(monkeypatch, tmp_path):
    """capture_request is a generator, so the browser is not opened by the
    call. A caller that forgets to iterate captures nothing -- and gets no
    CaptureFailed either, which is why every call site had to be updated."""
    from contextlib import contextmanager

    from career_lens.capture import driver

    opened: list[str] = []

    @contextmanager
    def recording_session(*, profile, show_cursor=False):
        opened.append((profile, show_cursor))
        raise AssertionError("the session must not open until iteration")
        yield  # pragma: no cover -- unreachable, makes this a context manager

    monkeypatch.setattr(driver, "session", recording_session)

    driver.capture_request("generic", "https://example.com/",
                           browser_profile="test", out_dir=tmp_path)

    assert opened == []


def test_direct_target_navigation_is_traced_as_network_time(tmp_path):
    from career_lens.capture import driver, trace

    class Page:
        url = "about:blank"

        def goto(self, url, *, wait_until):
            raise RuntimeError("navigation stopped for the test")

    recorded = []
    trace.activate(["test"], sink=lambda **event: recorded.append(event))
    try:
        with pytest.raises(CaptureFailed):
            list(driver.capture_request(
                "generic", "https://example.com/", browser_profile="test",
                out_dir=tmp_path, page=Page()))
    finally:
        trace.close()

    assert [(event["kind"], event["phase"], event["detail"])
            for event in recorded if event["kind"] != "note"] == [
        ("net", "open target", "https://example.com/"),
    ]


def test_worker_cursor_setting_reaches_the_browser_session(
        monkeypatch, tmp_path):
    from contextlib import contextmanager

    from career_lens.capture import driver

    opened = []

    @contextmanager
    def recording_session(*, profile, show_cursor=False):
        opened.append((profile, show_cursor))
        raise RuntimeError("stop after session configuration")
        yield

    monkeypatch.setattr(driver, "session", recording_session)
    monkeypatch.setattr(driver, "SHOW_CURSOR", True, raising=False)

    with pytest.raises(RuntimeError, match="session configuration"):
        list(driver.capture_request(
            "generic", "https://example.com/", browser_profile="test",
            out_dir=tmp_path))

    assert opened == [("test", True)]


def test_section_expansion_uses_the_selected_tempo(monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.linkedin import sections as sectioning

    selected = object()
    received = []
    monkeypatch.setattr(
        sectioning, "expand",
        lambda page, config, slug: received.append(config.tempo) or iter(()))

    # list() first: _sections is a generator, so expand is not called -- and
    # config.tempo not read -- until something pulls from it.
    assert list(driver._sections(None, tmp_path, tempo=selected)) == []
    assert received == [selected]


def test_every_section_failing_still_returns_the_profile(monkeypatch, tmp_path):
    # The boundary case: nothing to skip past. An empty list is correct here --
    # what must not happen is an exception, which would lose the profile page
    # that was already captured successfully.
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture
    from career_lens.linkedin import sections as sectioning

    monkeypatch.setattr(sectioning, "expand", lambda page, config, slug: iter([
        sectioning.SectionCapture(
            section=name,
            capture=PageCapture(url_requested=f"https://x/{name}/", ok=False,
                                error="boom"))
        for name in ("experience", "patents")]))

    assert list(driver._sections(page=None, out_dir=tmp_path)) == []


def test_the_main_profile_capture_strips_account_furniture(monkeypatch, tmp_path):
    """The parent profile gets the cleanup already used by detail sections."""
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture
    from career_lens.linkedin import dom

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def goto(self, _url, *, wait_until):
            assert wait_until == "domcontentloaded"
            return Response()

    @contextmanager
    def browser_session(*, profile):
        assert profile == "test"
        yield Page()

    received = {}

    def capture_page(_page, _out_dir, _slug, **kwargs):
        received.update(kwargs)
        return PageCapture(url_requested=kwargs["url_requested"], ok=True)

    monkeypatch.setattr(driver, "session", browser_session)
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(driver, "capture_page", capture_page)
    monkeypatch.setattr(driver, "_sections", lambda *_args, **_kwargs: [])

    list(driver.capture_request(
        "linkedin_person", "https://www.linkedin.com/in/jane/",
        browser_profile="test", out_dir=tmp_path))

    assert received["strip"] is dom.strip_irrelevant


def test_the_main_profile_expands_clipped_text_before_capture(
        monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture
    from career_lens.linkedin import sections

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def goto(self, _url, *, wait_until):
            return Response()

    @contextmanager
    def browser_session(*, profile):
        yield Page()

    order = []
    monkeypatch.setattr(driver, "session", browser_session)
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(
        sections, "open_clipped_text",
        lambda _page: order.append("expand") or 1)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda _page, _out_dir, _slug, **kwargs: (
            order.append("capture") or PageCapture(
                url_requested=kwargs["url_requested"], ok=True)))
    monkeypatch.setattr(driver, "_sections", lambda *_args, **_kwargs: [])

    list(driver.capture_request(
        "linkedin_person", Page.url, browser_profile="test", out_dir=tmp_path,
        discovery_chance=0))

    assert order == ["expand", "capture"]


def test_selected_sections_do_not_shadow_the_section_helpers(
        monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def goto(self, _url, *, wait_until):
            return Response()

    requested = frozenset({"experience"})
    received = []
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text",
        lambda _page: received.append("expanded") or 0)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda _page, _out_dir, _slug, **kwargs: PageCapture(
            url_requested=kwargs["url_requested"], ok=True))
    monkeypatch.setattr(
        driver, "_sections",
        lambda *_args, **kwargs: received.append(kwargs["requested_sections"]) or [])

    list(driver.capture_request(
        "linkedin_person", Page.url, browser_profile="test",
        out_dir=tmp_path, discovery_chance=0, page=Page(),
        linkedin_sections=requested))

    # The capture sections, not the raw request: a selective profile still reads
    # the three dialogs the structured profile carries as metadata.
    assert received == [
        "expanded",
        frozenset({"experience", "about-this-member", "contact-info",
                   "open-to-work"})]


def test_company_home_consolidates_virtualized_carousels_before_capture(
        monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/company/acme/"

        def goto(self, _url, *, wait_until):
            return Response()

    snapshots = {"carousels": [{
        "label": "Featured",
        "items": ["card"],
        "positions_visited": 2,
        "positions_total": 2,
        "reached_end": True,
    }]}
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        driver.linkedin_dom, "load_company_carousels",
        lambda page, section: snapshots if section == "home" else None)
    monkeypatch.setattr(
        driver.linkedin_dom, "company_on_page",
        lambda page: driver.linkedin_dom.Company("company", "acme", ""))
    monkeypatch.setattr(driver, "_tabs", lambda *_args, **_kwargs: [])
    options = {}
    capture = PageCapture(
        url_requested=Page.url, ok=True, html_bytes=b"<html></html>")
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *args, **kwargs: options.update(kwargs) or capture)

    list(driver.capture_request(
        "linkedin_company", Page.url, browser_profile="test",
        out_dir=tmp_path, discovery_chance=0, page=Page()))

    assert callable(options["extract_content"])
    assert capture.extra["carousels"] == [{
        "label": "Featured",
        "positions_visited": 2,
        "positions_total": 2,
        "reached_end": True,
    }]


def test_unselected_company_home_skips_home_scrolling_and_carousels(
        monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/company/acme/"

        def goto(self, _url, *, wait_until):
            return Response()

    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: False)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        driver.linkedin_dom, "load_company_carousels",
        lambda *_args: pytest.fail("Home carousels must not be traversed"))
    monkeypatch.setattr(
        driver.linkedin_dom, "company_on_page",
        lambda page: driver.linkedin_dom.Company("company", "acme", ""))
    received = {}
    monkeypatch.setattr(
        driver, "_tabs",
        lambda *_args, **kwargs: received.update(tabs=kwargs) or [])
    capture = PageCapture(
        url_requested=Page.url, ok=True, html_bytes=b"<html></html>")
    monkeypatch.setattr(
        driver, "capture_page",
        lambda *args, **kwargs: received.update(capture=kwargs) or capture)

    documents = list(driver.capture_request(
        "linkedin_company", Page.url, browser_profile="test",
        out_dir=tmp_path, discovery_chance=0, page=Page(),
        linkedin_sections=frozenset({"about"}),
    ))

    assert [document.section for document in documents] == ["company"]
    assert received["capture"]["scroll"] is False
    assert received["capture"]["extract_content"] is None
    assert received["tabs"]["requested_sections"] == frozenset({"about"})


def test_the_main_page_recovers_before_challenge_detection(monkeypatch, tmp_path):
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def goto(self, _url, *, wait_until):
            return Response()

    @contextmanager
    def browser_session(*, profile):
        yield Page()

    order = []

    monkeypatch.setattr(driver, "session", browser_session)
    monkeypatch.setattr(driver, "recover_transient_error",
                        lambda _page: order.append("recover"))
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(
        driver.detect, "detect",
        lambda _page, _response: order.append("detect"))
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0)
    monkeypatch.setattr(
        driver, "capture_page",
        lambda _page, _out_dir, _slug, **kwargs: PageCapture(
            url_requested=kwargs["url_requested"], ok=True))
    monkeypatch.setattr(driver, "_sections", lambda *_args, **_kwargs: [])

    list(driver.capture_request(
        "linkedin_person", Page.url,
        browser_profile="test", out_dir=tmp_path))

    assert order == ["recover", "detect"]


def test_arrived_linkedin_page_uses_the_shared_main_capture_flow(
        monkeypatch, tmp_path):
    """A Google arrival must get the exact direct-request main-page behavior."""
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

    order = []
    received = {}
    monkeypatch.setattr(
        driver, "recover_transient_error",
        lambda _page: order.append("recover"))
    monkeypatch.setattr(
        driver.overlays, "dismiss",
        lambda _page: order.append("dismiss") or [])
    monkeypatch.setattr(
        driver.detect, "detect",
        lambda _page, _response: order.append("detect") or None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text",
        lambda _page: order.append("clip"))

    def capture_page(_page, _out_dir, _slug, **kwargs):
        order.append("capture")
        received.update(kwargs)
        return PageCapture(
            url_requested=kwargs["url_requested"], final_url=Page.url,
            status=200, ok=True, html_bytes=b"<main>Jane</main>", text="Jane")

    monkeypatch.setattr(driver, "capture_page", capture_page)

    captured = driver.capture_arrived_page(
        "linkedin_person", Page.url, Page(), tmp_path,
        response=Response(), provenance={"arrival_method": "google_search"})

    assert captured.ok is True
    assert order == ["recover", "dismiss", "detect", "clip", "capture"]
    assert received["strip"] is driver.linkedin_dom.strip_irrelevant
    assert captured.extra["provenance"] == {
        "arrival_method": "google_search",
    }


def test_linkedin_profile_capture_does_not_reload_before_its_scroll(
        monkeypatch, tmp_path):
    """Empty lazy containers are normal until the capture sweep reaches them."""
    from career_lens.capture import driver
    from career_lens.capture.page import PageCapture

    class Response:
        status = 200

    class Page:
        url = "https://www.linkedin.com/in/jane/"

        def reload(self, **_kwargs):
            pytest.fail("the normal profile capture must not reload")

    calls = []
    monkeypatch.setattr(driver, "recover_transient_error", lambda _page: None)
    monkeypatch.setattr(driver.overlays, "dismiss", lambda _page: [])
    monkeypatch.setattr(driver.detect, "detect", lambda _page, _response: None)
    monkeypatch.setattr(driver, "wait_for_visuals", lambda _page, **_kwargs: None)
    monkeypatch.setattr(
        driver.linkedin_sectioning, "open_clipped_text", lambda _page: 0,
    )

    def capture_page(_page, _out_dir, _slug, **kwargs):
        calls.append("capture")
        return PageCapture(url_requested=Page.url, ok=True)

    monkeypatch.setattr(driver, "capture_page", capture_page)

    captured = driver.capture_arrived_page(
        "linkedin_person",
        Page.url,
        Page(),
        tmp_path,
        response=Response(),
        linkedin_sections=frozenset({"experience", "education", "skills"}),
    )

    assert captured.ok is True
    assert calls == ["capture"]


pytest.importorskip("patchright")


@pytest.mark.browser
def test_a_generic_url_produces_exactly_one_document(tmp_path, site):
    # `site` comes from tests/conftest.py, shared with test_capture_page.py.
    from career_lens.capture.driver import capture_request

    documents = list(capture_request(
        "generic", f"{site}/short.html",
        browser_profile="test", out_dir=tmp_path))
    assert len(documents) == 1
    assert documents[0].section == "page"
    assert documents[0].html
    assert documents[0].markdown


@pytest.mark.browser
def test_a_modal_is_dismissed_before_the_page_is_read(tmp_path, site):
    # overlay.html puts a full-screen dialog over the content. The driver runs
    # overlays.dismiss before capture, so the stored HTML is the page rather
    # than the thing covering it -- and a capture of a cookie wall is worth
    # nothing to extraction.
    from career_lens.capture.driver import capture_request

    documents = list(capture_request(
        "generic", f"{site}/overlay.html",
        browser_profile="test", out_dir=tmp_path))
    assert documents[0].html
    assert b"Content behind a modal" in documents[0].html
    assert b'id="modal"' not in documents[0].html


@pytest.mark.browser
def test_a_generic_capture_records_its_markdown_parser(tmp_path, site):
    from career_lens.capture.driver import capture_request

    documents = list(capture_request(
        "generic", f"{site}/short.html",
        browser_profile="test", out_dir=tmp_path))
    # Recorded so that a later parser version can re-derive markdown for pages
    # already captured, rather than only new ones benefiting.
    assert documents[0].markdown_parser == "docling-4"


def test_a_company_request_sweeps_its_tabs_rather_than_taking_one_page(
        monkeypatch):
    """The whole point: /company/stripe/ is nine pages, not one."""
    from pathlib import Path

    from career_lens.capture import driver
    from career_lens.linkedin.company import TabCapture

    class Capture:
        ok = True
        title = "T"
        html_bytes = b"<html></html>"
        url_requested = "https://www.linkedin.com/company/stripe/about/"
        final_url = None
        status = 200
        screenshot_bytes = []
        # A capture carries these since profile and cover images were stored;
        # document() reads them unconditionally.
        images = {}
        text = "page text"
        text_chars = 10
        scroll_stopped = "end"
        scroll_growing = False
        screenshot_error = None
        request_method = "GET"
        request_headers = {}
        response_headers = {}
        extra = {}

    monkeypatch.setattr(
        driver.company_module, "expand",
        lambda page, config, org: iter([TabCapture("about", Capture()),
                                        TabCapture("people", Capture())]))

    documents = list(driver._tabs(
        object(), Path("/tmp"),
        company=driver.linkedin_dom.Company("company", "stripe", ""),
        tempo=None))

    assert [document.section for document in documents] == ["about", "people"]


def test_company_request_sections_are_bounded_by_worker_tabs(monkeypatch):
    from pathlib import Path

    from career_lens.capture import driver

    received = {}

    def expand(page, config, company):
        received["tabs"] = config.tabs
        return iter(())

    monkeypatch.setattr(driver.company_module, "expand", expand)
    before = driver.TABS
    try:
        driver.TABS = frozenset({"about"})
        list(driver._tabs(
            object(), Path("/tmp"),
            company=driver.linkedin_dom.Company("company", "stripe", ""),
            requested_sections=frozenset({"people"}),
        ))
    finally:
        driver.TABS = before

    assert received["tabs"] is None


def test_a_tab_that_failed_is_not_stored(monkeypatch):
    """A failed capture carries no usable page. Storing it would file an empty
    row under a real section, which reads later as a tab that was genuinely bare."""
    from pathlib import Path

    from career_lens.capture import driver
    from career_lens.linkedin.company import TabCapture

    class Failed:
        ok = False
        html_bytes = None

    monkeypatch.setattr(
        driver.company_module, "expand",
        lambda page, config, org: iter([TabCapture("events", Failed(),
                                                   stop=True)]))

    documents = list(driver._tabs(
        object(), Path("/tmp"),
        company=driver.linkedin_dom.Company("company", "stripe", ""),
        tempo=None))

    assert documents == []


def test_the_request_job_pages_reach_the_sweep(monkeypatch):
    from pathlib import Path

    from career_lens.capture import driver

    received = {}

    def expand(page, config, company):
        received["job_pages"] = config.job_pages
        return iter(())

    monkeypatch.setattr(driver.company_module, "expand", expand)
    list(driver._tabs(
        object(), Path("/tmp"),
        company=driver.linkedin_dom.Company("company", "stripe", ""),
        requested_sections=frozenset({"jobs"}), job_pages=7,
    ))
    assert received["job_pages"] == 7


def test_the_worker_tab_bound_reaches_the_sweep():
    """configure() is how a CLI flag crosses a Procrastinate task boundary."""
    from career_lens.capture import driver

    before = driver.TABS
    try:
        driver.configure(tabs=frozenset({"about"}))
        assert driver.TABS == frozenset({"about"})
    finally:
        driver.TABS = before


def test_document_preserves_complete_rendered_text():
    from career_lens.capture.driver import document
    from career_lens.capture.page import PageCapture

    capture = PageCapture(
        url_requested="https://example.com/", final_url="https://example.com/",
        text="complete rendered text", text_chars=22,
    )

    assert document(capture, "page").text == "complete rendered text"


class _Recorder:
    """Stands in for HarRecorder: records what each Document drained."""

    def __init__(self):
        self.takes = []
        self.closed = False

    def take(self, section, *, url=None, title=None):
        self.takes.append(section)
        return f"har-for-{section}".encode()

    def close(self):
        self.closed = True


def test_each_document_carries_the_har_for_its_own_page():
    """One drain per Document, so no capture holds another's traffic."""
    from career_lens.capture.driver import document
    from career_lens.capture.page import PageCapture

    recorder = _Recorder()
    first = document(PageCapture(url_requested="https://x.invalid/a"),
                     "profile", recorder)
    second = document(PageCapture(url_requested="https://x.invalid/b"),
                      "skills", recorder)

    assert recorder.takes == ["profile", "skills"]
    assert first.har == b"har-for-profile"
    assert second.har == b"har-for-skills"


def test_without_a_recorder_a_document_has_no_har():
    from career_lens.capture.driver import document
    from career_lens.capture.page import PageCapture

    assert document(PageCapture(url_requested="https://x.invalid/"),
                    "page").har is None


@pytest.mark.parametrize("mode, expected", [
    ("off", None), ("headers", False), ("bodies", True)])
def test_the_har_setting_decides_whether_bodies_are_fetched(
        monkeypatch, mode, expected):
    from career_lens.capture import driver

    class FakePage:
        context = object()

    monkeypatch.setattr(driver, "HAR_MODE", mode)
    monkeypatch.setattr(driver.HarRecorder, "attach", lambda self, ctx: None)
    recorder = driver._new_recorder(FakePage())

    if expected is None:
        assert recorder is None
    else:
        assert recorder.bodies is expected


def test_a_recorder_that_cannot_attach_does_not_fail_the_capture(monkeypatch):
    """Evidence about the capture must never cost the capture itself."""
    from career_lens.capture import driver

    class FakePage:
        context = object()

    def explode(self, context):
        raise RuntimeError("context went away")

    monkeypatch.setattr(driver, "HAR_MODE", "bodies")
    monkeypatch.setattr(driver.HarRecorder, "attach", explode)
    assert driver._new_recorder(FakePage()) is None


def test_driver_reexports_neutral_capture_helpers():
    """The neutral halves moved out; the driver's names did not.

    `google/recipe.py` and every existing test import these from the driver.
    Moving them into site-neutral modules must not make the driver's spelling
    a second, drifting definition.
    """
    from career_lens.capture import documents, driver, failures

    assert driver.CaptureFailed is failures.CaptureFailed
    assert driver.classify_failure is failures.classify_failure
    assert driver.document is documents.to_document


def test_the_neutral_modules_import_no_linkedin_package():
    # A public page capture importing LinkedIn selectors would be the first
    # step back toward one driver that knows every site. The detect vocabulary
    # still names `linkedin_authwall`, which is a string the shared detector
    # emits rather than a dependency, so this checks imports rather than text.
    import ast
    import inspect

    from career_lens.capture import documents, failures

    for module in (documents, failures):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "linkedin" not in (node.module or "")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "linkedin" not in alias.name


def test_driver_reexports_neutral_arrival_pacing():
    """Arrival settle and dwell moved out; the driver's own names did not.

    Both are site-neutral -- a pause and a pointer placement -- and other
    sites need them to honour a tempo. LinkedIn's behaviour must be the same object,
    not a second copy that can drift.
    """
    from career_lens.capture import arrival, driver

    assert driver._settle_arrival is arrival.settle_arrival
    assert driver._dwell is arrival.dwell
    assert driver.ARRIVAL_SETTLE_RANGE == arrival.ARRIVAL_SETTLE_RANGE
