"""The goal report as a Markdown document, for the download on the Jobs page."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .jobs import is_closed

VERDICT_LABELS = {"fit": "Fit", "partial": "Partial", "unsure": "Unsure", "not_fit": "Not a fit", None: "Not checked"}
EVIDENCE_LABELS = {"full_description": "Full description read", "snippet": "From search snippet",
                   "listing_only": "Listing only"}
# The failure kind of a source that does not exist: a missing page or a parked domain. Not a malfunction.
UNAVAILABLE = "not_found"
# Characters that Markdown can read as formatting. "$" starts math on GitHub, e.g. in "$50k-$60k".
SPECIAL = re.compile(r"([\\`*_\[\]<>$|])")


def _esc(text: Any) -> str:
    """Agent or user text as literal Markdown text."""
    return SPECIAL.sub(r"\\\1", str(text or ""))


# Words that keep their own capitals in a label.
_PROPER = {"linkedin": "LinkedIn", "url": "URL", "urls": "URLs", "hq": "HQ"}


def _humanize(key: str) -> str:
    """ "company_size" -> "Company size", "linkedin_url" -> "LinkedIn URL"."""
    words = " ".join(_PROPER.get(word.lower(), word) for word in key.replace("_", " ").replace("-", " ").split())
    return words[:1].upper() + words[1:]


def _host(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").hostname or url
    return host.removeprefix("www.")


def _is_url(text: str) -> bool:
    return text.startswith(("http://", "https://")) and " " not in text.strip()


def _link(url: str, text: str | None = None) -> str:
    href = url if "://" in url else f"https://{url}"
    return f"[{_esc(text or _host(url))}]({href.replace(')', '%29').replace(' ', '%20')})"


def _value(value: Any) -> str:
    """A company value as one line. Lists join with commas."""
    if isinstance(value, str):
        return _link(value) if _is_url(value) else _esc(value)
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_value(item) for item in value)
    if isinstance(value, dict):
        return " · ".join(f"{_esc(_humanize(k))}: {_value(v)}" for k, v in value.items() if v not in (None, "", [], {}))
    return _esc(json.dumps(value, default=str))


def _item(text: str, indent: str = "") -> str:
    """One list item. The lines after the first are indented, so they stay in the item."""
    first, *rest = (text or "").strip().splitlines() or [""]
    return "\n".join([f"{indent}- {first}", *(f"{indent}  {line}" for line in rest)])


def _time(iso: str | None, zone: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(str(iso).replace(" ", "T", 1)).astimezone(ZoneInfo(zone)).strftime(fmt)


def _span(seconds: int) -> str:
    """A length of time, e.g. "45s", "4m 34s" or "1h 02m"."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _timing(timing: dict, zone: str) -> str:
    """When the goal finished and how long it worked. While it works: when it started, and the time so far. A mandate
    goal also gives its time limit."""
    limit = f" The time limit is {_span(timing['limit_s'])}." if timing.get("limit_s") else ""
    if timing["finished_at"]:
        return (f"Finished {_time(timing['finished_at'], zone)} ({zone}), after {_span(timing['work_seconds'])} of "
                f"run time.{limit}")
    return (f"Started {_time(timing['started_at'], zone)} ({zone}). It still works: {_span(timing['work_seconds'])} "
            f"of run time so far.{limit}")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def pipeline_sentence(p: dict) -> str:
    """The pipeline as one sentence, e.g. "3 roles found for a target of 1. 3 researched, none checked for fit."."""
    target = f" for a target of {p['target']} fit" if p.get("target") else ""
    found = f"{_plural(p['jobs'], 'role')} found{target}."
    if not p["jobs"]:
        return found
    checked = f"{p['fit']} fit, {p['partial']} partial, {p['unsure']} unsure, {p['not_fit']} not a fit" if p["checked"] \
        else "none checked for fit"
    parts = [f"{p['researched']} researched", checked,
             _plural(p["noted"], "application note") if p["noted"] else "no application notes"]
    return f"{found} {', '.join(parts)}."


def _job(job: dict) -> list[str]:
    out = [f"### {_esc(job['title'])}, {_esc(job['company_name'])}", ""]
    verdict = VERDICT_LABELS[job["fit"]] + (", more than the request" if job.get("extra") else "")
    facts = [job["location"], job["workplace_type"], f"Posted {job['posted_at']}" if job["posted_at"] else None,
             "Closed" if is_closed(job["posting_status"]) else None]
    shown = " · ".join(_esc(fact) for fact in facts if fact)
    out += [f"**Verdict:** {verdict}  ", *([f"{shown}  "] if shown else []),
            f"**Evidence:** {EVIDENCE_LABELS[job['evidence_level']]}  ", f"**Link:** {_link(job['url'])}", ""]
    if job["fit"]:
        out += [_esc(job["fit_reasons"]), ""]
        if job["fit_evidence"]:
            out += [*(_item(_esc(item)) for item in job["fit_evidence"]), ""]
    note = job.get("note")
    if note:
        out += ["#### Application note", "", f"**Why this role matches:** {_esc(note['why_match'])}", "",
                f"**Resume points to put first:** {_esc(note['resume_points'])}", "",
                f"**Gaps to prepare for:** {_esc(note['gaps'])}", ""]
    notes = [f"{n['agent']}: {_esc(n['text'])}" for n in job["notes"] if n["kind"] == "note"]
    if notes:
        out += ["**Notes**", "", *(_item(n) for n in notes), ""]
    for row in job["research"]:
        out += [f"#### Company {_esc(_humanize(row['kind']).lower())}", ""]
        if row.get("from_goal"):
            out += [f"From the goal \"{_esc(row['from_goal'])}\".", ""]
        for key, value in row["data"].items():
            if value not in (None, "", [], {}):
                out.append(_item(f"**{_esc(_humanize(key))}:** {_value(value)}"))
        if row["sources"]:
            sources = ", ".join(_link(s) if _is_url(s) else _esc(s) for s in row["sources"])
            out.append(_item(f"**Sources:** {sources}"))
        out.append("")
    return out


