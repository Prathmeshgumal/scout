import socket

import pytest

from job_finder.site_policy import blocked_reason, blocked_url_reason


def no_dns(host: str) -> list[str]:
    return []


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/acme/jobs/1",
    "https://www.acme.com/careers",
    "http://jobs.lever.co/acme",
    "https://www.essex.ac.uk/jobs",  # a word inside a word is not a match
    "https://www.levels.fyi/companies/acme/salaries",
])
def test_job_sites_are_allowed(url):
    assert blocked_url_reason(url, resolve=no_dns) is None


@pytest.mark.parametrize("url", [
    "http://localhost:8000/",
    "http://127.0.0.1:8090/console",
    "http://192.168.1.1/",
    "http://10.0.0.5/admin",
    "http://172.16.3.4/",
    "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/",
    "http://0.0.0.0/",
    "http://router.local/",
    "http://minio.internal:9000/",
])
def test_local_and_private_addresses_are_blocked(url):
    assert "local or private" in blocked_url_reason(url, resolve=no_dns)


def test_a_name_that_resolves_to_a_private_address_is_blocked():
    reason = blocked_url_reason("https://sneaky.example.com/", resolve=lambda host: ["93.184.216.34", "127.0.0.1"])
    assert "local or private" in reason


def test_a_name_that_does_not_resolve_is_left_to_career_lens():
    def fail(host):
        raise socket.gaierror("no such host")
    assert blocked_url_reason("https://new-startup.example/", resolve=fail) is None


@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://settings", "data:text/html,hi", "ftp://x.com/"])
def test_only_web_pages_are_allowed(url):
    assert "web page" in blocked_url_reason(url, resolve=no_dns)


@pytest.mark.parametrize("url", [
    "https://www.pornhub.com/",
    "https://videos.xvideos.com/x",
    "https://free-porn-site.net/",
    "https://something.xxx/",
    "https://lucky.casino/",
    "https://thepiratebay.org/search",
    "https://abcdefghijklmnop.onion/",
    "https://www.bet365.com/",
])
def test_blocked_kinds_of_site(url):
    reason = blocked_url_reason(url, resolve=no_dns)
    assert reason is not None and "not about jobs" in reason


def test_a_subdomain_of_a_blocked_domain_is_blocked():
    assert blocked_reason("m.pornhub.com") is not None
    assert blocked_reason("notpornhub.com") is None  # only the domain and its subdomains
