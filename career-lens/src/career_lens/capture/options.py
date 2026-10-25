"""Validated capture options shared by the CLI, API, and worker."""

from __future__ import annotations

import os
from collections.abc import Iterable

TEMPO_NAMES = ("human", "research", "brisk", "fast")
DEFAULT_TEMPO_NAME = "human"
DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE = 0.3
DEFAULT_MAX_ATTEMPTS = 3

# Pages of job results a request reads, about 25 postings each. Each page is
# one more signed-in page view on the account, so the default stays small and
# the ceiling stays well below what would make a run take hours.
JOB_LIST_KINDS = ("linkedin_company", "linkedin_job_search")
DEFAULT_JOB_PAGES = 5
MAX_JOB_PAGES = 20

# Public, logical sections callers may select for a LinkedIn person. Internal
# captures such as recommendations-given are derived from these rather than
# exposed as separate switches: selecting Recommendations means reading both
# sides of that section.
LINKEDIN_PERSON_SECTIONS = (
    # LinkedIn offers no causes page, so this one is captured as the card
    # where it stands rather than through a link -- see sections.capture_causes.
    # It is a section an operator may decline like any other.
    "featured", "experience", "causes", "education", "skills",
    "services", "connected-apps", "recommendations", "volunteering",
    "test-scores", "patents", "publications", "projects",
    "certifications", "courses", "languages", "honors", "organizations",
    "interests", "activity-posts", "activity-comments",
    "activity-articles", "activity-reactions",
)
LINKEDIN_COMPANY_SECTIONS = (
    "home", "events", "about", "services", "posts", "products", "jobs",
    "life", "people",
)
LINKEDIN_ACTIVITY_SECTIONS = frozenset({
    "activity-posts", "activity-comments", "activity-articles",
    "activity-reactions",
})
LINKEDIN_PERSON_SECTION_ALIASES = {
    "about": ("about-this-member",),
    "activity": tuple(sorted(LINKEDIN_ACTIVITY_SECTIONS)),
}


class InvalidCaptureOption(ValueError):
    """A request asked for capture behavior the worker cannot provide."""


def normalise_job_pages(value: object | None, *, kind: str) -> int | None:
    """The job pages to store for a request: the default when not given, and
    NULL for a kind that has no job list."""
    if kind not in JOB_LIST_KINDS:
        if value is not None:
            raise InvalidCaptureOption(
                "job_pages is only available for company and job search requests")
        return None
    if value is None:
        return DEFAULT_JOB_PAGES
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCaptureOption("job_pages must be a whole number")
    if not 1 <= value <= MAX_JOB_PAGES:
        raise InvalidCaptureOption(
            f"job_pages must be between 1 and {MAX_JOB_PAGES}")
    return value


def default_tempo(kind: str | None = None) -> str:
    """All new work defaults to human; explicit operator choices still win."""
    return DEFAULT_TEMPO_NAME


def normalise_tempo(value: object | None, *, kind: str | None = None) -> str:
    if value is None:
        return default_tempo(kind)
    if not isinstance(value, str) or value not in TEMPO_NAMES:
        choices = ", ".join(TEMPO_NAMES)
        raise InvalidCaptureOption(f"tempo must be one of: {choices}")
    return value


def default_max_attempts() -> int:
    """The deployment's attempt ceiling for newly submitted requests.

    Read at call time rather than at import, so a restart or a test can change
    it. An unusable value falls back rather than raising: the caller submitting
    a URL did not write this environment, and refusing their request over an
    operator's typo would turn a small misconfiguration into an outage.
    """
    raw = os.environ.get("CAREER_LENS_MAX_ATTEMPTS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS
    return value if value >= 1 else DEFAULT_MAX_ATTEMPTS


def normalise_max_attempts(value: object | None) -> int:
    """How many attempts a caller is willing to spend on one request."""
    if value is None:
        return default_max_attempts()
    # bool is an int in Python, and `max_attempts: true` is a mistake worth
    # naming rather than quietly reading as 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidCaptureOption(
            "max_attempts must be a whole number of 1 or more")
    return value


def normalise_linkedin_sections(
    value: object | None,
    *,
    kind: str = "linkedin_person",
) -> frozenset[str] | None:
    """Return selected sections for a LinkedIn recipe; ``None`` means all.

    ``none`` is deliberately an empty set rather than ``None`` so a base-only
    profile capture survives persistence through JSONB without becoming the
    default full-profile behavior.
    """
    if value is None:
        return None
    if isinstance(value, str):
        values = [part.strip().lower() for part in value.split(",")]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        values = []
        for part in value:
            if not isinstance(part, str):
                raise InvalidCaptureOption(
                    "linkedin_sections must contain section names")
            values.append(part.strip().lower())
    else:
        raise InvalidCaptureOption(
            "linkedin_sections must be 'all', 'none', or section names")

    values = [part for part in values if part]
    if not values:
        return frozenset()
    special = {part for part in values if part in {"all", "none"}}
    if special:
        if len(values) != 1:
            raise InvalidCaptureOption(
                "'all' and 'none' cannot be combined with LinkedIn sections")
        return None if values[0] == "all" else frozenset()

    if kind == "linkedin_person":
        supported = set(LINKEDIN_PERSON_SECTIONS)
        aliases = LINKEDIN_PERSON_SECTION_ALIASES
        label = "LinkedIn section"
    elif kind == "linkedin_company":
        supported = set(LINKEDIN_COMPANY_SECTIONS)
        aliases = {}
        label = "LinkedIn company section"
    else:
        raise InvalidCaptureOption(
            "linkedin_sections is only available for LinkedIn profiles")
    selected: set[str] = set()
    for name in values:
        alias = aliases.get(name)
        if alias is not None:
            selected.update(alias)
        elif name in supported:
            selected.add(name)
        else:
            raise InvalidCaptureOption(f"unknown {label}: {name}")
    # An explicit list containing every supported section is behaviorally the
    # same as ``all``. Keep one persisted representation so equivalent
    # requests do not conflict merely because one came from expanded UI
    # checkboxes.
    if selected == supported:
        return None
    return frozenset(selected)


def linkedin_capture_sections(
    selected: frozenset[str] | None,
) -> frozenset[str] | None:
    """Expand logical request sections into the capture sections they produce."""
    if selected is None:
        return None
    sections = set(selected)
    # This dialog is profile metadata, not an optional profile section. Keep
    # it with every selective capture so the structured profile has the same
    # metadata regardless of which detail sections were requested.
    sections.add("about-this-member")
    # Contact details are metadata too: what a member chose to publish about
    # reaching them belongs to the profile, not to whichever detail sections
    # this request happened to name.
    sections.add("contact-info")
    # A member who is looking says so on the profile itself, and what they
    # will take is metadata about them rather than a section they wrote.
    sections.add("open-to-work")
    if "recommendations" in sections:
        sections.add("recommendations-given")
    return frozenset(sections)


