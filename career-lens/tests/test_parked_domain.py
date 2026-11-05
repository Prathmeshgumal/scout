"""Recognised domain-sale redirects, not sales words in ordinary content."""

from types import SimpleNamespace

import pytest

from career_lens.capture import driver
from career_lens.capture.failures import CaptureFailed


@pytest.mark.parametrize("timeout", [False, True])
def test_domain_sale_redirect_retains_navigation_evidence_and_stops_retrying(
        monkeypatch, tmp_path, timeout):
    from career_lens.fleet.retry import Terminal, decide

    requested = "https://example.com/"
    landed = "https://forsale.godaddy.com/forsale/example.com?tracking=ignored"

    class Page:
        url = "about:blank"

        def goto(self, url, *, wait_until):
            assert url == requested
            self.url = landed
            if timeout:
                raise RuntimeError("Page.goto: Timeout 30000ms exceeded")
            return SimpleNamespace(status=200, url=landed)

    monkeypatch.setattr(driver.discovery, "discover", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver.verification, "wait", lambda page, response, **kw: response)
    def unexpected_capture(*args, **kwargs):
        pytest.fail("a confirmed sale redirect must not scrape the sales page")

    monkeypatch.setattr(driver, "capture_arrived_page", unexpected_capture)
    captures = driver.capture_request(
        "generic", requested, browser_profile="test", out_dir=tmp_path, page=Page())
    evidence = next(captures)
    assert evidence.section == "page"
    assert evidence.url_requested == requested
    assert evidence.url_final == "https://forsale.godaddy.com/forsale/example.com"
    assert evidence.http_status == (None if timeout else 200)
    assert evidence.html is None and evidence.text is None
    assert evidence.screenshots == []
    assert evidence.metadata["navigation"]["outcome"] == "domain_for_sale"
    with pytest.raises(CaptureFailed) as raised:
        next(captures)
    assert raised.value.failure_kind == "not_found"
    assert "parked / for sale" in raised.value.detail
    assert isinstance(decide(
        cause=raised.value.failure_kind, attempt_no=1, max_attempts=3), Terminal)


@pytest.mark.parametrize("landed", [
    "https://new-brand.example/",
    "https://forsale.godaddy.com/forsale/another.example",
    "https://forsale.godaddy.com.evil.example/forsale/example.com",
    "https://forsale.godaddy.com/articles/example.com-is-for-sale",
    "https://forsale.godaddy.com/forsale/example.com/another-page",
    "https://example.com/article/this-domain-is-for-sale",
])
def test_other_redirects_keep_the_original_navigation_failure(monkeypatch, tmp_path, landed):
    class Page:
        url = landed

        def goto(self, url, *, wait_until):
            raise RuntimeError("Page.goto: Timeout 30000ms exceeded")

    monkeypatch.setattr(driver.discovery, "discover", lambda *args, **kwargs: None)
    with pytest.raises(CaptureFailed) as raised:
        list(driver.capture_request(
            "generic", "https://example.com/", browser_profile="test",
            out_dir=tmp_path, page=Page()))
    assert raised.value.failure_kind == "timeout"


def test_a_normal_redirect_is_still_captured(monkeypatch, tmp_path):
    from career_lens.capture.page import PageCapture

    class Page:
        url = "https://new-brand.example/"

        def goto(self, url, *, wait_until):
            return SimpleNamespace(status=200, url=self.url)

    monkeypatch.setattr(driver.discovery, "discover", lambda *args, **kwargs: None)
    monkeypatch.setattr(driver.verification, "wait", lambda page, response, **kw: response)
    monkeypatch.setattr(driver, "capture_arrived_page", lambda *args, **kwargs: PageCapture(
        url_requested="https://example.com/", final_url=Page.url, status=200,
        ok=True, text="An article about domains for sale"))
    documents = list(driver.capture_request(
        "generic", "https://example.com/", browser_profile="test",
        out_dir=tmp_path, page=Page()))
    assert len(documents) == 1
    assert documents[0].url_final == Page.url
    assert documents[0].text == "An article about domains for sale"
