"""Deciding which images to fetch, and surviving a fetch that fails.

The URLs are signed and expire within days, so this runs while the page is
still open. It must never raise: a profile read successfully is not thrown away
because a photograph could not be taken.
"""

from __future__ import annotations

from career_lens.linkedin.dom import collect_profile_images

PROFILE = "tests/fixtures/profiles/afreen-munshi/profile.html"
URL = "https://www.linkedin.com/in/afreen-munshi/"


def profile_html() -> bytes:
    with open(PROFILE, "rb") as handle:
        return handle.read()


class FakeResponse:
    def __init__(self, status=200, content_type="image/jpeg", body=b"jpeg-bytes"):
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = {"content-type": content_type}
        self._body = body

    def body(self):
        return self._body


class FakePage:
    def __init__(self, response=None, *, explode=False):
        self.asked: list[str] = []
        outer = self

        class Request:
            def get(self, url, timeout=None):
                outer.asked.append(url)
                if explode:
                    raise RuntimeError("connection reset")
                return response or FakeResponse()

        self.request = Request()


def test_a_person_page_yields_the_owner_photo_at_its_largest():
    page = FakePage()

    found, error = collect_profile_images(
        page, profile_html(), "linkedin_person", URL)

    assert error is None
    assert set(found) == {"profile_image"}
    assert found["profile_image"].data == b"jpeg-bytes"
    # Upgraded from the 200x200 the top card links to.
    assert "shrink_800_800" in page.asked[0]


def test_a_profile_with_no_cover_fetches_only_the_photo():
    page = FakePage()

    found, error = collect_profile_images(
        page, profile_html(), "linkedin_person", URL)

    assert "cover_image" not in found
    assert error is None
    assert len(page.asked) == 1


def test_a_page_that_is_not_a_linkedin_profile_fetches_nothing():
    page = FakePage()

    found, error = collect_profile_images(
        page, b"<html><img src='x'></html>", "generic", "https://example.com/")

    assert (found, error) == ({}, None)
    assert page.asked == []


def test_a_capture_with_no_html_fetches_nothing():
    assert collect_profile_images(FakePage(), None, "linkedin_person", URL) == ({}, None)


def test_a_failed_fetch_is_a_recorded_reason_rather_than_an_exception():
    """The page read fine. Only the photograph is missing."""
    found, error = collect_profile_images(
        FakePage(explode=True), profile_html(), "linkedin_person", URL)

    assert found == {}
    assert error is not None
    assert "profile_image" in error and "connection reset" in error


def test_an_expired_url_is_reported_and_stores_nothing():
    found, error = collect_profile_images(
        FakePage(FakeResponse(status=403)), profile_html(), "linkedin_person",
        URL)

    assert found == {}
    assert "403" in error


def test_html_that_cannot_be_parsed_is_reported_rather_than_raised():
    found, error = collect_profile_images(
        FakePage(), b"\x00\x01not html at all", "linkedin_person", URL)

    assert found == {}
    # Either nothing was found in it or it would not parse; both are reasons,
    # and neither is a crash.
    assert error is None or isinstance(error, str)


# ---- a company, whose page keeps its class names ---------------------------

COMPANY_URL = "https://www.linkedin.com/company/acme/"
LOGO = ("https://media.licdn.com/dms/image/v2/LOGO/company-logo_200_200/0/1"
        "?e=1&v=beta&t=a")
LOGO_BIG = ("https://media.licdn.com/dms/image/v2/LOGO/company-logo_400_400/0/1"
            "?e=1&v=beta&t=b")
COVER = ("https://media.licdn.com/dms/image/v2/COVER/company-background_10000_2000"
         "/0/1?e=1&v=beta&t=c")

COMPANY_HTML = f"""
<html><body><main><section class="org-top-card">
  <h1 class="org-top-card-summary__title">Acme</h1>
  <img class="org-top-card-primary-content__logo" src="{LOGO}">
  <div class="org-cropped-image__cover-image"
       style="background-image: url({COVER})"></div>
  <img src="{LOGO_BIG}">
</section></main></body></html>
"""


def test_a_company_logo_and_cover_are_both_fetched():
    page = FakePage()

    found, error = collect_profile_images(
        page, COMPANY_HTML.encode(), "linkedin_company", COMPANY_URL)

    assert error is None
    assert set(found) == {"profile_image", "cover_image"}
    # The logo is taken at the largest size the page carries, not the 200x200
    # the top card happens to render.
    assert LOGO_BIG in page.asked
    assert COVER in page.asked


def test_a_company_with_no_images_on_its_page_fetches_nothing():
    page = FakePage()

    found, error = collect_profile_images(
        page, b'<html><body><main><section class="org-top-card">'
              b"<h1>Acme</h1></section></main></body></html>",
        "linkedin_company", COMPANY_URL)

    assert (found, error) == ({}, None)
    assert page.asked == []
