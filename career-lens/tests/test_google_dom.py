"""The SERP strip against real Google markup in a real browser."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.browser

FIXTURE = Path(__file__).parent / "fixtures" / "google" / "serp-personal.html"
SERP_URL = "https://www.google.com/search?q=ai+research+labs"


@pytest.fixture
def page(tmp_path):
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "profile"), channel="chrome",
            headless=True)
        try:
            page = context.new_page()
            # Served under Google's own hostname rather than the fixture
            # server's: the strip is host-guarded, so a test on 127.0.0.1
            # would pass while proving nothing.
            page.route(
                "https://www.google.com/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8",
                    body=FIXTURE.read_text(encoding="utf-8")))
            yield page
        finally:
            context.close()


def test_the_serp_strip_removes_the_ip_derived_location(page):
    from career_lens.google.dom import strip_personal

    page.goto(SERP_URL)
    assert "From your IP address" in page.inner_text("body")

    strip_personal(page)

    body = page.inner_text("body")
    assert "From your IP address" not in body
    assert "500032, Hyderabad, Telangana" not in body
    assert "Update location" not in body


def test_the_serp_strip_removes_the_signed_in_account(page):
    from career_lens.google.dom import strip_personal

    page.goto(SERP_URL)
    assert "Priya Nair" in page.inner_text("body")

    strip_personal(page)

    assert "Priya Nair" not in page.inner_text("body")


def test_the_serp_strip_keeps_the_query_and_the_results(page):
    """The account bar sits beside the search box, and the footer under the
    results. Taking either with them would cost the capture its subject."""
    from career_lens.google.dom import strip_personal

    page.goto(SERP_URL)
    strip_personal(page)

    assert not page.locator("#searchform").is_visible()
    assert page.input_value('input[name="q"]') == "ai research labs"
    body = page.inner_text("body")
    assert "Example Labs" in body
    assert "Jane Doe" in body


def test_the_serp_strip_counts_each_element_once(page):
    """It runs before every screenshot and again before the read, so a second
    pass over an already-stripped page must be a no-op."""
    from career_lens.google.dom import strip_personal

    page.goto(SERP_URL)

    assert strip_personal(page) > 0
    assert strip_personal(page) == 0


def test_the_serp_strip_does_nothing_away_from_google(page):
    """It is wired to the SERP capture today; a page that is not Google's must
    survive it untouched should that ever widen."""
    from career_lens.google.dom import strip_personal

    page.set_content(
        '<header id="gb">nav</header><div id="footcnt">footer</div>')

    assert strip_personal(page) == 0
    assert "footer" in page.inner_text("body")
