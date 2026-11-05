"""Turn large career-lens responses into short text for Claude. A missing value is left out, never invented."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .career_lens import jobs_state
from .jobs import is_closed

LIMIT = 6000
JOBS_LIMIT = 16000  # a company list holds many roles, and each one needs its link
MAX_JOBS = 80


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _value(value: Any) -> str | None:
    """A readable value: dicts use their display/name/value, lists are joined."""
    if value in (None, "", [], {}):
        return None
    if isinstance(value, dict):
        for key in ("display", "name", "value"):
            if value.get(key):
                return str(value[key])
        return None
    if isinstance(value, list):
        names = [_value(item) for item in value]
        return ", ".join(name for name in names if name) or None
    return str(value)


def _fields(source: dict, pairs: tuple[tuple[str, str], ...]) -> list[str]:
    lines = []
    for label, key in pairs:
        value = _value(source.get(key))
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _done(lines: list[str]) -> str:
    return "\n".join(lines)[:LIMIT]


def _job_line(job: dict) -> str:
    """One role of a company list. A value that the source does not give is left out."""
    facts = [job.get("location"), job.get("workplace_type"),
             f"posted {job['posted_at']}" if job.get("posted_at") else None,
             "closed" if is_closed(job.get("posting_status")) else None]
    shown = "; ".join(fact for fact in facts if fact)
    return f"- {job.get('title') or 'Untitled'}" + (f" ({shown})" if shown else "") + f" {job.get('url') or ''}".rstrip()


def summarize_company(body: dict) -> str:
    """A LinkedIn company: its facts, the state of its jobs section, and its roles."""
    data = body.get("data") or {}
    profile = data.get("profile") or {}
    about = (data.get("about") or {}).get("data") or {}
    lines = [f"LinkedIn company: {(body.get('entity') or {}).get('url', '')}"]
    if body.get("observed_at"):
        lines.append(f"Observed: {body['observed_at']}")
    lines += _fields(profile, (("Name", "name"), ("Tagline", "tagline"), ("Industry", "industry"),
                               ("Location", "location"), ("Employees", "employee_count")))
    lines += _fields(about, (("Website", "website"), ("Company size", "company_size"),
                             ("Headquarters", "headquarters"), ("Founded", "founded"), ("Specialties", "specialties")))
    if about.get("overview"):
        lines.append(f"Overview: {_clip(about['overview'], 500)}")
    lines.append(jobs_state(body)["text"])
    jobs = [job for job in ((data.get("jobs") or {}).get("data") or {}).get("items") or [] if isinstance(job, dict)]
    if jobs:
        lines.append("A post age is relative to the observed date. These roles are listings: no agent read the job text.")
        lines += [_job_line(job) for job in jobs[:MAX_JOBS]]
        if len(jobs) > MAX_JOBS:
            lines.append(f"(And {len(jobs) - MAX_JOBS} more roles that this reply does not show.)")
    return "\n".join(lines)[:JOBS_LIMIT]


GOOGLE_HOST = re.compile(r"(www\.)?google(\.[a-z]{2,3}){1,2}")


def result_url(url: str) -> str | None:
    """A search result's real destination, or None for Google image, favicon and opaque redirect links."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname or ""
    if host == "gstatic.com" or host.endswith(".gstatic.com"):
        return None
    if GOOGLE_HOST.fullmatch(host):
        query = parse_qs(parts.query)
        target = (query.get("q") or query.get("url") or [""])[0]
        return target if parts.path == "/url" and target.startswith(("http://", "https://")) else None
    return url


# Google starts many snippets with "Web results", the site name and a path of the result link:
# "Business Wire https://www.businesswire.com › home › 123 2 days ago — ...".
SNIPPET_NOISE = re.compile(r"^Web results\s+")
SNIPPET_LINK = re.compile(r"^.{0,80}?(https?://([^\s/]+))\S*(?:\s›\s\S+)*(?:\s·\sTranslate this page)?\s+")
# The text of Google buttons: "...Read more" sticks to the word before it. "Missing: x | Show results with: x"
# gives the query words that the page does not have. Google puts a U+200E mark before each word after the first.
SNIPPET_MISSING = re.compile(r"\s*Missing:(?: ‎?[^\s|]+)+(?: ‎?\| Show results with:.*)?$")
SNIPPET_READ_MORE = re.compile(r"(?<=\S)Read more(?=\s|$)")


