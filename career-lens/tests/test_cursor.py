from career_lens.capture import browser, cursor, interaction


class Page:
    def __init__(self):
        self.handlers = {}
        self.scripts = []

    def on(self, event, handler):
        self.handlers[event] = handler

    def evaluate(self, script, argument):
        self.scripts.append((script, argument))


class Context:
    def __init__(self, pages):
        self.pages = pages
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler


def test_cursor_overlay_follows_navigation_and_new_tabs():
    first = Page()
    context = Context([first])
    cursor.enable_cursor(context)

    assert first.scripts[-1][1] == {
        "cursorId": cursor.CURSOR_ID,
        "artifactOnly": cursor.ARTIFACT_ONLY,
        "position": None,
    }
    interaction._POSITIONS[first] = (321.0, 147.0)
    first.handlers["domcontentloaded"]()
    assert len(first.scripts) == 2
    assert first.scripts[-1][1] == {
        "cursorId": cursor.CURSOR_ID,
        "artifactOnly": cursor.ARTIFACT_ONLY,
        "position": {"x": 321.0, "y": 147.0},
    }

    second = Page()
    context.handlers["page"](second)
    script, settings = second.scripts[-1]
    assert settings == {"cursorId": cursor.CURSOR_ID,
                        "artifactOnly": cursor.ARTIFACT_ONLY,
                        "position": None}
    assert "mousemove" in script and "pointer-events:none" in script


def test_browser_session_only_enables_cursor_when_requested(monkeypatch):
    contexts = []

    class Driver:
        def __init__(self, profile, *, headless, window_size):
            self.page = Page()
            self.context = Context([self.page])
            self.page.context = self.context
            contexts.append(self.context)

        def start(self):
            return self.page

        def close(self):
            pass

    enabled = []
    monkeypatch.setitem(browser.DRIVERS, "fake", Driver)
    monkeypatch.setattr(browser, "enable_cursor", enabled.append, raising=False)
    with browser.session("fake"):
        pass
    with browser.session("fake", show_cursor=True):
        pass
    assert enabled == [contexts[-1]]


def test_the_overlay_marks_itself_as_ours_so_it_is_never_stored():
    """Turning the overlay on must not change a single byte we store.

    `page._stored_html` drops every node carrying ARTIFACT_ONLY. Without that
    mark, a run with the pointer on would put a `career-lens-debug-cursor` div
    into the HTML of every page it keeps, recorded as though the site had
    served it -- so the capture would differ depending on whether someone was
    watching.
    """
    from career_lens.capture.page import ARTIFACT_ONLY, _stored_html

    assert "artifactOnly" in cursor.CURSOR_SCRIPT
    assert cursor.ARTIFACT_ONLY == ARTIFACT_ONLY

    served = (
        f'<html><body><p>kept</p>'
        f'<div id="{cursor.CURSOR_ID}" {ARTIFACT_ONLY}="1">pointer</div>'
        f'</body></html>'
    )

    stored = _stored_html(served).decode("utf-8")

    assert "kept" in stored
    assert cursor.CURSOR_ID not in stored
