from __future__ import annotations

import pytest

from career_lens.capture.driver import CaptureFailed
from career_lens.capture.page import PageCapture
from career_lens.google.browser import BrowserEnvironment, SearchResult
from career_lens.google.recipe import classify_destination, run


class Page:
    url = "https://www.google.com/search?q=AI"

    def locator(self, _selector):
        return type("Candidates", (), {"count": lambda self: 1})()


def result(rank, url):
    from career_lens.capture.urls import canonical
    from urllib.parse import urlsplit

    normalised = canonical(url)
    return SearchResult(
        rank, url, normalised, urlsplit(normalised).hostname or "",
        f"Result {rank}", f"Snippet {rank}")


def capture(url, text, *, screenshot_error=None):
    return PageCapture(
        url_requested=url, final_url=url, status=200, title="Captured",
        ok=True, text=text, text_chars=len(text), html_bytes=b"<html></html>",
        scroll_stopped="bottom", scroll_growing=False,
        screenshot_error=screenshot_error,
    )


def setup(monkeypatch, results):
    from career_lens.google import recipe

    page = Page()
    monkeypatch.setattr(
        recipe, "search", lambda *a, **k: type("Response", (), {"status": 200})())
    monkeypatch.setattr(recipe, "read_results", lambda _page: results)
    monkeypatch.setattr(recipe, "classify_google", lambda *a: None)
    monkeypatch.setattr(
        recipe, "environment", lambda _page: BrowserEnvironment("en-IN", "Asia/Kolkata"))
    monkeypatch.setattr(recipe, "return_to_serp", lambda *a, **k: True)
    monkeypatch.setattr(
        recipe, "open_result",
        lambda _page, item, **kwargs: type("Response", (), {"status": 200})())
    monkeypatch.setattr(recipe.overlays, "dismiss", lambda _page: [])

    def capture_page(_page, _out, slug, **kwargs):
        if slug == "serp":
            return capture(page.url, "complete serp text")
        return capture(kwargs["url_requested"], f"complete text for {slug}")

    monkeypatch.setattr(recipe, "capture_page", capture_page)
    return page


def test_destination_classification_is_shallow_only_for_entity_roots():
    assert classify_destination(
        "https://in.linkedin.com/in/jane/?trk=x") == "linkedin_shallow"
    assert classify_destination(
        "https://www.linkedin.com/company/acme/") == "linkedin_shallow"
    assert classify_destination(
        "https://www.linkedin.com/jobs/view/1/") == "generic"
    assert classify_destination("https://example.com/a") == "generic"


