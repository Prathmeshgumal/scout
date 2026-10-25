from bs4 import BeautifulSoup


def test_loading_selector_distinguishes_blank_bars_from_content():
    from career_lens.linkedin.readiness import PROFILE_LOADING_SELECTOR

    document = '''<main>
      <section id="loading"><div><div></div><div></div></div></section>
      <section id="loaded"><h2>Experience</h2><div><div></div><div></div></div></section>
      <section id="spacer"><div></div></section>
      <section id="picture"><img src="fixture.png"><div></div><div></div></section>
    </main>'''
    soup = BeautifulSoup(document, "html.parser")
    assert [node['id'] for node in soup.select(PROFILE_LOADING_SELECTOR)] == ['loading']


def test_native_readiness_scrolls_and_waits_for_delayed_sections(monkeypatch):
    from career_lens.linkedin import sections

    class Page:
        pending = True
        url = "https://www.linkedin.com/in/fixture/"

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def count(self):
            return int(self.pending)

        def wait_for_function(self, script, *, arg, timeout):
            assert scrolled
            self.pending = False

    scrolled = []
    monkeypatch.setattr(sections.interaction, "scroll_to_target",
                        lambda *args, **kwargs: scrolled.append(True))
    assert sections.wait_for_profile_loading(Page()) is None


def test_native_readiness_is_bounded_when_sections_never_load(monkeypatch):
    from career_lens.linkedin import sections

    class Page:
        url = "https://www.linkedin.com/in/fixture/"
        waits = 0

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def wait_for_function(self, script, *, arg, timeout):
            self.waits += 1
            raise sections.BrowserTimeoutError("fixture remains loading")

    page = Page()
    monkeypatch.setattr(sections.interaction, "scroll_to_target", lambda *a, **k: None)
    assert "loading" in sections.wait_for_profile_loading(page)
    assert page.waits == 2


def test_native_discovery_waits_before_capturing_an_inline_selected_section(monkeypatch, tmp_path):
    from career_lens.linkedin import sections

    class Page:
        url = "https://www.linkedin.com/in/fixture/"
        pending = True

        def locator(self, selector):
            return self

        @property
        def first(self):
            return self

        def count(self):
            return int(self.pending)

        def wait_for_function(self, script, *, arg, timeout):
            self.pending = False

    page = Page()
    monkeypatch.setattr(sections, "raise_if_gone", lambda *a, **k: None)
    monkeypatch.setattr(sections, "recover_transient_error", lambda *a: None)
    monkeypatch.setattr(sections.interaction, "scroll_to_target", lambda *a, **k: None)

    def links(p):
        assert not p.pending
        return []

    monkeypatch.setattr(sections, "find_links", links)
    evidence = sections.SectionCapture("experience", sections.PageCapture(
        url_requested=page.url, ok=True, html_bytes=b"<section>fixture</section>"))
    monkeypatch.setattr(sections, "capture_inline_profile_section", lambda *a: evidence)
    captures = list(sections.expand(page, sections.ExpandConfig(
        scratch_dir=tmp_path, requested_sections=frozenset({"experience"}),
    ), "fixture"))
    assert [capture.section for capture in captures] == ["experience"]
    assert captures[0].capture.html_bytes == b"<section>fixture</section>"


def test_native_loading_failure_reaches_progress_journal(monkeypatch, tmp_path):
    from career_lens.capture import driver, trace
    from career_lens.linkedin import sections

    reason = "Profile sections remained loading after bounded readiness checks"
    monkeypatch.setattr(sections, "expand", lambda *args: iter([
        sections.SectionCapture("experience", sections.PageCapture(
            url_requested="https://www.linkedin.com/in/fixture/", error=reason), reason),
    ]))
    events = []
    trace.activate([], sink=lambda **event: events.append(event))
    try:
        assert list(driver._sections(None, tmp_path)) == []
    finally:
        trace.close()
    assert any("experience" in event["detail"] and reason in event["detail"]
               for event in events)


def test_pending_section_does_not_invalidate_captured_inline_section(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from career_lens.linkedin import sections

    page = SimpleNamespace(url="https://www.linkedin.com/in/fixture/")
    monkeypatch.setattr(sections, "raise_if_gone", lambda *a, **k: None)
    monkeypatch.setattr(sections, "recover_transient_error", lambda *a: None)
    monkeypatch.setattr(sections, "wait_for_profile_loading", lambda *a: "Still loading")
    monkeypatch.setattr(sections, "find_links", lambda *a: [])
    monkeypatch.setattr(sections, "capture_inline_profile_section", lambda *a: None)
    monkeypatch.setattr(sections, "capture_featured", lambda *a: sections.SectionCapture(
        "featured", sections.PageCapture(url_requested=page.url, ok=True,
                                         html_bytes=b"<section>fixture</section>")))
    captures = list(sections.expand(page, sections.ExpandConfig(
        scratch_dir=tmp_path, requested_sections=frozenset({"featured", "experience"}),
    ), "fixture"))
    assert [(item.section, item.capture.ok) for item in captures] == [
        ("featured", True), ("experience", False),
    ]
