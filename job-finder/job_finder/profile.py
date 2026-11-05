"""The candidate profile: what the user wants and can do. Each field says if the user stated it or an agent inferred it.
The resume text goes only to the two subagents that need it. It stays out of tables, events, reports and API replies."""

from __future__ import annotations

from typing import Any

PROFILE_FIELDS = ("target_titles", "seniority", "years_of_experience", "skills", "locations", "workplace_types",
                  "minimum_pay", "work_permit_needs", "must_haves", "deal_breakers", "target_companies",
                  "excluded_companies")
REQUIRED = ("target_titles", "locations")
BASES = ("stated", "inferred")
RESUME = "resume_text"


def _empty(value: Any) -> bool:
    return value in (None, "", [], {}) or (isinstance(value, str) and not value.strip())


def build_profile(args: dict) -> tuple[dict, list[str]]:
    """The profile from a save_profile call, and the required fields that it does not have. A field with no value is
    left out: an agent must not invent it."""
    profile: dict[str, Any] = {}
    for name in PROFILE_FIELDS:
        field = args.get(name)
        value, basis = (field.get("value"), field.get("basis")) if isinstance(field, dict) else (field, None)
        if not _empty(value):
            profile[name] = {"value": value, "basis": basis if basis in BASES else "inferred"}
    resume = args.get(RESUME)
    if isinstance(resume, str) and resume.strip():
        profile[RESUME] = resume.strip()
    return profile, [name for name in REQUIRED if name not in profile]


def public_profile(profile: dict | None) -> dict | None:
    """The profile without the resume text, with `has_resume` in its place."""
    if not profile:
        return None
    return {**{key: value for key, value in profile.items() if key != RESUME}, "has_resume": bool(profile.get(RESUME))}
