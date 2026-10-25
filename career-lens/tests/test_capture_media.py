"""Fetching a page's own images through the page.

Everything refused here would otherwise be stored as a photograph: an error
page, a tracking pixel served as HTML, a truncated body. A capture that keeps
the wrong bytes is worse than one that records why it kept none.
"""

from __future__ import annotations

import pytest

from career_lens.capture.media import (
    MAX_BYTES, Media, MediaFetchFailed, extension_for, fetch,
)

URL = "https://media.licdn.com/dms/image/v2/abc/profile-displayphoto-shrink_400_400/0/1?e=1&t=x"


class FakeResponse:
    def __init__(self, *, status=200, headers=None, body=b"\x89PNG\r\n"):
        self.status = status
        self.ok = 200 <= status < 300
        self.headers = headers if headers is not None else {"content-type": "image/png"}
        self._body = body

    def body(self):
        return self._body


class FakePage:
    """Only the surface `media.fetch` uses: `page.request.get`."""

    def __init__(self, response):
        self.asked: list[str] = []
        outer = self

        class Request:
            def get(self, url, timeout=None):
                outer.asked.append(url)
                if isinstance(response, Exception):
                    raise response
                return response

        self.request = Request()


def test_an_image_comes_back_with_its_bytes_and_type():
    page = FakePage(FakeResponse(body=b"\x89PNG\r\n\x1a\n"))

    found = fetch(page, URL)

    assert isinstance(found, Media)
    assert found.data == b"\x89PNG\r\n\x1a\n"
    assert found.content_type == "image/png"
    assert found.url == URL
    assert page.asked == [URL]


def test_the_content_type_is_read_without_its_parameters():
    page = FakePage(FakeResponse(headers={"content-type": "image/jpeg; charset=binary"}))

    assert fetch(page, URL).content_type == "image/jpeg"


def test_an_expired_url_is_refused_rather_than_stored():
    """A signed URL past its expiry answers 403, and the body is an error."""
    with pytest.raises(MediaFetchFailed, match="403"):
        fetch(FakePage(FakeResponse(status=403, body=b"<html>denied</html>")), URL)


def test_a_login_page_served_instead_of_an_image_is_refused():
    page = FakePage(FakeResponse(headers={"content-type": "text/html"},
                                 body=b"<html>sign in</html>"))

    with pytest.raises(MediaFetchFailed, match="text/html"):
        fetch(page, URL)


def test_a_response_with_no_content_type_is_refused():
    with pytest.raises(MediaFetchFailed, match="no content type"):
        fetch(FakePage(FakeResponse(headers={})), URL)


def test_an_empty_body_is_refused():
    with pytest.raises(MediaFetchFailed, match="empty body"):
        fetch(FakePage(FakeResponse(body=b"")), URL)


def test_something_far_too_large_to_be_an_avatar_is_refused():
    page = FakePage(FakeResponse(body=b"x" * (MAX_BYTES + 1)))

    with pytest.raises(MediaFetchFailed, match="ceiling"):
        fetch(page, URL)


def test_a_body_at_the_ceiling_is_still_accepted():
    assert len(fetch(FakePage(FakeResponse(body=b"x" * MAX_BYTES)), URL).data) == MAX_BYTES


@pytest.mark.parametrize("content_type,suffix", [
    ("image/jpeg", "jpg"),
    ("image/png", "png"),
    ("image/webp", "webp"),
    ("image/gif", "gif"),
])
def test_the_stored_name_comes_from_the_type_served(content_type, suffix):
    # A signed media URL has no extension in it, so the URL cannot name the file.
    assert extension_for(content_type) == suffix


def test_an_unknown_image_type_still_gets_a_name():
    assert extension_for("image/heif") == "bin"
