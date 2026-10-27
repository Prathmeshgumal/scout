"""Which stored reading answers for a section, at a given instant.

One module, because the read side and the scrape side have to agree. `at`
bounds a belief from above -- what did we know by then -- and `freshness`
bounds it from below -- is what we know recent enough. Both are the same walk
over the same append-only rows, and a second implementation of either would be
a second answer to one question.

Nothing here serialises anything or knows what HTTP is.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, text

PERSON = "linkedin_person"
COMPANY = "linkedin_company"
GENERIC = "generic"
JOB = "linkedin_job"
JOB_SEARCH = "linkedin_job_search"

# The head document's section, which is also the key its resolution comes back
# under. `capture.section` uses these same names, which is why no translation
# table is needed anywhere in here.
HEAD_SECTION = {PERSON: "profile", COMPANY: "company", GENERIC: "page",
                JOB: "job", JOB_SEARCH: "results"}

# Per kind: the status table, the head table, and the column the head is keyed
# by. Everything else in this module is shared between them.
#
# A generic page has no status table because it has no sections: the page is
# the whole reading. `None` here means every section query below is skipped
# and the head answers alone -- which is the difference between "this kind has
# nothing to walk" and "this kind cannot be resolved at all", and the second is
# what it used to mean.
_TABLES = {
    PERSON: ("linkedin.person_section_status", "linkedin.person_observation",
             "profile_url"),
    COMPANY: ("linkedin.company_section_status",
              "linkedin.company_observation", "company_url"),
    GENERIC: (None, "generic.page_observation", "url_normalised"),
    JOB: (None, "linkedin.job_observation", "job_url"),
    JOB_SEARCH: (None, "linkedin.job_search_observation", "search_url"),
}

READ = "read"
DEGRADED = "degraded"
FAILED = "failed"
NEVER = "never"

# Every member of both stored vocabularies, graded deliberately. A status
# absent from this table raises rather than defaulting: one that silently
# graded as `never` would make its section quietly unreadable, and one that
# silently graded as `read` would serve a failure as data.
GRADES = {
    "parsed": READ,
    "parsed_inline": READ,
    # Normalised text and links only. Degraded, but it is data, and the caller
    # is told which it got.
    "fallback": DEGRADED,
    "error": FAILED,
    "drift": FAILED,
    # Not evidence either way. Neither mode may stop on one of these.
    "not_captured": NEVER,
    "not_requested": NEVER,
    # Company-only. "This company has no Life tab" is a successful reading of
    # the page, in the same way an empty list is.
    "not_available": READ,
    "parse_failed": FAILED,
}

# What the walk will stop on, per mode. `allow_stale=True` steps over a failed
# attempt to the last good reading; `False` stops on it and reports it.
_ACCEPTABLE = {
    True: tuple(s for s, g in GRADES.items() if g in (READ, DEGRADED)),
    False: tuple(s for s, g in GRADES.items()
                 if g in (READ, DEGRADED, FAILED)),
}

# What may satisfy a freshness window. Never a failure: an authwall thirty
# minutes ago is not an answer, and treating it as one would suppress every
# scrape of that target for the whole window.
_SATISFYING = tuple(s for s, g in GRADES.items() if g in (READ, DEGRADED))


class UnknownStatus(KeyError):
    """A stored status with no grade -- a parser change that forgot this."""


def moment(at: datetime | None) -> datetime:
    """The instant a read is asked for, never in the future.

    A value past now is clamped rather than refused: no belief exists after
    now, so a client's clock skew has nothing to gain from being a 400.
    """
    now = datetime.now(UTC)
    if at is None:
        return now
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return min(at, now)


def grade(status: str) -> str:
    try:
        return GRADES[status]
    except KeyError:
        raise UnknownStatus(
            f"{status!r} has no grade; add it to GRADES deliberately") from None


@dataclass(frozen=True)
class Resolution:
    """Which observation answers for one section, and when it was read."""

    section: str
    observation_id: int | None
    capture_id: str | None
    observed_at: datetime | None
    grade: str
    status: str | None


def walk(connection: Connection, *, kind: str, key: str, at: datetime,
         allow_stale: bool = True,
         sections: Sequence[str] | None = None) -> dict[str, Resolution]:
    """The newest reading of each section at or before `at`.

    Sections that resolve to nothing are absent from the mapping rather than
    present with a null: "we have never read this" and "we read this and it
    was empty" are different answers, and only the second is a row.
    """
    status_table, head_table, key_column = _TABLES[kind]
    found: dict[str, Resolution] = {}
    if status_table is not None:
        found = {
            row.section: Resolution(
                section=row.section, observation_id=row.observation_id,
                capture_id=row.capture_id, observed_at=row.observed_at,
                grade=grade(row.status), status=row.status)
            for row in connection.execute(text(f"""
                SELECT DISTINCT ON (s.section)
                       s.section, s.observation_id, s.status, s.observed_at,
                       s.capture_id
                FROM {status_table} s
                JOIN {head_table} o ON o.id = s.observation_id
                WHERE o.{key_column} = :key
                  AND s.observed_at <= :at
                  AND s.status = ANY(:acceptable)
                  AND (CAST(:sections AS text[]) IS NULL
                       OR s.section = ANY(CAST(:sections AS text[])))
                ORDER BY s.section, s.observed_at DESC, s.id DESC
            """), {"key": key, "at": at,
                   "acceptable": list(_ACCEPTABLE[allow_stale]),
                   "sections": list(sections) if sections is not None else None}).all()
        }

    if allow_stale and status_table is not None:
        # A section whose readings all failed has walked back to nothing, and
        # reporting that as "never looked" would be a lie -- it is the
        # difference the status vocabulary exists for. So the newest failed
        # attempt is reported with its real status and no observation, which
        # gives the caller an empty section and an honest reason for it.
        for row in connection.execute(text(f"""
            SELECT DISTINCT ON (s.section)
                   s.section, s.status, s.observed_at, s.capture_id
            FROM {status_table} s
            JOIN {head_table} o ON o.id = s.observation_id
            WHERE o.{key_column} = :key
              AND s.observed_at <= :at
              AND s.status = ANY(:failed)
              AND (CAST(:sections AS text[]) IS NULL
                   OR s.section = ANY(CAST(:sections AS text[])))
            ORDER BY s.section, s.observed_at DESC, s.id DESC
        """), {"key": key, "at": at,
               "failed": [s for s, g in GRADES.items() if g == FAILED],
               "sections": list(sections) if sections is not None else None}).all():
            found.setdefault(row.section, Resolution(
                section=row.section, observation_id=None,
                capture_id=row.capture_id, observed_at=row.observed_at,
                grade=FAILED, status=row.status))

    # The head has no status row to walk over -- it is the observation itself
    # -- so it is one more statement rather than a special case in the one
    # above. Callers get it in the same mapping and handle one shape.
    head = connection.execute(text(f"""
        SELECT id, observed_at, capture_id FROM {head_table}
        WHERE {key_column} = :key AND observed_at <= :at
        ORDER BY observed_at DESC, id DESC LIMIT 1
    """), {"key": key, "at": at}).one_or_none()
    if head is not None:
        section = HEAD_SECTION[kind]
        found[section] = Resolution(
            section=section, observation_id=head.id, capture_id=head.capture_id,
            observed_at=head.observed_at, grade=READ, status=None)
    return found


def fresh_since(connection: Connection, *, kind: str, key: str,
                since: datetime,
                sections: Sequence[str]) -> dict[str, Resolution]:
    """Every named section with a successful reading at or after `since`.

    The mirror of `walk`: same rows, same ordering, bounded below instead of
    above. A section with no entry in the result has nothing recent enough.
    """
    status_table, head_table, key_column = _TABLES[kind]
    found: dict[str, Resolution] = {}
    if status_table is not None:
        found = {
            row.section: Resolution(
                section=row.section, observation_id=row.observation_id,
                capture_id=row.capture_id, observed_at=row.observed_at,
                grade=grade(row.status), status=row.status)
            for row in connection.execute(text(f"""
                SELECT DISTINCT ON (s.section)
                       s.section, s.observation_id, s.status, s.observed_at,
                       s.capture_id
                FROM {status_table} s
                JOIN {head_table} o ON o.id = s.observation_id
                WHERE o.{key_column} = :key
                  AND s.observed_at >= :since
                  AND s.status = ANY(:satisfying)
                  AND s.section = ANY(:sections)
                ORDER BY s.section, s.observed_at DESC, s.id DESC
            """), {"key": key, "since": since, "satisfying": list(_SATISFYING),
                   "sections": list(sections)}).all()
        }
    head_section = HEAD_SECTION[kind]
    if head_section in sections:
        head = connection.execute(text(f"""
            SELECT id, observed_at, capture_id FROM {head_table}
            WHERE {key_column} = :key AND observed_at >= :since
            ORDER BY observed_at DESC, id DESC LIMIT 1
        """), {"key": key, "since": since}).one_or_none()
        if head is not None:
            found[head_section] = Resolution(
                section=head_section, observation_id=head.id,
                capture_id=head.capture_id, observed_at=head.observed_at,
                grade=READ, status=None)
    return found


def pinned(connection: Connection, *, kind: str, key: str,
           capture_id: str) -> dict[str, Resolution]:
    """One capture's reading, entire -- no walk.

    For a caller who wants a single coherent snapshot rather than the best
    current picture. Every section comes from that capture's observation,
    including the ones it never looked at, which is the whole point: this is
    what we believed at one moment, not what we believe now.
    """
    status_table, head_table, key_column = _TABLES[kind]
    head = connection.execute(text(f"""
        SELECT o.id, o.observed_at, o.capture_id
        FROM {head_table} o
        JOIN extraction_input i ON i.extraction_id = o.extraction_id
        WHERE o.{key_column} = :key AND i.capture_id = :c
        ORDER BY o.observed_at DESC, o.id DESC LIMIT 1
    """), {"key": key, "c": capture_id}).one_or_none()
    if head is None:
        return {}
    section = HEAD_SECTION[kind]
    found = {section: Resolution(
        section=section, observation_id=head.id, capture_id=head.capture_id,
        observed_at=head.observed_at, grade=READ, status=None)}
    for row in (connection.execute(text(f"""
        SELECT section, observation_id, status, observed_at, capture_id
        FROM {status_table} WHERE observation_id = :o
    """), {"o": head.id}).all() if status_table is not None else ()):
        found[row.section] = Resolution(
            section=row.section, observation_id=row.observation_id,
            capture_id=row.capture_id, observed_at=row.observed_at,
            grade=grade(row.status), status=row.status)
    return found
