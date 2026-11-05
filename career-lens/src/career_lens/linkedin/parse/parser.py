"""Assemble one structured profile from parent and child LinkedIn HTML."""

from __future__ import annotations

from collections.abc import Mapping

from lxml.etree import ParserError

from .activity import empty_activity
from .cascade import (
    activity_tabs, announced_counts, empty_profile_container_count,
    linked_sections, offers_activity, offers_contact_info, offers_open_to_work,
)
from .contracts import ParserDrift, require_announced
from .dom import content_snapshot, parse_document
from .extractors import extract_profile, extract_section
from ...capture.options import linkedin_capture_sections


COLLECTIONS = (
    "experiences",
    "education",
    "skills",
    "certifications",
    "projects",
    "courses",
    "languages",
    "honors",
    "organizations",
    "patents",
    "publications",
    "featured",
    "causes",
    "volunteering",
    "test_scores",
    "services",
    "connected_apps",
)
CAPTURE_NAMES = (
    "experience",
    "education",
    "skills",
    "certifications",
    "projects",
    "courses",
    "languages",
    "honors",
    "organizations",
    "patents",
    "publications",
    "recommendations",
    "recommendations-given",
    "activity-posts",
    "activity-comments",
    "activity-articles",
    "activity-reactions",
    "featured",
    "about-this-member",
    # The Contact info dialog. Named beside about-this-member because it is
    # read the same way: a panel the profile links to rather than a page, and
    # profile metadata rather than an optional section.
    "contact-info",
    # The Open to Work dialog. Read like the two above it: a panel the profile
    # links to rather than a page.
    "open-to-work",
    "interests",
    # Captured as the card off the profile page, because LinkedIn offers no
    # causes page to navigate to. A section like the rest from here on.
    "causes",
    "volunteering",
    "test-scores",
    "services",
    "connected-apps",
)
#: Sections the profile page proves absent by not linking into them, keyed by
#: the anchor that would prove them present. Given recommendations are a tab on
#: the recommendations page, so the one link answers for both.
PROVEN_BY_LINK = {
    "experience": "experience", "education": "education", "skills": "skills",
    "certifications": "certifications", "projects": "projects",
    "courses": "courses", "languages": "languages", "honors": "honors",
    "organizations": "organizations", "patents": "patents",
    "publications": "publications", "interests": "interests",
    "featured": "featured", "recommendations": "recommendations",
    "recommendations-given": "recommendations",
    "volunteering": "volunteering", "test-scores": "test-scores",
    "services": "services",
}

#: Activity is the one part of a profile not served under `details/`, so its
#: four sections stand or fall on the feed link -- and, where the feed exists,
#: on the tab the strip offers for each. Keyed by the label on that tab.
PROVEN_BY_ACTIVITY = {
    "activity-posts": "posts", "activity-comments": "comments",
    "activity-articles": "articles", "activity-reactions": "reactions",
}

#: The capture the strip is read from: the feed's landing tab, which every
#: profile with a feed has.
ACTIVITY_LANDING = "activity-posts"

#: LinkedIn has no causes page, so a card read off the profile page is the
#: only reading there can be -- and its absence there is the whole evidence.
PROVEN_INLINE = frozenset({"causes", "connected-apps"})

# `about-this-member` is deliberately in none of these. Every member has an
# account history behind that dialog, so a missing one says the dialog would
# not open, which is a failure to read rather than an absence.


