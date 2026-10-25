"""Fields we derive because upstream does not emit them yet.

Requested upstream in these terms (spec 5.4): keep `date_range` and `duration`
exactly as they are, and add `started_on` / `ended_on` / `is_current` beside
them, plus `company_id` beside the employer `url`.

Until that lands, this module derives them -- and `project.py` calls it only
when the document does not already carry the field. That conditional is what
makes the switchover free: the day upstream emits these, our values come from
the parser without a code change here, and deleting this file is a cleanup.

Nothing here guesses. An unparseable range yields None rather than an
approximation, because the raw string is kept alongside and a wrong derived
date is indistinguishable from a right one to everything downstream.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05",
    "jun": "06", "jul": "07", "aug": "08", "sep": "09", "oct": "10",
    "nov": "11", "dec": "12",
}

# LinkedIn writes ranges with an en dash as often as a hyphen, and appends a
# computed duration after a middle dot: "Oct 2021 - Jul 2024 · 2 yrs 10 mos".
SEPARATOR = re.compile(r"\s+[-–—]\s+")
POINT = re.compile(r"^(?:([A-Za-z]{3})[a-z]*\s+)?(\d{4})$")
PRESENT = ("present", "now")

# LinkedIn's own numeric entity id, which is stable across a slug change.
NUMERIC_COMPANY = re.compile(r"^/(company|school|showcase)/(\d+)/?$")
LINKEDIN_HOSTS = ("linkedin.com", "www.linkedin.com", "")


def _point(text: str) -> str | None:
    """`"Oct 2021"` or `"2021"` into `"2021-10"` or `"2021"`, else None."""
    match = POINT.match(text.strip())
    if not match:
        return None
    month, year = match.group(1), match.group(2)
    if month is None:
        return year
    key = month.lower()[:3]
    return f"{year}-{MONTHS[key]}" if key in MONTHS else None


def date_range(text: str | None) -> tuple[str | None, str | None, bool | None]:
    """`(started_on, ended_on, is_current)` from a rendered range.

    `is_current` is None rather than False when there is no range to read: not
    knowing whether a role is current is different from knowing it is not.
    """
    if not text or not text.strip():
        return (None, None, None)
    # The duration LinkedIn computes and appends is not part of the range.
    head = text.split("·")[0].strip()

    parts = SEPARATOR.split(head, maxsplit=1)
    if len(parts) == 1:
        # A single date, not a range: a start with no stated end, and nothing
        # said about whether it is current.
        return (_point(parts[0]), None, None)

    start, end = parts[0].strip(), parts[1].strip()
    if end.lower() in PRESENT:
        return (_point(start), None, True)
    return (_point(start), _point(end), False)


def company_id(url: str | None) -> str | None:
    """LinkedIn's numeric entity id out of a company URL, or None.

    A slug is not an id. `/company/stripe/` yields nothing, because the slug is
    the company's to change and the numeric id is not.
    """
    if not url or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    host = parts.netloc.lower().split(":", 1)[0]
    if host not in LINKEDIN_HOSTS and not host.endswith(".linkedin.com"):
        return None
    match = NUMERIC_COMPANY.match(parts.path)
    return match.group(2) if match else None
