"""A link in stored Markdown has to name the site it came from.

A captured page's own markup says `/person/albinder-dhindsa`, which the browser
resolves against the site it is on. Stored Markdown has no such context: the
operator console renders it at `127.0.0.1:8787`, so a root-relative link there
points at the console, and clicking it navigates to a page that does not exist.

Measured on real captures: 4,487 of 5,692 LinkedIn
captures carried at least one such link.
"""

from __future__ import annotations

from career_lens.capture.markdown import absolutise_links

CB = "https://www.example.com/organization/blinkit/profiles_and_contacts"


def _fix(html: str, base: str = CB) -> str:
    return absolutise_links(html.encode("utf-8"), base).decode("utf-8")


def test_a_root_relative_link_is_resolved_against_the_page_it_came_from():
    html = '<a href="/person/albinder-dhindsa">Albinder Dhindsa</a>'

    assert 'href="https://www.example.com/person/albinder-dhindsa"' in _fix(html)


def test_the_host_comes_from_the_page_not_from_a_hardcoded_site():
    html = '<a href="/in/jane/">Jane</a>'

    fixed = _fix(html, "https://www.linkedin.com/in/jane/details/experience/")

    assert 'href="https://www.linkedin.com/in/jane/"' in fixed


def test_a_query_string_survives_being_absolutised():
    html = '<a href="/preload/custom-invite/?vanityName=alexjespinoza">Invite</a>'

    fixed = _fix(html, "https://www.linkedin.com/in/alexjespinoza/")

    assert ("href=\"https://www.linkedin.com/preload/custom-invite/"
            "?vanityName=alexjespinoza\"") in fixed


def test_an_absolute_link_is_left_exactly_as_it_was():
    html = '<a href="https://builtwith.com/">BuiltWith</a>'

    assert _fix(html) == html


def test_a_protocol_relative_link_is_left_alone():
    """`//host/path` already names its host; only its scheme is inherited."""
    html = '<a href="//cdn.example/x.png">CDN</a>'

    assert _fix(html) == html


def test_an_anchor_is_left_alone():
    """It names a place on this page, and stays that way."""
    html = '<a href="#financials">Financials</a>'

    assert _fix(html) == html


def test_a_non_navigational_scheme_is_left_alone():
    for href in ("mailto:someone@example.com", "tel:+15551234",
                 "javascript:void(0)", "data:text/plain,hello"):
        html = f'<a href="{href}">x</a>'
        assert _fix(html) == html


def test_single_quoted_and_unquoted_hrefs_are_handled():
    assert "https://www.example.com/person/x" in _fix("<a href='/person/x'>x</a>")
    assert "https://www.example.com/person/x" in _fix("<a href=/person/x>x</a>")


def test_other_attributes_are_not_rewritten():
    """Only `href`. `src` points at media the capture does not store."""
    html = '<img src="/image/logo.png">'

    assert _fix(html) == html


def test_nothing_changes_without_a_usable_base():
    html = '<a href="/person/x">x</a>'

    assert absolutise_links(html.encode(), None).decode() == html
    assert absolutise_links(html.encode(), "not a url").decode() == html


def test_the_parser_version_records_that_links_are_now_absolute():
    """So captures stored before this can be told apart from ones after."""
    from career_lens.capture import markdown

    assert markdown.PARSER == "docling-4"
