"""Dedicated Posts-tab parsing for LinkedIn company captures."""

from __future__ import annotations


from career_lens.linkedin.parse.company import parse_company_html

COMPANY_URL = "https://www.linkedin.com/company/acme/"
POSTS_URL = f"{COMPANY_URL}posts/"

HOME_HTML = """
<html><body><main><section aria-label="Primary content">
  <section class="org-top-card">
    <h1 class="org-top-card-summary__title">Acme</h1>
  </section>
</section></main></body></html>
"""

POSTS_HTML = """
<html><body><main><section aria-label="Primary content">
  <p>Loaded 2 Posts posts</p>
  <div class="feed-shared-update-v2" data-urn="urn:li:activity:7492325888571658240">
    <div class="feed-shared-update-v2__control-menu-container"></div>
    <div class="update-components-actor__container">
      <a class="update-components-actor__meta-link" href="/company/acme/">
        <span class="update-components-actor__title">
          <span aria-hidden="true">Acme</span>
        </span>
        <span class="update-components-actor__description">
          <span aria-hidden="true">2M followers</span>
        </span>
        <span class="update-components-actor__sub-description">
          <span aria-hidden="true">1d</span>
        </span>
      </a>
    </div>
    <div class="update-components-text"><span>A complete company post.</span></div>
    <div class="update-components-image">
      <img src="https://media.example/post.png" alt="Launch"/>
    </div>
  </div>
</section></main></body></html>
"""

EMPTY_POSTS_HTML = """
<html><body><main><section aria-label="Primary content">
  <div class="feed-container-theme">
    <div class="scaffold-finite-scroll">
      <div class="scaffold-finite-scroll__empty">
        <section class="artdeco-empty-state">
          <h2>There is no company activity</h2>
        </section>
      </div>
    </div>
  </div>
</section></main></body></html>
"""

def test_company_posts_use_the_shared_activity_shape() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"posts": POSTS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"posts": POSTS_URL},
    )

    posts = result["company"]["posts"]
    assert posts["source_url"] == POSTS_URL
    assert posts["reached_end"] is None
    assert "loaded" not in posts
    assert "captured" not in posts
    assert "complete" not in posts
    assert posts["items"][0]["post"]["author"]["name"] == "Acme"
    assert posts["items"][0]["post"]["commentary"]["text"] == (
        "A complete company post."
    )
    assert posts["items"][0]["post"]["media"][0]["url"] == (
        "https://media.example/post.png"
    )
    assert result["section_status"]["posts"] == "parsed"
    assert result["sources"]["posts"] == {"artifact": None, "url": POSTS_URL}

def test_company_posts_recognize_a_structural_empty_feed() -> None:
    result = parse_company_html(
        HOME_HTML,
        {"posts": EMPTY_POSTS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"posts": POSTS_URL},
    )

    assert result["company"]["posts"] == {
        "source_url": POSTS_URL,
        "reached_end": True,
        "items": [],
    }

def test_company_posts_translate_capture_stop_reason() -> None:
    reached_end = parse_company_html(
        HOME_HTML,
        {"posts": POSTS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"posts": POSTS_URL},
        tab_capture={"posts": {"scroll_stopped": "bottom"}},
    )
    limited = parse_company_html(
        HOME_HTML,
        {"posts": POSTS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"posts": POSTS_URL},
        tab_capture={"posts": {"scroll_stopped": "distance"}},
    )
    stalled = parse_company_html(
        HOME_HTML,
        {"posts": POSTS_HTML},
        company_url=COMPANY_URL,
        tab_urls={"posts": POSTS_URL},
        tab_capture={"posts": {"scroll_stopped": "stalled"}},
    )

    assert reached_end["company"]["posts"]["reached_end"] is True
    assert limited["company"]["posts"]["reached_end"] is False
    assert stalled["company"]["posts"]["reached_end"] is None
