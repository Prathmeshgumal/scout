"""Personal LinkedIn furniture against realistic markup in a real browser."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

POST_URL = "https://www.linkedin.com/posts/example"
POST_PAGE = """<!doctype html>
<html><body><main id="workspace">
  <div id="composer-shell" style="min-height: 100px; background: white">
    <div>
      <div>
        <div id="shareboxProfilePictureComponentRef">Viewer avatar</div>
        <div id="draft-text-replaceable-component">Start a post</div>
      </div>
      <div>
        <button><svg id="video-medium"></svg>Video</button>
        <button><svg id="image-medium"></svg>Photo</button>
        <a href="/article/new/">Write article</a>
      </div>
    </div>
  </div>
  <section>Recommended for you</section>
  <aside aria-label="Sidebar">
    <a href="https://www.linkedin.com/in/operator/">Operator Name</a>
    <a href="https://www.linkedin.com/mynetwork/invite-connect/connections/">
      Connections
    </a>
  </aside>
  <article><h1>Subject post</h1><p>Research content</p></article>
  <aside aria-label="Aside"><a href="https://about.linkedin.com/">About</a></aside>
</main></body></html>"""


@pytest.fixture
def page(tmp_path):
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "profile"), channel="chrome",
            headless=True)
        try:
            page = context.new_page()
            page.route(
                "https://www.linkedin.com/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html; charset=utf-8",
                    body=POST_PAGE))
            yield page
        finally:
            context.close()


def test_strip_removes_the_viewer_sidebar_and_keeps_the_post_and_footer(page):
    from career_lens.linkedin.dom import strip_irrelevant

    page.goto(POST_URL)
    assert "Operator Name" in page.inner_text("body")

    strip_irrelevant(page)

    body = page.inner_text("body")
    assert "Operator Name" not in body
    assert "Subject post" in body
    assert "About" in body


def test_strip_removes_the_viewer_composer_and_keeps_the_feed(page):
    from career_lens.linkedin.dom import strip_irrelevant

    page.goto(POST_URL)
    assert "Start a post" in page.inner_text("body")
    assert page.locator("#composer-shell").is_visible()

    strip_irrelevant(page)

    body = page.inner_text("body")
    assert page.locator("#composer-shell").is_visible() is False
    assert "Viewer avatar" not in body
    assert "Start a post" not in body
    assert "Video" not in body
    assert "Photo" not in body
    assert "Write article" not in body
    assert "Recommended for you" in body
    assert "Subject post" in body
