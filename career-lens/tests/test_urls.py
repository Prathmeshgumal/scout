import pytest

from career_lens.capture.urls import UrlError, canonical, classify


@pytest.mark.parametrize("raw,expected", [
    ("https://WWW.LinkedIn.com/in/Jane/",
     "https://www.linkedin.com/in/Jane/"),
    ("https://www.linkedin.com/in/jane",
     "https://www.linkedin.com/in/jane/"),
    ("https://www.linkedin.com/in/jane/#experience",
     "https://www.linkedin.com/in/jane/"),
    ("https://www.linkedin.com/in/jane/?trk=public_profile",
     "https://www.linkedin.com/in/jane/"),
    ("https://www.linkedin.com/in/jane/?trk=x&keep=1",
     "https://www.linkedin.com/in/jane/?keep=1"),
    ("https://in.linkedin.com/in/jane/",
     "https://www.linkedin.com/in/jane/"),
    ("https://uk.linkedin.com/company/acme/",
     "https://www.linkedin.com/company/acme/"),
    ("https://example.com:443/a", "https://example.com/a"),
])
def test_canonicalisation(raw, expected):
    assert canonical(raw) == expected


def test_a_non_linkedin_path_keeps_its_exact_trailing_slash():
    # On an arbitrary site /a and /a/ can be two different documents, so the
    # slash rule is LinkedIn-only.
    assert canonical("https://example.com/a") == "https://example.com/a"
    assert canonical("https://example.com/a/") == "https://example.com/a/"


@pytest.mark.parametrize("raw", ["", "   ", "ftp://h/f", "not a url", "https://"])
def test_unusable_urls_are_refused(raw):
    with pytest.raises(UrlError):
        canonical(raw)


def test_the_refusal_says_which_scheme_was_given():
    with pytest.raises(UrlError) as raised:
        canonical("ftp://host/file")
    assert "ftp" in str(raised.value)


@pytest.mark.parametrize("url,kind", [
    ("https://www.linkedin.com/in/jane/", "linkedin_person"),
    ("https://linkedin.com/in/jane/", "linkedin_person"),
    ("https://www.linkedin.com/company/stripe/", "linkedin_company"),
    ("https://www.linkedin.com/school/mit/", "linkedin_company"),
    ("https://www.linkedin.com/showcase/aws/", "linkedin_company"),
    ("https://www.linkedin.com/feed/", "generic"),
    ("https://www.linkedin.com/jobs/view/123/", "generic"),
    ("https://example.com/anything", "generic"),
])
def test_classification(url, kind):
    assert classify(canonical(url)) == kind


def test_a_details_url_is_not_itself_a_profile_request():
    # A caller pasting a section URL is asking for that page, not for the
    # profile it belongs to -- and section pages are captured as part of a
    # profile scrape, never requested on their own.
    assert classify(canonical(
        "https://www.linkedin.com/in/jane/details/experience/")) == "generic"


