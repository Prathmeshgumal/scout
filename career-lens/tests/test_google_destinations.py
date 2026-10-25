"""Which destinations a search records rather than opens."""

from __future__ import annotations

import pytest

from career_lens.google.destinations import is_excluded


@pytest.mark.parametrize("url", [
    "https://www.facebook.com/jane",
    "https://m.facebook.com/jane",
    "https://fb.com/jane",
    "https://fb.watch/abc123/",
    "https://www.instagram.com/jane/",
    "https://www.youtube.com/watch?v=abc",
    "https://www.youtube.com/@janechannel",
    "https://youtu.be/abc",
    "https://www.tiktok.com/@jane",
    "https://www.snapchat.com/add/jane",
    "https://in.pinterest.com/jane/",
    "https://www.pinterest.co.uk/jane/",
    "https://www.quora.com/Who-is-Jane",
    "https://es.quora.com/Quien-es-Jane",
])
def test_social_and_ugc_destinations_are_skipped(url):
    """The sites a scrape reaches and learns nothing from."""
    assert is_excluded(url) is True


@pytest.mark.parametrize("url", [
    "https://example.com/jane",
    "https://www.linkedin.com/in/jane/",
    "https://en.wikipedia.org/wiki/Jane",
    "https://nvidia.com/careers",
])
def test_ordinary_destinations_are_opened(url):
    assert is_excluded(url) is False


@pytest.mark.parametrize("url", [
    "https://instagram.example.com/jane",
    "https://youtube.com.phishing.example/jane",
    "https://notquora.com/Who-is-Jane",
    "https://myfacebookfanpage.net/jane",
])
def test_a_site_that_merely_names_one_is_not_one(url):
    """The brand has to be the registrable domain, not a label anywhere in the
    host: `youtube.com.phishing.example` is the phisher's site, not YouTube's,
    and skipping it would leave a real result silently unread."""
    assert is_excluded(url) is False


def test_a_url_without_a_host_is_not_excluded():
    assert is_excluded("not a url") is False