def test_search_only_resolves_a_full_first_page_without_visiting_destinations(
        monkeypatch, tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace
    from career_lens.google.browser_contract import REDIRECT_RESOLUTIONS

    original = [replace(result(i, f"https://www.google.com/goto?url=token{i}"),
                        click_href=f"/goto?url=token{i}", opaque_redirect=True)
                for i in range(1, REDIRECT_RESOLUTIONS + 2)]
    page = setup(monkeypatch, original)
    calls = []
    disposed = []

    def get(url, **kwargs):
        assert url.startswith("https://www.google.com/goto?")
        assert kwargs["max_redirects"] == 0
        assert kwargs["max_retries"] == 0
        assert 0 < kwargs["timeout"] <= 3000
        calls.append(url)
        return SimpleNamespace(status=302, headers={
            "location": "https://ca.linkedin.com/company/example"},
            dispose=lambda: disposed.append(url))

    page.context = SimpleNamespace(request=SimpleNamespace(get=get))
    output = run(page, query="Example", top_n=0, out_dir=tmp_path,
                 before_search=lambda _: None, linkedin_host=lambda: None,
                 persist=lambda doc: f"cp-{doc.section}")
    assert REDIRECT_RESOLUTIONS >= 10  # a full first page, not the old five
    assert len(calls) == len(disposed) == REDIRECT_RESOLUTIONS
    assert [item.search_result.url for item in output.results] == [
        "https://ca.linkedin.com/company/example", original[REDIRECT_RESOLUTIONS].url]
    assert [item.rank for item in output.results] == [1, 2]
    assert output.results[0].search_result.title == original[0].title
    assert all(item.capture is None for item in output.results)
    assert output.results[0].search_result.click_href == original[0].click_href
    assert page.url == "https://www.google.com/search?q=AI"


@pytest.mark.parametrize("outcome", ["timeout", "html", "missing", "unsafe", "blocked", "closed"])
def test_search_redirect_failure_keeps_results_and_does_not_visit_provider(outcome):
    from dataclasses import replace
    from types import SimpleNamespace
    from patchright.sync_api import TimeoutError, Error
    from career_lens.capture.failures import BrowserGone
    from career_lens.google.browser import resolve_search_redirects

    item = replace(result(1, "https://www.google.com/goto?url=opaque"),
                   opaque_redirect=True, click_href="/goto?url=opaque")
    calls = []
    disposed = []

    def get(url, **kwargs):
        calls.append(url)
        if outcome == "timeout":
            raise TimeoutError("timed out")
        if outcome == "closed":
            raise Error("Target page, context or browser has been closed")
        return SimpleNamespace(
            status={"html": 200, "blocked": 429}.get(outcome, 302),
            headers={"location": "javascript:alert(1)"} if outcome == "unsafe" else {},
            dispose=lambda: disposed.append(True))

    page = SimpleNamespace(url="https://www.google.com/search?q=example",
                           context=SimpleNamespace(request=SimpleNamespace(get=get)))
    if outcome == "closed":
        with pytest.raises(BrowserGone):
            resolve_search_redirects(page, (item,))
    else:
        assert resolve_search_redirects(page, (item,)) == (item,)
    assert calls == [item.url]
    assert len(disposed) == (0 if outcome in {"timeout", "closed"} else 1)


def test_search_redirect_limit_counts_direct_results_and_skips_already_resolved_links():
    from dataclasses import replace
    from types import SimpleNamespace
    from career_lens.google.browser import resolve_search_redirects
    from career_lens.google.browser_contract import REDIRECT_RESOLUTIONS

    direct = [result(i, f"https://example.com/{i}") for i in range(1, REDIRECT_RESOLUTIONS + 1)]
    direct[0] = replace(direct[0], opaque_redirect=True, click_href="/goto?url=known")
    opaque = replace(result(REDIRECT_RESOLUTIONS + 1, "https://www.google.com/goto?url=opaque"),
                     opaque_redirect=True)
    # No request client: any attempt to fetch a direct/already resolved result fails.
    page = SimpleNamespace(url="https://www.google.com/search?q=example")
    original = tuple([*direct, opaque])
    assert resolve_search_redirects(page, original) == original


def test_nonstandard_response_gets_one_delayed_refresh(monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://example.com/jobs/1")])
    calls = []
    page.wait_for_timeout = lambda ms: calls.append(ms)
    page.reload = lambda **kwargs: calls.append("reload") or type("Response", (), {"status": 200})()
    monkeypatch.setattr(recipe, "open_result",
                        lambda *args, **kwargs: type("Response", (), {"status": 999})())
    output = run(page, query="jobs", top_n=1, out_dir=tmp_path,
                 before_search=lambda _: None, linkedin_host=lambda: None,
                 persist=lambda doc: f"cp-{doc.section}")
    assert calls == [1000, "reload"]
    assert output.results[0].capture.status == "succeeded"


def test_generic_linkedin_result_uses_linkedin_personal_content_cleanup(
        monkeypatch, tmp_path):
    from career_lens.google import recipe
    from career_lens.linkedin import dom as linkedin_dom

    linkedin_post = "https://www.linkedin.com/posts/jane-example_activity-1"
    page = setup(monkeypatch, [
        result(1, linkedin_post),
        result(2, "https://example.com/article"),
    ])
    destination_strips = []

    def opened(_page, item, **_kwargs):
        page.url = item.normalised_url
        return type("Response", (), {"status": 200})()

    def capture_page(_page, _out, slug, **kwargs):
        if slug == "serp":
            return capture(page.url, "complete serp text")
        destination_strips.append(kwargs.get("strip"))
        return capture(kwargs["url_requested"], f"complete text for {slug}")

    monkeypatch.setattr(recipe, "open_result", opened)
    monkeypatch.setattr(recipe, "capture_page", capture_page)

    run(
        page, query="Jane", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert destination_strips == [linkedin_dom.strip_irrelevant, None]


def test_regional_linkedin_routes_keep_their_path_on_the_verified_host():
    from career_lens.google import recipe

    assert recipe.linkedin_url_on_host(
        "https://in.linkedin.com/in/jane?trk=google",
        "www.linkedin.com",
    ) == "https://www.linkedin.com/in/jane?trk=google"
    directory = recipe.linkedin_url_on_host(
        "https://in.linkedin.com/pub/dir/Jane/Doe",
        "www.linkedin.com",
    )
    assert directory == "https://www.linkedin.com/pub/dir/Jane/Doe"
    assert classify_destination(directory) == "generic"


def test_google_linkedin_profile_clicks_before_using_shared_authenticated_capture(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    original = "https://in.linkedin.com/in/jane"
    page = setup(monkeypatch, [result(1, original)])
    events = []

    def open_result(_page, item, **_kwargs):
        events.append(("click", item.url))
        page.url = item.url
        return type("Response", (), {"status": 200})()

    def goto(url, **kwargs):
        events.append(("goto", url, kwargs))
        page.url = url
        return type("Response", (), {"status": 200})()

    def capture_arrived(kind, url_requested, _page, _out, **kwargs):
        events.append(("capture", kind, url_requested, page.url))
        return capture(page.url, "authenticated profile main page")

    page.goto = goto
    monkeypatch.setattr(recipe, "open_result", open_result)
    monkeypatch.setattr(recipe, "capture_arrived_page", capture_arrived)

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert events == [
        ("click", original),
        ("goto", "https://www.linkedin.com/in/jane/", {
            "wait_until": "domcontentloaded",
        }),
        ("capture", "linkedin_person", original,
         "https://www.linkedin.com/in/jane/"),
    ]
    assert output.results[0].capture.mode == "linkedin_shallow"
    assert output.results[0].capture.url_final == (
        "https://www.linkedin.com/in/jane/")


def test_google_linkedin_profile_discards_a_regional_login_redirect(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    original = "https://in.linkedin.com/in/jane"
    page = setup(monkeypatch, [result(1, original)])
    events = []

    def open_result(_page, _item, **_kwargs):
        page.url = (
            "https://in.linkedin.com/uas/login"
            "?session_redirect=https%3A%2F%2Fin.linkedin.com%2Fin%2Fjane"
        )
        return type("Response", (), {"status": 200})()

    def goto(url, **_kwargs):
        events.append(("goto", url))
        page.url = url
        return type("Response", (), {"status": 200})()

    def capture_arrived(kind, url_requested, _page, _out, **_kwargs):
        events.append(("capture", kind, url_requested, page.url))
        return capture(page.url, "authenticated profile main page")

    page.goto = goto
    monkeypatch.setattr(recipe, "open_result", open_result)
    monkeypatch.setattr(recipe, "capture_arrived_page", capture_arrived)

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert events == [
        ("goto", "https://www.linkedin.com/in/jane/"),
        ("capture", "linkedin_person", original,
         "https://www.linkedin.com/in/jane/"),
    ]
    assert output.results[0].capture.url_final == (
        "https://www.linkedin.com/in/jane/")


def test_google_linkedin_profile_reopens_after_an_off_domain_interstitial(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    original = "https://in.linkedin.com/in/jane"
    page = setup(monkeypatch, [result(1, original)])
    events = []

    def open_result(_page, _item, **_kwargs):
        page.url = "https://consent.example/interstitial"
        return type("Response", (), {"status": 200})()

    def goto(url, **_kwargs):
        events.append(("goto", url))
        page.url = url
        return type("Response", (), {"status": 200})()

    def capture_arrived(kind, url_requested, _page, _out, **_kwargs):
        events.append(("capture", kind, url_requested, page.url))
        return capture(page.url, "authenticated profile main page")

    page.goto = goto
    monkeypatch.setattr(recipe, "open_result", open_result)
    monkeypatch.setattr(recipe, "capture_arrived_page", capture_arrived)

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert events == [
        ("goto", "https://www.linkedin.com/in/jane/"),
        ("capture", "linkedin_person", original,
         "https://www.linkedin.com/in/jane/"),
    ]
    assert output.results[0].capture.url_final == (
        "https://www.linkedin.com/in/jane/")


def test_top_n_zero_persists_only_the_unscrolled_serp(monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://example.com/a")])
    opened = []
    monkeypatch.setattr(recipe, "open_result", lambda *a, **k: opened.append(True))
    stored = []

    output = run(
        page, query="AI", top_n=0, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: stored.append(document) or "cp-serp")

    assert [document.section for document in stored] == ["serp"]
    assert opened == []
    assert output.serp.completeness["scroll_stopped"] == "not_scrolled"
    assert output.results[0].selection_reason == "beyond_top_n"


def test_unavailable_linkedin_result_selects_the_next_eligible_result(
        monkeypatch, tmp_path):
    page = setup(monkeypatch, [
        result(1, "https://www.linkedin.com/in/jane/"),
        result(2, "https://example.com/a"),
    ])

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None, linkedin_host=lambda: None,
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].selected is False
    assert output.results[0].selection_reason == "linkedin_unavailable"
    assert output.results[1].selected is True
    assert output.results[1].capture.mode == "generic"


def test_a_social_destination_is_recorded_without_being_opened(
        monkeypatch, tmp_path):
    """The result is kept -- rank, URL, title, snippet -- and the page is not.

    These sites answer a scrape with a login wall or with somebody's personal
    page, so opening them spends a worker and returns nothing worth storing.
    """
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://www.instagram.com/jane/"),
        result(2, "https://example.com/a"),
    ])
    opened = []
    monkeypatch.setattr(
        recipe, "open_result",
        lambda _page, item, **kwargs: opened.append(item.rank)
        or type("Response", (), {"status": 200})())

    output = run(
        page, query="Jane", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].selected is False
    assert output.results[0].selection_reason == "excluded_host"
    assert output.results[0].capture is None
    assert output.results[0].search_result.title == "Result 1"
    assert opened == [2]


def test_a_skipped_social_destination_still_spends_its_slot(
        monkeypatch, tmp_path):
    """`top_n` is a depth into the ranking, not a quota of captures.

    "The top two results" means the two Google ranked first. One of them being
    a destination this run declines to open does not promote the third into
    the top two -- it means the top two held one page worth reading.
    """
    page = setup(monkeypatch, [
        result(1, "https://www.facebook.com/jane"),
        result(2, "https://example.com/a"),
        result(3, "https://example.com/b"),
    ])

    output = run(
        page, query="Jane", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert [item.selected for item in output.results] == [False, True, False]
    assert output.results[0].selection_reason == "excluded_host"
    assert output.results[2].selection_reason == "beyond_top_n"


def test_an_unavailable_linkedin_result_still_hands_its_slot_on(
        monkeypatch, tmp_path):
    """The exception to the rule above, and why it is one: no decision was
    made about this destination. The session that would have read it was not
    assigned, which is a gap in the run rather than a fact about the page."""
    page = setup(monkeypatch, [
        result(1, "https://www.linkedin.com/in/jane/"),
        result(2, "https://example.com/a"),
        result(3, "https://example.com/b"),
    ])

    output = run(
        page, query="Jane", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None, linkedin_host=lambda: None,
        persist=lambda document: f"cp-{document.section}")

    assert [item.selected for item in output.results] == [False, True, True]
    assert output.results[0].selection_reason == "linkedin_unavailable"


def test_a_social_destination_behind_an_opaque_redirect_is_still_skipped(
        monkeypatch, tmp_path):
    """What the destination is cannot be known before the click, so the skip
    happens after it -- but nothing about the page that opened is captured."""
    from career_lens.google import recipe

    opaque = SearchResult(
        1,
        "https://www.google.com/goto?url=opaque-token",
        "https://www.google.com/goto?url=opaque-token",
        "www.google.com",
        "Jane on YouTube",
        "YouTube channel",
        click_href="/goto?url=opaque-token",
        opaque_redirect=True,
    )
    page = setup(monkeypatch, [opaque, result(2, "https://example.com/next")])

    def opened(_page, item, **_kwargs):
        page.url = ("https://www.youtube.com/@jane"
                    if item.rank == 1 else item.normalised_url)
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].selected is False
    assert output.results[0].selection_reason == "excluded_host"
    assert output.results[0].capture is None
    # The worker was spent on rank 1 even though the click had to happen before
    # that could be known, so rank 2 is still beyond the top one.
    assert output.results[1].selected is False
    assert output.results[1].selection_reason == "beyond_top_n"


def test_linkedin_destination_is_one_shallow_page(monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://www.linkedin.com/in/jane/"),
    ])
    calls = []
    def opened(_page, item, **_kwargs):
        page.url = item.url
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)

    def capture_arrived(kind, url_requested, page, out_dir, **kwargs):
        calls.append((kind, url_requested, page.url))
        return capture(page.url, "complete profile first page")

    monkeypatch.setattr(recipe, "capture_arrived_page", capture_arrived)
    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].capture.mode == "linkedin_shallow"
    assert calls == [(
        "linkedin_person", "https://www.linkedin.com/in/jane/",
        "https://www.linkedin.com/in/jane/",
    )]


def test_linkedin_destination_signout_is_owned_by_linkedin(
        monkeypatch, tmp_path):
    from career_lens.capture.failures import CaptureFailed, ProviderCaptureFailed
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://www.linkedin.com/in/jane/"),
    ])

    def opened(_page, item, **_kwargs):
        page.url = item.url
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)
    monkeypatch.setattr(
        recipe, "capture_arrived_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CaptureFailed("login_required", "LinkedIn signed out")))

    with pytest.raises(ProviderCaptureFailed) as failure:
        run(
            page, query="Jane", top_n=1, out_dir=tmp_path,
            before_search=lambda _host: None,
            linkedin_host=lambda: "www.linkedin.com",
            persist=lambda document: f"cp-{document.section}")

    assert failure.value.provider == "linkedin"
    assert failure.value.failure_kind == "login_required"


def test_opaque_redirect_uses_final_url_for_result_and_capture_mode(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    opaque = SearchResult(
        1,
        "https://www.google.com/goto?url=opaque-token",
        "https://www.google.com/goto?url=opaque-token",
        "www.google.com",
        "Jane Example",
        "LinkedIn profile",
        click_href="/goto?url=opaque-token",
        opaque_redirect=True,
    )
    page = setup(monkeypatch, [opaque])

    def opened(_page, _item, **_kwargs):
        page.url = "https://www.linkedin.com/in/jane/"
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)
    monkeypatch.setattr(
        recipe, "capture_arrived_page",
        lambda kind, url, page, out, **kwargs: capture(
            page.url, "complete profile first page"))

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    observed = output.results[0]
    assert observed.search_result.normalised_url == (
        "https://www.linkedin.com/in/jane/")
    assert observed.search_result.host == "www.linkedin.com"
    assert observed.capture.mode == "linkedin_shallow"
    assert observed.capture.url_requested == "https://www.linkedin.com/in/jane/"


def test_unavailable_linkedin_behind_opaque_redirect_backfills_next_result(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    opaque = SearchResult(
        1,
        "https://www.google.com/goto?url=opaque-token",
        "https://www.google.com/goto?url=opaque-token",
        "www.google.com",
        "Jane Example",
        "LinkedIn profile",
        click_href="/goto?url=opaque-token",
        opaque_redirect=True,
    )
    page = setup(monkeypatch, [
        opaque,
        result(2, "https://example.com/next"),
    ])

    def opened(_page, item, **_kwargs):
        page.url = (
            "https://www.linkedin.com/in/jane/"
            if item.rank == 1 else item.normalised_url)
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)
    shallow_calls = []
    monkeypatch.setattr(
        recipe, "capture_arrived_page",
        lambda *args, **kwargs: shallow_calls.append(True))

    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None, linkedin_host=lambda: None,
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].selected is False
    assert output.results[0].selection_reason == "linkedin_unavailable"
    assert output.results[0].capture is None
    assert output.results[1].selected is True
    assert output.results[1].capture.mode == "generic"
    assert shallow_calls == []


