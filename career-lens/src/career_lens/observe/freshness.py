"""How stale an answer a caller will accept, and whether we have one.

The mirror of the read side: `at` bounds a belief from above, this bounds it
from below. Both go through `resolve`, so "which reading answers" has one
definition and cannot drift between the two directions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection

from . import resolve

# A whole number and a unit. No weeks and no months: a week is one more thing
# to explain, and a month is genuinely ambiguous.
PATTERN = re.compile(r"^(\d+)([mhd])$")
UNITS = {"m": 60, "h": 3600, "d": 86400}

# A window longer than a year is a caller who meant "never rescrape" and should say so
# by not scraping.
MAX_SECONDS = 366 * 86400

ACCEPTED = "a whole number and one of m, h, d -- for example 90m, 24h, 7d"


class InvalidFreshness(ValueError):
    """A freshness window that is not one."""


def parse(value: object) -> int | None:
    """Seconds, or None when freshness was not asked for.

    Not asked for means always scrape, which is what every caller written
    before this existed meant and still means.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidFreshness(f"freshness must be {ACCEPTED}")
    matched = PATTERN.match(value.strip())
    if matched is None:
        raise InvalidFreshness(f"freshness must be {ACCEPTED}")
    count = int(matched.group(1))
    if count < 1:
        raise InvalidFreshness(f"freshness must be {ACCEPTED}")
    seconds = count * UNITS[matched.group(2)]
    if seconds > MAX_SECONDS:
        raise InvalidFreshness("freshness cannot exceed 366 days")
    return seconds


@dataclass(frozen=True)
class Satisfaction:
    """Whether the stored history already answers, and what answered."""

    satisfied: bool
    stale: list[str]
    by_section: dict[str, resolve.Resolution]
    oldest_observed_at: datetime | None


def satisfies(connection: Connection, *, kind: str, key: str,
              freshness_seconds: int, sections: Sequence[str],
              now: datetime) -> Satisfaction:
    """All or nothing: one stale section scrapes the lot.

    Narrowing the scrape to only the stale sections would break the outstanding
    request comparison, which asks whether two requests have the same section
    selection. That is a decision about deduplication rather than about time.
    """
    if kind not in resolve.HEAD_SECTION:
        # A kind with no observation table has nothing that could answer, so
        # the scrape always runs.
        return Satisfaction(False, sorted(sections), {}, None)

    since = now - timedelta(seconds=freshness_seconds)
    found = resolve.fresh_since(connection, kind=kind, key=key, since=since,
                                sections=sections)
    stale = sorted(section for section in sections if section not in found)
    instants = [r.observed_at for r in found.values() if r.observed_at]
    return Satisfaction(
        satisfied=not stale,
        stale=stale,
        by_section=found,
        # The guarantee actually delivered: every requested unit is at least
        # this recent. Left off when something was stale, because then the
        # number would describe an answer nobody is getting.
        oldest_observed_at=min(instants) if instants and not stale else None,
    )


