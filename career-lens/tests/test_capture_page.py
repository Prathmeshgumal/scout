"""Capture, against a local fixture server.

Never against LinkedIn: a test that needs a signed-in session and a live site
is not a test. What is being checked here is the mechanics -- that a tall page
becomes several bands, that HTML and text come back, that a failed navigation
is reported rather than raised.
"""

from __future__ import annotations

import pytest

# `site` lives in tests/conftest.py, shared with test_driver.py.


@pytest.fixture
def page(tmp_path):
    """A real browser, on a throwaway profile.

    Marked `browser` so the suite can be run without one: capture needs a
    downloaded Chrome, which not every checkout has yet.
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


pytestmark = pytest.mark.browser


def test_a_short_page_yields_one_screenshot(page, site, tmp_path):
    from career_lens.capture.page import capture_page

    page.goto(f"{site}/short.html")
    result = capture_page(page, tmp_path, "short",
                          url_requested=f"{site}/short.html")
    assert result.ok
    assert result.title == "Short page"
    assert len(result.screenshot_parts) == 1
    assert b"<h1>Short</h1>" in result.html_bytes


def test_a_tall_unscrolled_page_is_captured_in_bands(page, site, tmp_path):
    # Banding is the NOT-scrolled path. With scroll=True the page is
    # photographed a screen at a time and `_join_screens` merges those into one
    # image on purpose -- "one page is one picture ... the reader scrolls the
    # image rather than a list of them". So a banding test must turn scrolling
    # off, or it measures the joiner and always sees exactly one part however
    # tall the page is.
    from career_lens.capture.page import capture_page

    page.goto(f"{site}/tall.html")
    result = capture_page(page, tmp_path, "tall",
                          url_requested=f"{site}/tall.html", scroll=False)
    assert result.ok
    assert len(result.screenshot_parts) > 1
    assert len(result.screenshot_bytes) == len(result.screenshot_parts)


def test_a_scrolled_page_is_joined_into_one_image(page, site, tmp_path):
    # The other half of the pair, and the one production actually takes: a
    # profile capture scrolls. Without this, nothing would notice if the joiner
    # started returning its input unjoined -- and the test above would still
    # pass, because it runs the other branch.
    from career_lens.capture.page import capture_page

    page.goto(f"{site}/tall.html")
    result = capture_page(page, tmp_path, "tall",
                          url_requested=f"{site}/tall.html", scroll=True)
    assert result.ok
    assert len(result.screenshot_parts) == 1


def test_scrolling_reaches_the_bottom_of_a_tall_page(page, site, tmp_path):
    from career_lens.capture.page import capture_page

    page.goto(f"{site}/tall.html")
    result = capture_page(page, tmp_path, "tall",
                          url_requested=f"{site}/tall.html", scroll=True)
    assert "bottom" in (result.text or "")
    assert result.scrolled_px > 0


def test_an_unreadable_page_is_reported_not_raised(page, site, tmp_path):
    from career_lens.capture.page import capture_page

    # capture_page's contract, in its own words: "a failed page is data, not a
    # crash". `ok` is set only after the text and the HTML have been read, and
    # anything that throws on the way there is recorded as `error` instead.
    #
    # So the READ has to fail, which is why the page is closed first. A failed
    # navigation does not test this: `goto` raises, the page stays on
    # about:blank, and capturing about:blank succeeds -- ok would be True and
    # the test would be asserting nothing. Nor does an unreachable port: Chrome
    # renders its own interstitial, which is a perfectly readable page.
    page.goto(f"{site}/short.html")
    page.close()

    result = capture_page(page, tmp_path, "gone",
                          url_requested=f"{site}/short.html")
    assert not result.ok
    assert result.error


@pytest.mark.parametrize("viewport", [(640, 480), (1440, 900)])
def test_inner_page_scroller_and_target_work_at_different_viewports(
        page, viewport):
    from career_lens.capture import interaction
    from career_lens.capture.page import CLEAR_SCROLLER, FIND_SCROLLER

    width, height = viewport
    page.set_viewport_size({"width": width, "height": height})
    page.set_content("""
      <style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        header { height: 64px; }
        #workspace { height: calc(100vh - 64px); overflow-y: auto; }
        #menu { height: 80px; width: 180px; overflow-y: auto; }
        #content { height: 1800px; position: relative; }
        #target { position: absolute; top: 1650px; left: 24px; }
      </style>
      <header>Header</header>
      <main id="workspace">
        <div id="menu"><div style="height:300px">Menu</div></div>
        <div id="content"><button id="target">Show all</button></div>
      </main>
    """)

    scroller = page.evaluate(FIND_SCROLLER)
    assert scroller is not None
    assert page.locator("#workspace").get_attribute("data-wf-scroller") == "1"

    box = interaction.scroll_to_target(page, page.locator("#target"))
    assert box is not None
    assert 0 <= box["y"] < height
    assert page.locator("#workspace").evaluate("el => el.scrollTop") > 0
    page.evaluate(CLEAR_SCROLLER)


def test_linkedin_workspace_remains_the_scroll_owner_when_overflow_is_hidden(page):
    from career_lens.capture import interaction
    from career_lens.capture.page import FIND_SCROLLER

    page.set_viewport_size({"width": 900, "height": 600})
    page.set_content("""
      <style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        #workspace { height: 500px; overflow-y: hidden; }
        #content { height: 1400px; position: relative; }
        #target { position: absolute; top: 1200px; left: 40px; }
      </style>
      <main id="workspace">
        <div id="content"><a id="target" href="#activity">Show all</a></div>
      </main>
    """)

    scroller = page.evaluate(FIND_SCROLLER)
    context = page.locator("#target").evaluate(interaction.SCROLL_CONTEXT)

    assert scroller is not None
    assert page.locator("#workspace").get_attribute("data-wf-scroller") == "1"
    assert {key: context[key] for key in ("left", "top", "right", "bottom")} == {
        "left": 0, "top": 0, "right": 900, "bottom": 500,
    }
    assert context["owner"]["selector"] == "main#workspace"
    assert 0 <= context["wheelPoint"]["x"] <= 900
    assert 0 <= context["wheelPoint"]["y"] <= 500


def test_obfuscated_skeletons_are_seen_without_a_name_to_match_on(page, site):
    """`[class*="skeleton"]`, `[aria-busy]` and `[role=progressbar]` match
    nothing on the LinkedIn build in production -- the captured profile HTML
    has zero of all three -- so the probe has to recognise a placeholder by
    what it is: animated, sized, and holding no text.
    """
    from career_lens.capture.page import PENDING_VISUALS

    page.goto(f"{site}/skeletons.html?ms=60000")
    assert page.evaluate(PENDING_VISUALS) == 4


def test_a_pulsing_element_with_text_is_not_a_placeholder(page, site):
    """The "Open to work" badge animates forever. Counting it would hold every
    capture open to its full budget and teach us nothing."""
    from career_lens.capture.page import PENDING_VISUALS

    page.goto(f"{site}/skeletons.html?ms=1")
    page.wait_for_selector("#done", state="visible")
    assert page.evaluate(PENDING_VISUALS) == 0


def test_visual_scope_excludes_a_complementary_placeholder(page):
    """A sidebar loader cannot hold the primary LinkedIn page gate open."""
    from career_lens.capture.page import PENDING_VISUALS, _pending_visuals_expression

    page.set_content("""
      <main><section aria-label="Primary content"><p>Loaded company</p></section></main>
      <aside><div id="sidebar-loader"></div></aside>
      <style>
        #sidebar-loader { width: 290px; height: 50px; animation: pulse 1s infinite; }
        @keyframes pulse { from { opacity: .5; } to { opacity: 1; } }
      </style>
    """)

    scope = [
        'main section[aria-label="Primary content"]',
        "main section",
        "main",
    ]
    assert page.evaluate(PENDING_VISUALS) == 1
    assert page.evaluate(_pending_visuals_expression(scope)) == 0


def test_waiting_for_the_page_reports_that_it_settled(page, site):
    from career_lens.capture import trace
    from career_lens.capture.page import wait_for_visuals

    page.goto(f"{site}/skeletons.html?ms=300")
    events: list[dict] = []
    trace.activate(["test"], sink=lambda **event: events.append(event))
    try:
        assert wait_for_visuals(page) == 0
    finally:
        trace.close()

    span = next(e for e in events if e["phase"] == "await page content")
    assert "no placeholders" in span["detail"]
    assert page.locator("#done").is_visible()


def test_a_page_that_never_settles_is_captured_anyway_and_says_so(page, site):
    """A bounded wait that returns what is there. A profile held open forever
    by one stuck placeholder would cost the run, and the whole point of the
    journal is that an under-settled shot admits it."""
    from career_lens.capture import trace
    from career_lens.capture.page import wait_for_visuals

    page.goto(f"{site}/skeletons.html?ms=60000")
    events: list[dict] = []
    trace.activate(["test"], sink=lambda **event: events.append(event))
    try:
        assert wait_for_visuals(page, timeout_ms=500) == 4
    finally:
        trace.close()

    span = next(e for e in events if e["phase"] == "await page content")
    assert "4 still drawing" in span["detail"]


def test_a_page_still_growing_is_not_photographed_yet(page, site):
    """LinkedIn's footer is genuinely on screen while the profile is short.
    Shooting then and stitching the result puts a footer in the middle of the
    page, with the section that finally mounted painted over it."""
    from career_lens.capture.page import _settle_height

    page.goto(f"{site}/growing.html?grow=250&px=2000")
    grew = _settle_height(page)

    assert grew >= 2000, grew
    # And it really waited: the section is in the document now.
    assert page.locator("#late").is_visible()


def test_a_settled_page_costs_almost_nothing_to_check(page, site):
    """The wait is paid only while something is happening. A static page
    leaves after the quiet polls, not after the budget."""
    import time

    from career_lens.capture.page import _settle_height

    page.goto(f"{site}/growing.html?grow=1&px=100")
    page.wait_for_selector("#late", state="visible")

    started = time.monotonic()
    assert _settle_height(page) == 0
    assert time.monotonic() - started < 1.0


def test_a_page_that_never_stops_growing_is_still_captured(page, site):
    """Bounded, like every other wait here: a feed that keeps producing must
    not hold a capture open forever."""
    import time

    from career_lens.capture.page import HEIGHT_SETTLE_BUDGET_S, _settle_height

    page.goto(f"{site}/endless.html")
    started = time.monotonic()
    _settle_height(page)
    elapsed = time.monotonic() - started

    assert elapsed < HEIGHT_SETTLE_BUDGET_S + 1.0, elapsed


def test_the_shot_says_when_the_page_grew_under_it(page, site, tmp_path):
    from career_lens.capture import trace
    from career_lens.capture.page import capture_page

    # Late enough that the capture is already under way when it lands, which
    # is the case that used to stitch a footer into the middle of the page.
    page.goto(f"{site}/growing.html?grow=1200&px=2000")
    events: list[dict] = []
    trace.activate(["test"], sink=lambda **event: events.append(event))
    try:
        capture_page(page, tmp_path, "growing",
                     url_requested=f"{site}/growing.html")
    finally:
        trace.close()

    shots = [e for e in events if e["phase"] == "screenshot"]
    assert any("grew" in shot["detail"] for shot in shots), [
        shot["detail"] for shot in shots]


def test_the_footer_is_photographed_once_and_only_at_the_end(page, site,
                                                             tmp_path):
    """The reported symptom, in pixels.

    While the page is short its footer is on screen, so a screen taken then
    holds it; the section then mounts, the footer moves down, and the next
    screen holds real content. Stitched, the footer appears twice -- once in
    the middle of the page, painted over by the section that arrived.

    The fixture's footer is the only dark band on the page, so counting runs
    of dark rows in the joined image counts footers.
    """
    import io

    from PIL import Image

    from career_lens.capture.page import capture_page

    page.goto(f"{site}/growing.html?grow=1200&px=2500")
    result = capture_page(page, tmp_path, "growing",
                          url_requested=f"{site}/growing.html")
    assert result.ok

    # `screenshot_bytes` rather than the path: the joiner deletes the parts it
    # merged, and the bytes are what actually reaches the store.
    image = Image.open(io.BytesIO(result.screenshot_bytes[0])).convert("RGB")
    dark = [
        all(image.getpixel((x, y))[0] < 90
            for x in range(0, image.width, max(1, image.width // 12)))
        for y in range(image.height)
    ]
    runs = sum(1 for index, row in enumerate(dark)
               if row and not (index and dark[index - 1]))

    assert runs == 1, f"{runs} footer bands in a {image.size} image"
    # And it is at the end, where a footer belongs.
    assert dark[-1], "the page does not end on its footer"

def _image_height(png: bytes) -> int:
    from PIL import Image
    from io import BytesIO

    with Image.open(BytesIO(png)) as image:
        return image.height


def test_a_scroll_lock_we_cannot_press_still_yields_the_whole_page(
        page, site, tmp_path):
    """The screens taken under a lock are of a page that never moved. Keeping
    them stores one screen of an article and calls the capture a success, which
    is how a stunted screenshot reaches an operator looking like a real one."""
    from career_lens.capture import overlays
    from career_lens.capture.page import STALLED, capture_page

    url = f"{site}/consent-lock.html?deaf"
    page.goto(url)
    result = capture_page(page, tmp_path, "locked", url_requested=url,
                          scroll=True, dismiss=overlays.dismiss)

    assert result.ok
    # The lock did happen, so this is the case under test rather than a page
    # that happened to scroll.
    assert result.scroll_stopped == STALLED
    assert len(result.screenshot_parts) == 1
    assert _image_height(result.screenshot_bytes[0]) > 3000


def test_a_consent_banner_that_took_the_scroll_is_pressed_and_the_page_read(
        page, site, tmp_path):
    """The recovery the fallback above is a backstop for. Pressing the banner
    gives the scroll back, so the page is read properly rather than
    photographed whole from a standstill."""
    from career_lens.capture import overlays
    from career_lens.capture.page import BOTTOM, capture_page

    url = f"{site}/consent-lock.html"
    page.goto(url)
    result = capture_page(page, tmp_path, "consent", url_requested=url,
                          scroll=True, dismiss=overlays.dismiss)

    assert result.ok
    assert result.dismissed_late == ["cookie_banner"]
    assert result.scroll_stopped == BOTTOM
    assert "bottom" in (result.text or "")


def _scroller_id(page) -> str | None:
    """Which element the scroll hunt marked, by id. None means the window."""
    from career_lens.capture.page import CLEAR_SCROLLER, FIND_SCROLLER

    found = page.evaluate(FIND_SCROLLER)
    marked = page.evaluate(
        "() => (document.querySelector('[data-wf-scroller]') || {}).id ?? null")
    page.evaluate(CLEAR_SCROLLER)
    return marked if found else None


def test_a_page_the_window_can_scroll_is_scrolled_by_the_window(page):
    """The defect this replaces: candidates were ranked by visible area, so a
    documentation rail could out-rank the page it sits in. Ranking cannot
    decide this -- a rail nested in a scrolling page is never the page."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        body { margin: 0; }
        #toc { position: fixed; left: 0; top: 0; width: 300px; height: 700px;
               overflow-y: auto; }
        #article { margin-left: 320px; height: 6000px; }
      </style>
      <nav id="toc"><div style="height:4000px">contents</div></nav>
      <article id="article">the page</article>
    """)

    assert _scroller_id(page) is None


def test_a_shell_that_owns_the_scroll_is_found_when_the_window_cannot_move(page):
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        #shell { height: 100%; overflow-y: auto; display: flex; }
        #rail { width: 300px; overflow-y: auto; }
        #main { flex: 1; height: 9000px; }
      </style>
      <div id="shell">
        <div id="rail"><div style="height:6000px">rail</div></div>
        <div id="main">main</div>
      </div>
    """)

    assert _scroller_id(page) == "shell"