def _mark_absent(document, statuses: dict[str, str],
                 warnings: list[dict[str, str]],
                 tabs: set[str] | None = None, *,
                 profile_complete: bool = False) -> None:
    """Say which sections the profile page proves this member does not have.

    `not_captured` carries two very different facts: the run never got the
    section, and there was no section to get. A freshness check cannot tell
    them apart, so a member with no patents re-scrapes for ever chasing
    patents that will never exist.

    The profile page settles it. LinkedIn renders no empty sections, so one it
    offers no link into is one this member does not have.
    """
    linked = linked_sections(document)
    # Absence only means anything where presence would have shown. A page we
    # cannot read one section link on is drift, and calling every section
    # empty there would report a whole unread profile as read -- silently, and
    # worse than the conflation this fixes.
    if not linked and not profile_complete:
        return
    activity = offers_activity(document)
    failed_inline = {warning.get("section") for warning in warnings
                     if warning.get("code") == "inline_extract_failed"}
    for name, status in statuses.items():
        # A section that was read, refused or never asked for has its answer.
        if status != "not_captured" or name in failed_inline:
            continue
        if name in PROVEN_BY_LINK:
            if PROVEN_BY_LINK[name] not in linked:
                statuses[name] = "not_available"
        elif name in PROVEN_BY_ACTIVITY:
            # No feed at all, or a feed whose strip does not offer this tab.
            # The strip is the only thing that separates them: the feed link
            # says a feed exists and nothing about what is on it.
            if not activity or (tabs and PROVEN_BY_ACTIVITY[name] not in tabs):
                statuses[name] = "not_available"
        elif name in PROVEN_INLINE:
            statuses[name] = "not_available"
        elif name == "contact-info" and not offers_contact_info(document):
            statuses[name] = "not_available"
        elif name == "open-to-work" and not offers_open_to_work(document):
            statuses[name] = "not_available"


# A section that reached one of these has its content in `profile`, so its
# snapshot is duplication. Anything else -- `fallback`, `error` -- keeps one,
# which is what `additional_sections` reads.
PARSED = frozenset({"parsed", "parsed_inline"})
# Sections whose reader is the generic `{name, details[], text, links[]}` one,
# where "parsed" claims less than it sounds like. They keep their snapshot
# until they have a reader that accounts for what they hold. Empty today: the
# last two, honors and organizations, were typed alongside the rest. These
# four are intentionally generic until enough live shapes have been measured.
UNTYPED: frozenset[str] = frozenset({
    "volunteering", "test-scores", "services", "connected-apps",
})
PROFILE_COLLECTION = {
    "experience": "experiences",
    "education": "education",
    "skills": "skills",
    "certifications": "certifications",
    "projects": "projects",
    "courses": "courses",
    "languages": "languages",
    "honors": "honors",
    "organizations": "organizations",
    "patents": "patents",
    "publications": "publications",
    "volunteering": "volunteering",
    "test-scores": "test_scores",
    "services": "services",
    "connected-apps": "connected_apps",
}
INLINE_RECOVERY = frozenset({
    "volunteering", "test-scores", "services", "connected-apps",
})


def _profile_skeleton(summary: dict[str, object]) -> dict[str, object]:
    profile = dict(summary)
    profile.update({name: [] for name in COLLECTIONS})
    profile.update(
        {
            "recommendations": {"received": [], "given": []},
            "activity": {
                "posts": empty_activity(),
                "comments": empty_activity(),
                "articles": empty_activity(),
                "reactions": empty_activity(),
            },
            "about_this_member": None,
            "contact_info": None,
            "open_to_work": None,
            "additional_sections": {},
        }
    )
    return profile


def _interests_skeleton() -> dict[str, object]:
    return {"captured_categories": [], "categories": {}}


def _add_interest_category(
    profile: dict[str, object],
    category: str,
    *,
    status: str,
    source_url: str,
    entries: list,
) -> None:
    interests = profile.setdefault("interests", _interests_skeleton())
    if not isinstance(interests, dict):
        interests = _interests_skeleton()
        profile["interests"] = interests
    captured = interests.setdefault("captured_categories", [])
    if isinstance(captured, list) and category not in captured:
        captured.append(category)
    categories = interests.setdefault("categories", {})
    if isinstance(categories, dict):
        categories[category] = {
            "status": status,
            "source_url": source_url,
            "entries": entries,
        }


#: A section the count contract can speak about: one that was captured and
#: produced a reading, however poor. Every other status means there is nothing
#: to compare an announced total against.
READ_STATUSES = frozenset({"parsed", "parsed_inline", "fallback"})