def _with_role(gap: dict) -> str:
    return f"{gap['role']}: {gap['text']}" if gap.get("role") else gap["text"]


def _rejected(row: dict) -> str:
    company = f" ({_esc(row['company_name'])})" if row.get("company_name") else ""
    return f"**{_esc(row['title'])}**{company}: {_esc(row['reason'])}"


def _target(scrape: dict) -> str:
    return _link(scrape["target"]) if _is_url(scrape["target"]) else _esc(scrape["target"])


def _not_taken(report: dict) -> list[str]:
    not_fit = [job for job in report["jobs"] if job["fit"] == "not_fit"]
    nt = report["not_taken"]
    if not (not_fit or any(nt.values())):
        return ["Nothing was left out. No agent reported missing data or failed work.", ""]
    out: list[str] = []
    if not_fit:
        out += ["### Roles that are not a fit", "",
                *(_item(f"**{_esc(job['title'])}, {_esc(job['company_name'])}:** {_esc(job['fit_reasons'])}")
                  for job in not_fit), ""]
    if nt.get("rejected"):
        out += ["### Roles that were not taken", "", *(_item(_rejected(r)) for r in nt["rejected"]), ""]
    if nt["remarks"]:
        out += ["### Notes from the agents", "", *(_item(f"{r['agent']}: {_esc(r['text'])}") for r in nt["remarks"]), ""]
    if nt["missing"]:
        out += ["### Data that the agents could not find", "",
                *(_item(f"{m['agent']}: {_esc(_with_role(m))}") for m in nt["missing"]), ""]
    # A failed step or scrape whose source does not exist is not failed work: it has its own list.
    gone_steps = [s for s in nt["failed_steps"] if s.get("failure_kind") == UNAVAILABLE]
    gone_scrapes = [s for s in nt["failed_scrapes"] if s["status"] == "failed" and s.get("failure_kind") == UNAVAILABLE]
    failed_steps = [s for s in nt["failed_steps"] if s not in gone_steps]
    failed_scrapes = [s for s in nt["failed_scrapes"] if s not in gone_scrapes]
    if failed_steps or failed_scrapes:
        out += ["### Failed work", ""]
        out += [_item(f"{s['agent'] or 'main'}: {_esc(s['label'])} ({_esc(s['summary'] or 'failed')})")
                for s in failed_steps]
        for scrape in failed_scrapes:
            why = _esc(", ".join(part for part in (scrape["status"], scrape.get("failure_kind")) if part))
            out.append(_item(f"Scrape of {_target(scrape)} ({why})"))
        out.append("")
    if gone_steps or gone_scrapes:
        out += ["### Source unavailable", ""]
        out += [_item(f"{s['agent'] or 'main'}: {_esc(s['label'])} ({_esc(UNAVAILABLE)})") for s in gone_steps]
        out += [_item(f"Scrape of {_target(scrape)} ({_esc(UNAVAILABLE)})") for scrape in gone_scrapes]
        out.append("")
    return out


def render_markdown(report: dict, zone: str = "UTC") -> str:
    """zone: the time zone of the reader. The times of the report show in it."""
    goal = report["goal"]
    jobs = [job for job in report["jobs"] if job["fit"] != "not_fit"]
    out = [f"# {_esc(report['title'])}", ""]
    about = [f"**Request:** {_esc(' '.join(report['request'].split()))}"]
    if report.get("brief"):
        about.append(f"**Criteria:** {_esc(report['brief']['criteria'])}")
    out += ["  \n".join(about), ""]
    out += [f"{pipeline_sentence(report['pipeline'])} Spent \\${goal['spend_usd']:.2f} "
            f"of the \\${goal['max_spend_usd']:.2f} limit.", "",
            f"Goal `{goal['id']}`, status: {goal['status']}. {_timing(report['timing'], zone)}", ""]
    if report["warnings"]:
        out += ["## Warnings", "", *(_item(_esc(w["text"])) for w in report["warnings"]), ""]
    out += [f"## Roles ({len(jobs)})", ""]
    if jobs:
        for job in jobs:
            out += _job(job)
    else:
        out += ["No roles yet.", ""]
    out += ["## Not taken", "", *_not_taken(report)]
    return "\n".join(out).rstrip() + "\n"
