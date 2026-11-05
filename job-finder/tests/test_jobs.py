"""The dedupe keys and the checks of job text. One function for each key: the store and the tools use them."""

import pytest

from job_finder.jobs import (
    is_closed,
    is_linkedin_job_url,
    job_key,
    job_source,
    linkedin_job_id,
    looks_like_sign_in,
    role_key,
    usable_description,
)


@pytest.mark.parametrize("url, key", [
    ("https://www.linkedin.com/jobs/view/senior-backend-engineer-at-acme-4487285593", "li:4487285593"),
    ("https://www.linkedin.com/jobs/view/4487285593/", "li:4487285593"),
    ("https://in.linkedin.com/jobs/view/senior-backend-engineer-at-acme-4487285593", "li:4487285593"),
    ("https://www.linkedin.com/jobs/view/4487285593/?trackingId=abc&refId=1#top", "li:4487285593"),
    ("https://LINKEDIN.com/jobs/view/4487285593", "li:4487285593"),
])
def test_a_linkedin_job_url_gives_the_trailing_number(url, key):
    assert job_key(url) == key
    assert linkedin_job_id(url) == key.removeprefix("li:")


@pytest.mark.parametrize("url, key", [
    ("https://boards.greenhouse.io/acme/jobs/123?gh_src=x#apply", "url:boards.greenhouse.io/acme/jobs/123"),
    ("http://www.Acme.com/careers/backend/", "url:acme.com/careers/backend"),
    ("https://jobs.lever.co/acme/abc-def", "url:jobs.lever.co/acme/abc-def"),
])
def test_another_url_gives_the_normalised_url_without_query_or_fragment(url, key):
    assert job_key(url) == key
    assert linkedin_job_id(url) is None


def test_a_linkedin_url_without_a_number_is_not_a_linkedin_job_key():
    assert job_key("https://www.linkedin.com/jobs/view/no-number-here") == "url:linkedin.com/jobs/view/no-number-here"


@pytest.mark.parametrize("url", ["https://www.linkedin.com/jobs/view/1", "https://in.linkedin.com/jobs/view/x-1?a=b",
                                 "https://linkedin.com/jobs/search?keywords=x", "http://uk.linkedin.com/jobs/collections"])
def test_linkedin_job_pages_are_known(url):
    assert is_linkedin_job_url(url)


@pytest.mark.parametrize("url", ["https://www.linkedin.com/company/acme/", "https://acme.com/jobs/view/1",
                                 "https://notlinkedin.com/jobs/view/1"])
def test_other_pages_are_not_linkedin_job_pages(url):
    assert not is_linkedin_job_url(url)


@pytest.mark.parametrize("url, source", [
    ("https://www.linkedin.com/jobs/view/1", "google"),
    ("https://boards.greenhouse.io/acme/jobs/1", "job_board"),
    ("https://job-boards.greenhouse.io/acme/jobs/1", "job_board"),
    ("https://jobs.lever.co/acme/1", "job_board"),
    ("https://jobs.ashbyhq.com/acme/1", "job_board"),
    ("https://acme.com/careers/1", "company_site"),
])
def test_the_source_comes_from_the_url(url, source):
    assert job_source(url) == source


def test_the_same_role_on_two_sites_has_one_role_key():
    assert (role_key("Acme Corp.", "Senior Backend Engineer", "Bengaluru")
            == role_key("ACME corp", " senior  backend-engineer ", "Bengaluru, Karnataka, India"))


def test_the_legal_form_of_a_company_does_not_change_the_role_key():
    assert role_key("Acme Pvt Ltd", "SRE", "Pune") == role_key("Acme", "SRE", "Pune")


@pytest.mark.parametrize("one, other", [
    (("Acme", "Engineer", "Pune"), ("Acme", "Staff Engineer", "Pune")),
    (("Acme", "Engineer", "Pune"), ("Beta", "Engineer", "Pune")),
    (("Acme", "Engineer", "Pune"), ("Acme", "Engineer", "Mumbai")),
    (("Acme", "Engineer", "Remote, India"), ("Acme", "Engineer", "Remote, USA")),
    (("Acme", "Engineer", "Pune"), ("Acme", "Engineer", None)),
])
def test_different_roles_have_different_role_keys(one, other):
    assert role_key(*one) != role_key(*other)


@pytest.mark.parametrize("text", [
    "Sign in to view more jobs\nEmail or phone\nPassword\nForgot password?",
    "Join LinkedIn\nAgree & Join\nAlready on LinkedIn? Sign in",
    "Sign in to LinkedIn. Welcome back. Email or phone. Password.",
    "You must log in to continue. Create an account or log in.",
    "Please sign in to continue\nUsername\nPassword",
])
def test_a_sign_in_wall_is_found(text):
    assert looks_like_sign_in(text)


def test_a_job_text_that_names_sign_in_is_not_a_wall():
    text = ("About the role. You will build the sign in service for our product, with SSO and passwords. " * 6)
    assert not looks_like_sign_in(text)


def test_a_full_description_must_be_long_and_not_a_wall():
    assert usable_description("We build things. " * 20) is None
    assert "sign-in" in usable_description("Sign in to view more jobs. Email or phone. Password. Forgot password?")
    assert "short" in usable_description("Backend role.")


@pytest.mark.parametrize("status", [None, "", "posted", "open", "reposted", "Reposted"])
def test_a_live_posting_is_not_closed(status):
    assert not is_closed(status)


def test_a_closed_posting_is_closed():
    assert is_closed("closed")