def parse_profile_html(
    profile_html: bytes | str,
    sections: Mapping[str, bytes | str],
    *,
    profile_url: str,
    section_urls: Mapping[str, str] | None = None,
    profile_capture: Mapping[str, object] | None = None,
    requested_sections: frozenset[str] | None = None,
) -> dict[str, object]:
    """Parse supplied captures independently and combine their usable results."""
    document = parse_document(profile_html)
    # The profile page's own snapshot went the way of the sections': every
    # block it held is structured now -- the top card's company, school,
    # pronouns, top skills and causes were the last of it -- and what remained
    # was the video player's controls, which are furniture rather than content.
    sources: dict[str, dict[str, object]] = {
        "profile": {"artifact": None, "url": profile_url},
    }
    expected = linkedin_capture_sections(requested_sections)
    statuses = {
        name: (
            "not_captured" if expected is None or name in expected
            else "not_requested"
        )
        for name in CAPTURE_NAMES
    }
    profile = _profile_skeleton(extract_profile(document, profile_url))
    # What the profile page claims each section holds, read before any section
    # is parsed. It is the one oracle here with no opinion about markup:
    # LinkedIn states the number itself.
    announced = announced_counts(document)
    warnings: list[dict[str, str]] = []
    drift: list[dict[str, str]] = []
    # Which activity tabs this member's feed offers, read off the landing
    # capture as it goes past. None until that capture is parsed, and it stays
    # None when the feed was never captured -- where nothing may be concluded
    # about a tab either way.
    tabs: set[str] | None = None

    # Every capture is preserved as text until its extractor has had a go, so
    # an unsupported DOM never means lost content. What parsed then drops its
    # snapshot: it would be a second, flatter copy of what `profile` already
    # holds -- the doubled screen-reader labels included -- and the HTML itself
    # is still in the run, named by `artifact`, for anything needing a re-read.
    for name, source in sections.items():
        url = (section_urls or {}).get(name, profile_url)
        try:
            section_document = parse_document(source)
            snapshot = content_snapshot(section_document, url)
        except (ParserError, TypeError, ValueError) as exc:
            statuses[name] = "error"
            sources[name] = {"artifact": None, "url": url, "content": None}
            warnings.append(
                {"code": "invalid_html", "section": name, "message": str(exc)}
            )
            continue
        sources[name] = {"artifact": None, "url": url, "content": snapshot}
        if name == ACTIVITY_LANDING:
            tabs = activity_tabs(section_document)
        try:
            value, section_warnings, status = extract_section(
                name, section_document, url, announced=announced.get(name))
        except ParserDrift as exc:
            statuses[name] = "drift"
            drift.append(exc.as_dict())
            profile["additional_sections"][name] = snapshot
            continue
        statuses[name] = status
        if name in PROFILE_COLLECTION and status == "parsed":
            profile[PROFILE_COLLECTION[name]] = value
        elif name in {"recommendations", "recommendations-given"} and status == "parsed":
            key = "given" if name.endswith("-given") else "received"
            profile["recommendations"][key] = value
        elif name.startswith("activity-") and status == "parsed":
            profile["activity"][name.removeprefix("activity-")] = value
        elif (name == "interests" or name.startswith("interests-")) and status == "parsed":
            # The five categories are separate captures of one page -- switching
            # a category changes neither URL nor history -- so they are gathered
            # into one object here, each as a metadata envelope under the name
            # the page gave it.
            _add_interest_category(
                profile, value["category"], status=status, source_url=url,
                entries=value["entries"],
            )
            sources[name]["interest_category"] = value["category"]
        elif name == "featured" and status == "parsed":
            profile["featured"] = value
        elif name == "causes" and status == "parsed":
            # The capture is the card itself, so this is the same list the
            # profile page carries -- read from the document that is only
            # about causes rather than from the one that is about everything.
            profile["causes"] = value
        elif name == "about-this-member" and status == "parsed":
            profile["about_this_member"] = value
        elif name == "contact-info" and status == "parsed":
            profile["contact_info"] = value
        elif name == "open-to-work" and status == "parsed":
            profile["open_to_work"] = value
        else:
            profile["additional_sections"][name] = snapshot
        if status in PARSED and name not in UNTYPED:
            sources[name].pop("content", None)
        warnings.extend(section_warnings)

    # A section only gets its own capture behind a "Show all" button. Below that
    # threshold LinkedIn renders it inline on the main page and there is nothing
    # to follow, so read it from the page we already hold. `inline` scopes the
    # read to the section's own heading: a section the profile does not have is
    # left `not_captured` rather than filled with whatever the page contained.
    for section_name, collection in PROFILE_COLLECTION.items():
        if (statuses[section_name] != "not_captured"
                and not (section_name in INLINE_RECOVERY
                         and statuses[section_name] == "fallback")):
            continue
        try:
            value, section_warnings, status = extract_section(
                section_name, document, profile_url, inline=True
            )
        except ParserDrift as exc:
            statuses[section_name] = "drift"
            drift.append(exc.as_dict())
            continue
        except (ParserError, TypeError, ValueError) as exc:
            warnings.append(
                {"code": "inline_extract_failed", "section": section_name, "message": str(exc)}
            )
            continue
        if status != "parsed" or not value:
            continue
        profile[collection] = value
        statuses[section_name] = "parsed_inline"
        profile["additional_sections"].pop(section_name, None)
        warnings.extend(section_warnings)

    if statuses["recommendations"] == "not_captured":
        try:
            value, section_warnings, status = extract_section(
                "recommendations", document, profile_url, inline=True
            )
        except ParserDrift as exc:
            statuses["recommendations"] = "drift"
            drift.append(exc.as_dict())
        except (ParserError, TypeError, ValueError) as exc:
            warnings.append(
                {"code": "inline_extract_failed", "section": "recommendations", "message": str(exc)}
            )
        else:
            if status == "parsed" and value:
                profile["recommendations"]["received"] = value
                statuses["recommendations"] = "parsed_inline"
                warnings.extend(section_warnings)

    # The fallback for a causes capture that did not arrive -- declined is
    # handled by the status test, but a card that would not screenshot must
    # not cost the list itself, which the profile page carries too. Kept out
    # of the loop above because that one enforces the announced-count
    # contract, and LinkedIn announces no total for this section.
    if statuses["causes"] == "not_captured":
        try:
            value, section_warnings, status = extract_section(
                "causes", document, profile_url, inline=True
            )
        except (ParserError, TypeError, ValueError) as exc:
            warnings.append(
                {"code": "inline_extract_failed", "section": "causes",
                 "message": str(exc)}
            )
        else:
            if status == "parsed" and value:
                profile["causes"] = value
                statuses["causes"] = "parsed_inline"
                warnings.extend(section_warnings)

    # Last, because a section is only in breach once every rung has had a go: a
    # capture that failed the positional walk and was recovered by the sweep is
    # not drift, and neither is one read off the profile page.
    for section_name, collection in PROFILE_COLLECTION.items():
        # Only a section that was captured and read is in scope. A count is a
        # promise about a capture: a section nobody asked for, one that was
        # never captured, and one whose HTML would not parse have nothing to be
        # held to. `fallback` is in scope precisely because it is a reading --
        # a capture no rung could structure is exactly what the count is for.
        #
        # Named as what qualifies rather than what does not. The excluding form
        # silently admitted `not_requested` the day selective capture added it,
        # and failed a request that asked for education and publications
        # against the 14 courses the profile page happened to announce.
        if statuses[section_name] not in READ_STATUSES:
            continue
        try:
            require_announced(
                profile[collection], announced.get(section_name),
                section=section_name,
                # The profile page shows a subset on purpose; a details capture
                # is the page that promises all of them.
                complete=statuses[section_name] != "parsed_inline",
            )
        except ParserDrift as exc:
            statuses[section_name] = "drift"
            drift.append(exc.as_dict())
            # Whatever it did read is why the contract failed. Leaving it in
            # place hands a consumer a partial section dressed as a whole one,
            # and `drift` is not something every consumer reads.
            profile[collection] = []

    # Last of all, and only over what is still `not_captured`: every reading
    # above -- capture, inline fallback, sweep -- had to have its go before an
    # empty result can mean the page had nothing rather than that we missed it.
    profile_complete = (profile_capture or {}).get("scroll_stopped") == "bottom"
    empty_containers = (
        empty_profile_container_count(document)
        if profile_complete and requested_sections != frozenset()
        else 0
    )
    unresolved_profile_sections = any(
        statuses[name] == "not_captured"
        for name in PROFILE_COLLECTION
        if expected is None or name in expected
    )
    if empty_containers and unresolved_profile_sections:
        drift.append({
            "section": "profile",
            "contract": "profile section containers",
            "expected": "content after the completed profile scroll",
            "found": f"{empty_containers} empty containers",
        })
        # The empty shell cannot prove that every unresolved section is absent.
        profile_complete = False

    _mark_absent(
        document,
        statuses,
        warnings,
        tabs,
        profile_complete=profile_complete,
    )

    return {
        "schema_version": "2.2",
        "profile": profile,
        "sources": sources,
        "section_status": statuses,
        "warnings": warnings,
        "drift": drift,
    }