def test_selected_failure_is_not_replaced_and_persistence_is_incremental(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    results = [
        result(1, "https://example.com/a"),
        result(2, "https://example.com/b"),
        result(3, "https://example.com/c"),
    ]
    page = setup(monkeypatch, results)
    order = []

    def opened(_page, item, **kwargs):
        order.append(f"open-{item.rank}")
        if item.rank == 2:
            raise TimeoutError("navigation timed out")
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", opened)

    output = run(
        page, query="AI", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: order.append(f"persist-{document.section}")
        or f"cp-{document.section}")

    assert order.index("persist-result-001") < order.index("open-2")
    assert output.results[1].capture.status == "failed"
    assert output.results[1].capture.error["kind"] == "timeout"
    assert output.results[2].selected is False


def test_http_403_evidence_is_persisted_but_reported_as_failed(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://research.example/profile")])
    order = []
    monkeypatch.setattr(
        recipe, "open_result",
        lambda *_args, **_kwargs: order.append("open")
        or type("Response", (), {"status": 200})())
    monkeypatch.setattr(
        recipe.overlays, "dismiss",
        lambda _page: order.append("dismiss") or [])

    def capture_page(_page, _out, slug, **kwargs):
        if slug == "serp":
            return capture(page.url, "complete serp text")
        order.append("capture")
        return PageCapture(
            url_requested=kwargs["url_requested"],
            final_url="https://research.example/profile",
            status=403, title="Just a moment...", ok=True,
            text="Security check required", text_chars=23,
            html_bytes=b"<main>Security check required</main>",
            scroll_stopped="bottom", scroll_growing=False,
        )

    monkeypatch.setattr(recipe, "capture_page", capture_page)
    output = run(
        page, query="Jane", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    captured = output.results[0].capture
    assert captured.capture_id == "cp-result-001"
    assert captured.http_status == 403
    assert captured.text == "Security check required"
    assert captured.status == "failed"
    assert captured.error == {
        "kind": "blocked",
        "detail": "destination returned HTTP 403",
    }
    assert order == ["open", "dismiss", "capture"]


def test_automatic_verification_finishes_before_destination_capture(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://reviews.example/product")])
    page.body = "Verifying your connection... Verified! Redirecting..."
    page.page_title = "Verifying your connection"
    page.waited = False

    def inner_text(_selector):
        return page.body

    def wait_for_function(_script, *, arg, timeout):
        assert "verifying your connection" in arg
        assert timeout == 15_000
        page.waited = True
        page.body = "Product reviews and ratings"
        page.page_title = "Product reviews"

    page.title = lambda: page.page_title

    page.inner_text = inner_text
    page.wait_for_function = wait_for_function
    monkeypatch.setattr(
        recipe, "open_result",
        lambda *_args, **_kwargs: type("Response", (), {"status": 403})())

    def capture_page(_page, _out, slug, **kwargs):
        if slug == "serp":
            return capture(page.url, "complete serp text")
        response = kwargs["response"]
        return PageCapture(
            url_requested=kwargs["url_requested"], final_url=page.url,
            status=response.status if response is not None else None,
            title="Product reviews", ok=True, text=page.body,
            text_chars=len(page.body), html_bytes=b"<main>Product reviews</main>",
            scroll_stopped="bottom", scroll_growing=False,
        )

    monkeypatch.setattr(recipe, "capture_page", capture_page)

    output = run(
        page, query="Product reviews", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    captured = output.results[0].capture
    assert page.waited is True
    assert captured.status == "succeeded"
    assert captured.http_status is None
    assert captured.text == "Product reviews and ratings"


def test_one_blocked_destination_does_not_stop_later_results(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://blocked.example/a"),
        result(2, "https://ok.example/b"),
    ])

    def capture_page(_page, _out, slug, **kwargs):
        if slug == "serp":
            return capture(page.url, "complete serp text")
        status = 403 if slug == "result-001" else 200
        return PageCapture(
            url_requested=kwargs["url_requested"],
            final_url=kwargs["url_requested"], status=status,
            title="Destination", ok=True, text="evidence", text_chars=8,
            html_bytes=b"<main>evidence</main>", scroll_stopped="bottom",
            scroll_growing=False,
        )

    monkeypatch.setattr(recipe, "capture_page", capture_page)
    output = run(
        page, query="mixed", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].capture.status == "failed"
    assert output.results[1].capture.status == "succeeded"


def test_detected_destination_challenge_keeps_evidence_and_fails_result(
        monkeypatch, tmp_path):
    from career_lens.capture.detect import Challenge
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://challenge.example/a")])
    monkeypatch.setattr(
        recipe.detect, "detect",
        lambda _page, _response: Challenge(
            kind="captcha", evidence="security check",
            url="https://challenge.example/a", status=200))

    output = run(
        page, query="mixed", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    captured = output.results[0].capture
    assert captured.capture_id == "cp-result-001"
    assert captured.status == "failed"
    assert captured.error == {
        "kind": "blocked",
        "detail": "captcha: security check",
    }


def test_serp_restore_failure_marks_remaining_results_without_retyping(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://example.com/a"),
        result(2, "https://example.com/b"),
    ])
    monkeypatch.setattr(recipe, "return_to_serp", lambda *a, **k: False)
    searches = []

    def search(*args, **kwargs):
        searches.append("search")
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "search", search)
    output = run(
        page, query="AI", top_n=2, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.results[0].capture.status == "succeeded"
    assert output.results[1].capture.error["kind"] == "browser_error"
    assert searches == ["search"]


def test_successful_serp_restore_never_charges_a_second_navigation(
        monkeypatch, tmp_path):
    from career_lens.google import recipe

    page = setup(monkeypatch, [
        result(1, "https://example.com/a"),
        result(2, "https://example.com/b"),
    ])
    monkeypatch.setattr(recipe, "return_to_serp", lambda *a, **k: True)
    charged = []

    def search(_page, _query, *, before_submit, **kwargs):
        before_submit("www.google.com")
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "search", search)
    output = run(
        page, query="AI", top_n=2, out_dir=tmp_path,
        before_search=charged.append,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert charged == ["www.google.com"]
    assert all(item.capture.status == "succeeded" for item in output.results)


def test_block_before_capturable_evidence_raises_typed_failure(
        monkeypatch, tmp_path):
    from career_lens.google import recipe
    from career_lens.google.challenge import GoogleChallenge

    page = setup(monkeypatch, [])
    monkeypatch.setattr(
        recipe, "classify_google",
        lambda *args: GoogleChallenge(
            "blocked", "unusual traffic", page.url, 429, True))
    monkeypatch.setattr(
        recipe, "capture_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("browser vanished")))
    stored = []

    with pytest.raises(CaptureFailed) as failure:
        run(
            page, query="sensitive query", top_n=1, out_dir=tmp_path,
            before_search=lambda host: None,
            linkedin_host=lambda: "www.linkedin.com",
            persist=lambda document: stored.append(document))

    assert failure.value.failure_kind == "blocked"
    assert failure.value.detail == "unusual traffic"
    assert stored == []


def test_visible_result_candidates_without_parsed_results_are_parser_drift(
        monkeypatch, tmp_path):
    page = setup(monkeypatch, [])

    output = run(
        page, query="AI", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.failure == {
        "kind": "parser_drift",
        "detail": "Google rendered result candidates that could not be parsed",
    }


def test_google_recipe_journals_named_phases_without_query_text(
        monkeypatch, tmp_path):
    from career_lens.capture import trace

    page = setup(monkeypatch, [result(1, "https://example.com/a")])
    events = []
    trace.activate(
        ["google test"],
        sink=lambda **event: events.append(event),
    )
    try:
        run(
            page, query="sensitive query body", top_n=1, out_dir=tmp_path,
            before_search=lambda host: None,
            linkedin_host=lambda: "www.linkedin.com",
            persist=lambda document: f"cp-{document.section}")
    finally:
        trace.close()

    phases = {event["phase"] for event in events}
    assert {
        "google navigation", "google serp parse", "google serp capture",
        "google result selection", "google result click",
        "google result capture",
    } <= phases
    assert all("sensitive query body" not in event["detail"]
               for event in events)


def test_a_cancel_from_persist_ends_the_run_rather_than_one_result(
        monkeypatch, tmp_path):
    """A cancel is a fact about the search, not about the page that happened
    to be open when it arrived.

    The destination loop turns a CaptureFailed into a failed result and moves
    on, which is right for a timeout and wrong for this: the run kept fetching
    the remaining destinations after an operator had stopped it.
    """
    from career_lens.capture.driver import CaptureFailed
    from career_lens.google import recipe

    results = [
        result(1, "https://example.com/a"),
        result(2, "https://example.com/b"),
        result(3, "https://example.com/c"),
    ]
    page = setup(monkeypatch, results)
    opened = []

    def open_result(_page, item, **kwargs):
        opened.append(item.rank)
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", open_result)

    def persist(document):
        if document.section == "result-001":
            raise CaptureFailed("cancelled", "an operator stopped this run")
        return f"cp-{document.section}"

    with pytest.raises(CaptureFailed) as stopped:
        run(page, query="AI", top_n=3, out_dir=tmp_path,
            before_search=lambda _host: None,
            linkedin_host=lambda: "www.linkedin.com", persist=persist)

    assert stopped.value.failure_kind == "cancelled"
    # The second destination was never opened, which is the whole point.
    assert opened == [1]


def test_an_ordinary_destination_failure_still_only_fails_that_result(
        monkeypatch, tmp_path):
    """The counterpart: widening the escape hatch must not turn every
    destination failure into an abandoned run."""
    from career_lens.capture.driver import CaptureFailed
    from career_lens.google import recipe

    results = [
        result(1, "https://example.com/a"),
        result(2, "https://example.com/b"),
    ]
    page = setup(monkeypatch, results)
    opened = []

    def open_result(_page, item, **kwargs):
        opened.append(item.rank)
        return type("Response", (), {"status": 200})()

    monkeypatch.setattr(recipe, "open_result", open_result)

    def persist(document):
        if document.section == "result-001":
            raise CaptureFailed("timeout", "the page never settled")
        return f"cp-{document.section}"

    output = run(page, query="AI", top_n=2, out_dir=tmp_path,
                 before_search=lambda _host: None,
                 linkedin_host=lambda: "www.linkedin.com", persist=persist)

    assert opened == [1, 2]
    assert output.results[0].capture.status == "failed"
    assert output.results[0].capture.error["kind"] == "timeout"


def test_the_serp_capture_strips_personal_furniture(monkeypatch, tmp_path):
    """The SERP is the one page carrying the operator's own identity.

    Google footers the IP-derived location under every result page and headers
    the signed-in account beside the query. Neither is a search result, and a
    capture is kept indefinitely, so the strip has to happen before anything is
    stored rather than being parsed out afterwards.
    """
    from career_lens.google import dom as google_dom
    from career_lens.google import recipe

    page = setup(monkeypatch, [result(1, "https://example.com/a")])
    received = {}

    def capture_page(_page, _out, slug, **kwargs):
        received[slug] = kwargs
        return capture(page.url, "complete serp text")

    monkeypatch.setattr(recipe, "capture_page", capture_page)
    run(page, query="AI", top_n=0, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: "cp-serp")

    assert received["serp"]["strip"] is google_dom.strip_personal


def test_a_profile_without_assist_never_waits_out_a_challenge(
        monkeypatch, tmp_path):
    """The wall would stand for the whole budget and the answer is the same."""
    from career_lens.capture import captcha_assist
    from career_lens.google import recipe
    from career_lens.google.challenge import GoogleChallenge

    page = setup(monkeypatch, [])
    monkeypatch.setattr(captcha_assist, "available", lambda: False)
    waited = []
    monkeypatch.setattr(
        captcha_assist, "wait_until",
        lambda *args, **kwargs: waited.append(True) or False)
    monkeypatch.setattr(
        recipe, "classify_google",
        lambda *args: GoogleChallenge(
            "blocked", "unusual traffic", page.url, 429, True))

    output = run(
        page, query="AI", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert waited == []
    assert output.failure == {"kind": "blocked", "detail": "unusual traffic"}


def test_an_answered_challenge_stores_the_results_page_and_continues(
        monkeypatch, tmp_path):
    """The solver clears the wall, so the SERP is captured as results."""
    from career_lens.capture import captcha_assist
    from career_lens.google import recipe
    from career_lens.google.challenge import GoogleChallenge

    page = setup(monkeypatch, [result(1, "https://example.test/a")])
    monkeypatch.setattr(captcha_assist, "available", lambda: True)
    monkeypatch.setattr(captcha_assist, "wait_until",
                        lambda *args, **kwargs: True)
    seen = []

    responses = []

    def classify(_page, response):
        # Blocked on arrival, readable once the solver has answered.
        responses.append(response)
        seen.append(True)
        if len(seen) == 1:
            return GoogleChallenge(
                "blocked", "unusual traffic", page.url, 429, True)
        return None

    monkeypatch.setattr(recipe, "classify_google", classify)

    output = run(
        page, query="AI", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    # Re-read after the wait, and the run is no longer a blocked failure.
    assert len(seen) == 2
    assert responses[1] is None
    assert output.failure is None
    assert output.serp is not None
    # The search carried on rather than stopping at the wall.
    assert [item.search_result.url for item in output.results] == [
        "https://example.test/a"]


def test_a_challenge_the_solver_cannot_answer_still_fails(
        monkeypatch, tmp_path):
    from career_lens.capture import captcha_assist
    from career_lens.google import recipe
    from career_lens.google.challenge import GoogleChallenge

    page = setup(monkeypatch, [])
    monkeypatch.setattr(captcha_assist, "available", lambda: True)
    monkeypatch.setattr(captcha_assist, "wait_until",
                        lambda *args, **kwargs: False)
    monkeypatch.setattr(
        recipe, "classify_google",
        lambda *args: GoogleChallenge(
            "blocked", "unusual traffic", page.url, 429, True))

    output = run(
        page, query="AI", top_n=1, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: "www.linkedin.com",
        persist=lambda document: f"cp-{document.section}")

    assert output.failure == {"kind": "blocked", "detail": "unusual traffic"}


def test_google_snippet_text_cannot_become_a_linkedin_authwall():
    from career_lens.google.challenge import classify_google

    class GoogleResults:
        url = "https://www.google.com/search?q=example"

        def locator(self, _selector):
            return type("Candidates", (), {"count": lambda self: 1})()

        def title(self):
            return "Example - Google Search"

        def inner_text(self, selector):
            assert selector == "body"
            return "Example profile Join LinkedIn to see this result"

    assert classify_google(GoogleResults(), None) is None


def test_unrecognised_empty_serp_is_parser_drift(monkeypatch, tmp_path):
    page = setup(monkeypatch, [])
    page.locator = lambda _selector: type(
        "Candidates", (), {"count": lambda self: 0})()
    page.inner_text = lambda selector: "Google Search navigation"

    output = run(
        page, query="AI", top_n=0, out_dir=tmp_path,
        before_search=lambda _host: None,
        linkedin_host=lambda: None,
        persist=lambda document: f"cp-{document.section}")

    assert output.failure == {
        "kind": "parser_drift",
        "detail": "Google returned no recognisable results state",
    }
@pytest.mark.parametrize("visible", [True, False])
def test_empty_organic_illustration_requires_visibility(visible):
    from career_lens.google.recipe import _reports_no_results

    class Page:
        def inner_text(self, selector):
            return "AI Overview with citations but no organic entries"

        def locator(self, selector):
            assert selector == '[role="img"][aria-label="An image of a yeti fishing for something in the ice"]'
            return self

        @property
        def first(self):
            return self

        def is_visible(self):
            return visible

    assert _reports_no_results(Page()) is visible
