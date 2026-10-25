"""Overlay dismissal without a live browser."""

from career_lens.capture import overlays


class FakeLocator:
    def __init__(self, matches: int, log: list, name: str, text: str = ""):
        self.matches = matches
        self.log = log
        self.name = name
        self.text = text

    @property
    def first(self):
        return self

    def count(self):
        self.log.append(("count", self.name))
        return self.matches

    def wait_for(self, **_kwargs):
        self.log.append(("wait_for", self.name))

    def evaluate(self, _script):
        return self.text

    def click(self, **_kwargs):
        self.log.append(("click", self.name))


class FakeFrame:
    """A frame holding known CSS matches and known button labels.

    `buttons` are the visible texts of the frame's buttons, matched against a
    rule's label patterns the way `get_by_role` matches an accessible name.
    """

    def __init__(self, name: str, css: dict | None = None,
                 buttons: tuple[str, ...] = ()):
        self.name = name
        self.css = css or {}
        self.buttons = buttons
        self.log = []

    def locator(self, selector):
        return FakeLocator(
            1 if selector in self.css else 0, self.log,
            f"{self.name}:{selector}")

    def get_by_role(self, _role, name):
        matched = [text for text in self.buttons if name.match(text)]
        return FakeLocator(
            len(matched), self.log, f"{self.name}:{name.pattern}",
            text=matched[0] if matched else "")


class FakePage:
    def __init__(self, frame: FakeFrame,
                 url: str = "https://www.linkedin.com/in/example/"):
        self.url = url
        self.frames = [frame]
        self.main_frame = frame


def test_linkedin_survey_requires_the_qualtrics_survey_container(monkeypatch):
    selector = (".QSIPopOver:has(iframe[title*='survey' i]) "
                "button[aria-label='Close Modal']")
    monkeypatch.setattr(overlays.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        overlays.interaction, "click_in_place",
        lambda _page, target, **_kwargs: target.click())
    survey = FakeFrame("main", css={selector: {}})

    assert overlays.dismiss(FakePage(survey), grace_s=0) == ["linkedin_survey"]
    assert ("click", f"main:{selector}") in survey.log

    generic = FakeFrame(
        "main", css={".QSIPopOver button[aria-label='Close Modal']": {}})
    assert overlays.dismiss(FakePage(generic), grace_s=0) == []


def test_an_iubenda_banner_is_dismissed_by_its_own_button(monkeypatch):
    """iubenda sets `overflow: hidden` on the document while its notice is up,
    so a banner we cannot press is a page we cannot scroll."""
    monkeypatch.setattr(overlays.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        overlays.interaction, "click_in_place",
        lambda _page, target, **_kwargs: target.click())
    frame = FakeFrame("main", css={".iubenda-cs-accept-btn": {}})

    dismissed = overlays.dismiss(
        FakePage(frame, url="https://go9x.com/blog/x"), grace_s=0)

    assert dismissed == ["cookie_banner"]
    assert ("click", "main:.iubenda-cs-accept-btn") in frame.log


def test_a_button_labelled_only_accept_is_a_cookie_banner(monkeypatch):
    """The vendor selectors cover the platforms we know by name. A banner from
    one we do not is reached by what its button says, and the shortest thing a
    consent button says is "Accept" on its own."""
    monkeypatch.setattr(overlays.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        overlays.interaction, "click_in_place",
        lambda _page, target, **_kwargs: target.click())
    frame = FakeFrame("main", buttons=("Accept",))

    dismissed = overlays.dismiss(
        FakePage(frame, url="https://example.com/article"), grace_s=0)

    assert dismissed == ["cookie_banner"]


def test_accept_matches_the_whole_label_and_not_its_first_word(monkeypatch):
    """`^accept$` is the broadest rule we have, and the anchor is what keeps it
    that way. Relaxed to a word boundary it would press "Accept invitation" on
    a signed-in profile, which acts on a real account."""
    monkeypatch.setattr(overlays.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        overlays.interaction, "click_in_place",
        lambda _page, target, **_kwargs: target.click())
    frame = FakeFrame("main", buttons=("Accept invitation",))

    assert overlays.dismiss(
        FakePage(frame, url="https://example.com/article"), grace_s=0) == []