def _snippet(snippet: str, url: str) -> str:
    """The snippet without the start that repeats the result link, and without the text of Google buttons.
    A link to an other site stays."""
    text = SNIPPET_NOISE.sub("", " ".join(snippet.split()))
    match = SNIPPET_LINK.match(text)
    if match and match.group(2).lower() == (urlsplit(url).hostname or "").lower():
        text = text[match.end():]
    text = SNIPPET_READ_MORE.sub("", SNIPPET_MISSING.sub("", text))
    return text.strip()


def format_search_results(body: dict) -> str:
    query = body.get("query", "")
    results = body.get("results") or []
    if not results:
        return f'Google returned no results for "{query}".'
    usable = [(result, url) for result in results if (url := result_url(result.get("url") or ""))]
    if not usable:
        return f'Google returned no usable results for "{query}" (only image or redirect links).'
    lines = [f'Google results for "{query}":']
    for index, (result, url) in enumerate(usable[:10], start=1):
        lines.append(f"{index}. {result.get('title') or '(no title)'}\n   {url}\n   "
                     f"{_clip(_snippet(result.get('snippet') or '', url), 300)}")
    skipped = len(results) - len(usable)
    if skipped:
        lines.append(f"(Skipped {skipped} Google image or redirect link{'' if skipped == 1 else 's'}.)")
    return _done(lines)


def summarize_job_search(body: dict) -> str:
    """A finished LinkedIn job search: what it covered and its roles. The viewer's own insights are left out."""
    found = body.get("result") or {}
    jobs = [job for job in found.get("items") or [] if isinstance(job, dict)]
    pages = found.get("captured_pages")
    lines = [f"LinkedIn job search: {found.get('keywords') or ''}".rstrip()]
    if found.get("location"):
        lines.append(f"Location: {found['location']}")
    if found.get("observed_at"):
        lines.append(f"Observed: {found['observed_at']}")
    if found.get("reached_end") is True:
        lines.append(f"Read to the end: {len(jobs)} roles.")
    else:
        lines.append(f"PARTIAL: {len(jobs)} roles from {pages or 'some'} page{'' if pages == 1 else 's'}. More roles "
                     "exist. Do not say that these are all the roles.")
    if jobs:
        lines.append("A post age is relative to the observed date. These roles are listings: no agent read the job "
                     "text. When you save them, give source linkedin_search.")
        lines += [_job_line(job) + (f" — {job['company_name']}" if job.get("company_name") else "")
                  for job in jobs[:MAX_JOBS]]
        if len(jobs) > MAX_JOBS:
            lines.append(f"(And {len(jobs) - MAX_JOBS} more roles that this reply does not show.)")
    return "\n".join(lines)[:JOBS_LIMIT]


DESCRIPTION_LIMIT = 12000


def summarize_job(body: dict) -> str:
    """One LinkedIn job posting, read signed in: its facts and its full text."""
    job = body.get("job") or {}
    lines = [f"LinkedIn job: {job.get('job_url', '')}"]
    if body.get("observed_at"):
        lines.append(f"Observed: {body['observed_at']}")
    lines += _fields(job, (("Title", "title"), ("Company", "company_name"), ("Company page", "company_url"),
                           ("Location", "location"), ("Workplace", "workplace_type"),
                           ("Employment", "employment_type"), ("Posted", "posted_at"), ("Applicants", "applicants")))
    if job.get("posting_status") == "closed":
        lines.append("Status: CLOSED. LinkedIn says that it no longer accepts applications.")
    text = (job.get("description_text") or "").strip()
    lines.append("Description:\n" + (text[:DESCRIPTION_LIMIT] + ("…" if len(text) > DESCRIPTION_LIMIT else "")
                                     if text else "(none)"))
    return "\n".join(lines)