def test_a_clipped_box_is_not_mistaken_for_the_page_shell(page):
    """`overflow: hidden` with content past the edge describes most layout
    wrappers on the web. Only one that fills the viewport is the page."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        #wrap { width: 400px; height: 200px; overflow: hidden; }
        #pane { height: 100%; overflow-y: auto; }
      </style>
      <div id="wrap"><div style="height:900px">a clipped box</div></div>
      <div id="pane"><div style="height:5000px">the real content</div></div>
    """)

    assert _scroller_id(page) == "pane"


def test_a_page_with_no_main_landmark_still_finds_its_own_scroll(page):
    """The hole the old containment guard left. `main, [role=main]` returning
    null switched the guard off entirely, and every overflowing element on the
    page became eligible -- which is how a table of contents was elected."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        body { margin: 0; }
        #menu { width: 200px; height: 300px; overflow-y: auto; float: left; }
        #body { height: 7000px; }
      </style>
      <div id="menu"><div style="height:2000px">menu</div></div>
      <div id="body">no main landmark anywhere on this page</div>
    """)

    assert _scroller_id(page) is None


def test_a_root_that_under_reports_its_own_height_is_still_the_scroller(page):
    """`documentElement.scrollHeight` alone is not the page's extent: a root
    that clips reports one viewport while the body below it is thousands of
    pixels tall. kore.ai reports exactly this until its scroll library
    finishes initialising, and a hunt that believes the root descends into the
    hero section and wheels that instead of the page."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        html, body { margin: 0; height: 100%; overflow: hidden; }
        #hero { height: 100%; overflow-y: auto; }
        #rest { height: 6000px; }
      </style>
      <section id="hero"><div style="height:2400px">hero</div></section>
      <div id="rest">the rest of the page</div>
    """)
    # The shape under test, not an accident of this browser version.
    assert page.evaluate("() => document.documentElement.scrollHeight") == 800
    assert page.evaluate("() => document.body.scrollHeight") > 800

    assert _scroller_id(page) is None


