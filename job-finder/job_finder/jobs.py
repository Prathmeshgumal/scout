"""One place for the dedupe keys of a job role, and for the checks of the text that describes it."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

LINKEDIN_HOST = re.compile(r"^([a-z0-9-]+\.)?linkedin\.com$")
LINKEDIN_JOB = re.compile(r"^/jobs/view/(?:[^/]*-)?(\d+)/?$")
JOB_BOARDS = ("greenhouse.io", "lever.co", "ashbyhq.com", "workable.com", "smartrecruiters.com", "myworkdayjobs.com")
LEGAL_FORMS = {"inc", "ltd", "llc", "llp", "pvt", "private", "limited", "corp", "corporation", "gmbh"}
MIN_DESCRIPTION = 200  # characters: a shorter text is not a full job description
WALL_LIMIT = 1500  # characters: a sign-in wall is a short page
# Text that only a sign-in or a sign-up page has.
WALL_MARKS = ("sign in to view", "sign in to linkedin", "join linkedin", "agree & join", "already on linkedin",
              "forgot password", "please sign in", "must log in", "must sign in", "log in to continue",
              "sign in to continue", "create an account or log in")


def _parts(url: str):
    text = (url or "").strip()
    return urlsplit(text if "://" in text else f"https://{text}")


def _host(url: str) -> str:
    return (_parts(url).hostname or "").lower()


def is_linkedin_url(url: str) -> bool:
    return bool(LINKEDIN_HOST.match(_host(url)))


def is_linkedin_job_url(url: str) -> bool:
    """A job page or a job search page of LinkedIn, on any regional host. These show only a sign-in wall."""
    return is_linkedin_url(url) and _parts(url).path.startswith("/jobs")


def linkedin_job_id(url: str) -> str | None:
    if not is_linkedin_url(url):
        return None
    match = LINKEDIN_JOB.match(_parts(url).path)
    return match.group(1) if match else None


def job_key(url: str) -> str:
    """The dedupe key of a role link: li:<number> for a LinkedIn job, else url:<url without query or fragment>."""
    number = linkedin_job_id(url)
    if number:
        return f"li:{number}"
    parts = _parts(url)
    return f"url:{(parts.hostname or '').lower().removeprefix('www.')}{parts.path.rstrip('/')}"


def job_source(url: str) -> str:
    """Where a link found by search goes. A LinkedIn company list gives the source itself."""
    host = _host(url)
    if is_linkedin_url(url):
        return "google"
    return "job_board" if any(host == b or host.endswith(f".{b}") for b in JOB_BOARDS) else "company_site"


def _words(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def company_key(name: str | None) -> str:
    """The name of a company without its legal form. "Acme Pvt. Ltd." and "ACME" have one key."""
    words = _words(name).split()
    while len(words) > 1 and words[-1] in LEGAL_FORMS:
        words.pop()
    return " ".join(words)


def _place(location: str | None) -> str:
    """The first part of a location. A remote location keeps its region: "Remote, India" is not "Remote, USA"."""
    text = (location or "").strip()
    return _words(text) if text.lower().startswith("remote") else _words(text.split(",")[0])


def role_key(company: str | None, title: str | None, location: str | None) -> str:
    """The same role on two sites has one key. So LinkedIn and a job board show it once."""
    return "|".join((company_key(company), _words(title), _place(location)))


def looks_like_sign_in(text: str | None) -> bool:
    """True for a page that asks for a login and has no job text."""
    body = " ".join((text or "").lower().split())
    return len(body) < WALL_LIMIT and any(mark in body for mark in WALL_MARKS)


def usable_description(text: str | None) -> str | None:
    """Why a text cannot be a full job description, or None when it can."""
    if looks_like_sign_in(text):
        return "This text is a sign-in wall, not a job description."
    if len((text or "").strip()) < MIN_DESCRIPTION:
        return "This text is too short for a full job description."
    return None


def is_closed(status: str | None) -> bool:
    """If a posting is closed. career-lens gives posted or reposted for a live posting. Other sources can give open."""
    return (status or "").strip().lower() not in ("", "posted", "reposted", "open")