def test_a_long_rail_does_not_displace_a_shorter_article(page):
    """Travel alone cannot decide it either. A documentation sidebar listing
    every page on the site is routinely longer than the page beside it --
    Tailwind's is 6685px against the document's 2970 -- and it is still a rail.
    A surface that owns the page is the width of the page."""
    page.set_viewport_size({"width": 1280, "height": 800})
    page.set_content("""
      <style>
        body { margin: 0; }
        #toc { position: fixed; left: 0; top: 0; width: 288px; height: 780px;
               overflow-y: auto; }
        #article { margin-left: 300px; height: 3000px; }
      </style>
      <nav id="toc"><div style="height:7000px">every page on the site</div></nav>
      <article id="article">a shorter article</article>
    """)
    # The shape under test: the rail really does have further to travel.
    assert page.evaluate(
        "() => document.querySelector('#toc').scrollHeight - 780") > 2200

    assert _scroller_id(page) is None


def test_the_har_recorder_sees_a_real_page_s_subresources(page, site):
    """End to end against real Chrome: attach, load, drain, read the HAR."""
    import gzip
    import json

    from career_lens.capture.har import HarRecorder

    recorder = HarRecorder()
    recorder.attach(page.context)
    try:
        page.goto(f"{site}/fetches.html")
        page.wait_for_selector("#state:has-text('done')")
        blob = recorder.take("page", url=f"{site}/fetches.html")
    finally:
        recorder.close()

    entries = json.loads(gzip.decompress(blob))["log"]["entries"]
    urls = [entry["request"]["url"] for entry in entries]
    assert f"{site}/fetches.html" in urls
    assert f"{site}/short.html" in urls

    # The document is recorded but its body is not: it is already the html
    # artifact. The fetch is first-party and small, so its body is kept.
    document = next(e for e in entries if e["request"]["url"].endswith("fetches.html"))
    fetched = next(e for e in entries if e["request"]["url"].endswith("short.html"))
    assert "text" not in document["response"]["content"]
    assert "text" in fetched["response"]["content"]
    assert document["response"]["status"] == 200

    # Headers Chromium does expose are recorded under their real names.
    sent = {h["name"] for h in fetched["request"]["headers"]}
    assert "x-career-lens-probe" in sent
