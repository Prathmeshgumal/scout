"""Certifications, courses, languages and projects, read as typed entries.

These four came through one generic reader that returned `{name, details[],
text, links[]}` -- every fact a person actually wants left as an unlabelled
string in `details`, and on four of eight captured profiles the section heading
stored as an entry of its own ("Licenses & certifications" as a certification).

They are served by LinkedIn's newer application, where class names are build
hashes, so nothing anchors on one. What is reliable is that each entry is a
sibling under the section, and that its facts arrive as ordered text runs whose
wording is stable: "Issued Apr 2021", "Credential ID UC-086a…", "Associated
with Cornell University", "Native or bilingual proficiency". Those phrasings are
the grammar this module reads.

The same reading works on the older markup the main profile page still serves,
where a section is rendered inline: both give ordered runs, and the doubled
screen-reader copy is dropped first either way.

Fields are omitted when the page does not carry them.
"""

from __future__ import annotations

import re
from copy import deepcopy
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from lxml.html import HtmlElement


HIDDEN = ".//*[contains(@class, 'visually-hidden')]"
ISSUED = re.compile(r"^Issued\s+(?P<when>.+)$", re.IGNORECASE)
EXPIRY = re.compile(r"^Expire[sd]\s+(?P<when>.+)$", re.IGNORECASE)
CREDENTIAL_ID = re.compile(r"^Credential ID\s+(?P<id>.+)$", re.IGNORECASE)
ASSOCIATED = re.compile(r"^Associated with\s+(?P<with>.+)$", re.IGNORECASE)
# "Skills: Product Management, Project Performance, +1 skill"
SKILLS = re.compile(r"^Skills:\s*(?P<skills>.*)$", re.IGNORECASE)
SKILLS_MORE = re.compile(r"^\+\s*(?P<count>\d+)\s+skills?$", re.IGNORECASE)
PROFICIENCY = re.compile(r"proficiency|native|bilingual", re.IGNORECASE)
# "Jan 2025 – Mar 2025", "Sep 2024", "Jan 2025 - Present"
DATE_RANGE = re.compile(
    r"^(?:[A-Za-z]{3,9}\s+)?\d{4}(?:\s*[-–]\s*(?:(?:[A-Za-z]{3,9}\s+)?\d{4}|Present))?$",
    re.IGNORECASE,
)
# A course code sits beside the name: "ENMGT 5930".
COURSE_NUMBER = re.compile(r"^[A-Za-z]{0,6}\s?[-\w]*\d[-\w]*$")
CREDENTIAL_LABEL = re.compile(r"^Show credential$", re.IGNORECASE)
SHOW_LABEL = re.compile(r"^Show (credential|patent|publication)$", re.IGNORECASE)
# Newer details pages sometimes collapse adjacent labelled facts into one text
# run ("Issued Jan 2026 · Expires Jan 2029").  Older pages keep them in
# separate elements.  Split only on a typographic metadata separator; ordinary
# punctuation inside an issuer or credential name remains content.
METADATA_SEPARATOR = re.compile(r"\s*(?:·|•)\s*")
# "US 20130018674 · Issued Jan 17, 2013", "EU WO 2011121457 A1 · Issued Oct 6, 2011"
PATENT_META = re.compile(
    r"^(?P<office>[A-Z]{2})\s+(?P<number>.+?)\s*·\s*(?P<status>[A-Za-z]+)\s+(?P<when>.+)$"
)
# "International Journal of Industrial Engineering · Oct 26, 2020"
PUBLICATION_META = re.compile(r"^(?P<publisher>.+?)\s*·\s*(?P<when>[A-Za-z]{3,9}\s+\d{1,2},\s*\d{4})$")
# "Jan 17, 2013". These two sections date to the day; the rest give a month.
FULL_DATE = re.compile(r"^(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})$")
OTHER_AUTHORS = re.compile(r"^\+\s*(?P<count>\d+)$")
# "Issued by IDDBA · Jun 2022"
ISSUED_BY = re.compile(r"^Issued by\s+(?P<issuer>.+?)\s*·\s*(?P<when>.+)$", re.IGNORECASE)
# "Board Member · Feb 2022 – Present", where the half before the dot is a role.
ROLE_DATES = re.compile(r"^(?P<role>.+?)\s*·\s*(?P<range>.+)$")
# A bare address on its own line, which is how these two sections carry a link.
BARE_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
# Outbound links go through LinkedIn's interstitial, which carries the real
# address in its query. Stripping the query here would discard the destination.
REDIRECTOR = "/safety/go/"
# Skills open a LinkedIn overlay rather than addressing anything, so the names
# are kept and the address is not.
OVERLAY = "/overlay/"


def _date_part(value: str | None) -> str | None:
    """`Apr 2021` as `2021-04`. Imported where used: the extractors module
    holds the month table and imports this one, so the call is made inside the
    function rather than at module scope, which would be a cycle."""
    from .extractors import _date_part as parse

    return parse(value)


def _date_metadata(value: str | None) -> dict[str, object]:
    from .extractors import _date_metadata as parse

    return parse(value)


def _full_date(value: str | None) -> str | None:
    """`Jan 17, 2013` as `2013-01-17`."""
    from .extractors import MONTHS

    match = FULL_DATE.match((value or "").strip())
    if match is None:
        return None
    month = MONTHS.get(match.group("month").casefold())
    return f"{match.group('year')}-{month}-{int(match.group('day')):02d}" if month else None


def _destination(url: str | None) -> str | None:
    """The address a LinkedIn interstitial is standing in front of."""
    if not url or REDIRECTOR not in url:
        return url
    target = parse_qs(urlsplit(url).query).get("url")
    return target[0] if target else url


def _clean(element: HtmlElement) -> HtmlElement:
    copied = deepcopy(element)
    for node in reversed(copied.xpath(HIDDEN)):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    return copied


def parts(element: HtmlElement) -> list[str]:
    """The entry's text runs in order, with immediate repeats collapsed."""
    found: list[str] = []
    for text in _clean(element).itertext():
        value = " ".join(text.split())
        if value and (not found or found[-1] != value):
            found.append(value)
    return found


def _url(href: str | None, source_url: str) -> str | None:
    if not href or not href.strip():
        return None
    parts_ = urlsplit(urljoin(source_url, href.strip()))
    return urlunsplit((parts_.scheme, parts_.netloc, parts_.path, "", ""))


#: How many nested wrappers to descend through before giving up. LinkedIn has
#: served two shapes for the same section; the bound exists so a pathological
#: document cannot spin, not because five is meaningful.
MAX_WRAPPERS = 5


def descend(node: HtmlElement) -> HtmlElement:
    """Walk down while there is exactly one element child."""
    while True:
        children = [child for child in node if isinstance(child.tag, str)]
        if len(children) != 1:
            return node
        node = children[0]


def text_bearing(node: HtmlElement) -> list[HtmlElement]:
    """The element children that carry any text at all."""
    return [
        child for child in node
        if isinstance(child.tag, str) and " ".join(child.text_content().split())
    ]


def entry_elements(
    document: HtmlElement, section_name: str | None = None
) -> list[HtmlElement]:
    """The repeated entries of a details capture, without the heading.

    LinkedIn wraps a section in single-child divs down to the level that
    actually repeats, and `descend` follows that. What it could not follow is
    the second shape, where the entries sit inside one further wrapper that is
    a *sibling* of the heading: the level then has two children, the descent
    stops, and the whole section comes back as one entry -- which is how three
    languages were read as one, and how a heading came to be stored as a
    certification.

    So when a level yields exactly one entry and that entry is itself a list,
    descend into it and look again.

    The heading is identified by name rather than by position. Position only
    works at the outermost level; once we have descended past the heading,
    dropping the first child eats a real entry -- which is exactly what an
    earlier attempt at this did, coming out consistently one short. The
    positional rule is kept as a fallback at the outer level only, because not
    every layout's heading matches the alias table and removing it outright
    regresses sections this repo already reads.
    """
    # `descendant-or-self`, because a capture parsed down to its own <main>
    # arrives with that element as the root, and `.//main` -- a descendant
    # search -- passes straight over it and reads the section as empty. A
    # strict superset of the old expression: where a root is above <main>,
    # both select the same node.
    roots = (document.xpath("descendant-or-self::main//section")
             or document.xpath("descendant-or-self::main"))
    if not roots:
        return []
    node = descend(roots[0])
    entries: list[HtmlElement] = []
    for level in range(MAX_WRAPPERS):
        children = text_bearing(node)
        entries = [
            child for child in children
            if not _is_heading(
                section_name or "", " ".join(child.text_content().split()))
        ]
        if level == 0 and len(entries) == len(children) and len(children) > 1:
            # A lazy list can put the heading and its first batch inside one
            # wrapper, followed by later batches as siblings.  That first
            # wrapper is content, not the old leading-heading shape: expand
            # batches before applying the positional heading fallback.
            expanded = _without_batches(entries)
            if len(expanded) > len(entries):
                return _without_heading(expanded, section_name)
            entries = children[1:]
        if len(entries) != 1:
            return _without_heading(entries, section_name)
        inner = descend(entries[0])
        if not looks_like_entries(text_bearing(inner)):
            return _without_heading(entries, section_name)
        node = inner
    return _without_heading(entries, section_name)


def _without_heading(
    entries: list[HtmlElement], section_name: str | None
) -> list[HtmlElement]:
    """Flatten lazy batches, then remove a heading found inside a batch."""
    flattened = _without_batches(entries)
    kept: list[HtmlElement] = []
    for index, entry in enumerate(flattened):
        text = " ".join(entry.text_content().split())
        if _is_heading(section_name or "", text):
            continue
        # Filter strips can share the heading's first lazy batch, so their
        # wrapper contains "Skills" followed by All/category buttons rather
        # than equalling the heading.  Only the leading candidate can be this
        # header shell; later rows are real entries even if one happens to use
        # a section-like name.
        if index == 0 and _contains_heading(entry, section_name):
            continue
        kept.append(entry)
    return kept


def _contains_heading(
    element: HtmlElement, section_name: str | None
) -> bool:
    from .extractors import SECTION_HEADING_ALIASES, _heading_key

    aliases = SECTION_HEADING_ALIASES.get(
        section_name or "", (section_name or "",))
    return any(
        _heading_key(" ".join(candidate.text_content().split())) in aliases
        for candidate in element.xpath(
            "self::h1 | self::h2 | self::h3 | self::p | "
            ".//h1 | .//h2 | .//h3 | .//p"
        )
    )


def _without_batches(entries: list[HtmlElement]) -> list[HtmlElement]:
    """Replace any candidate that is itself a list of entries with its entries.

    A lazily-loaded list arrives in batches, and each batch is appended inside
    its own wrapper -- so the repeated level ends up holding three wrappers of
    10, 10 and 2 rather than 22 entries. Expanding them is not the same as
    descending: the batches sit side by side, with nothing above them to
    descend into.

    Run *after* the descent, never before: a section wrapped once and batched
    zero times descends to its entries, and flattening first would stop at the
    first level that happened to hold two of anything.

    Safe against the two things that resemble a batch and are not, because both
    fail `looks_like_entries`: a single entry descends to one child, and a
    grouped entry holds a heading beside a list of a different tag.
    """
    opened: list[HtmlElement] = []
    found = False
    for entry in entries:
        inside = text_bearing(descend(entry))
        if looks_like_entries(inside):
            # Batches can themselves contain a heading beside the first list,
            # while later batches descend directly to entries.  Recursing
            # follows either wrapper shape until the repeated entry level; the
            # depth/sameness guard in `looks_like_entries` keeps one entry's
            # fields intact.
            opened.extend(_without_batches(inside))
            found = True
        else:
            opened.append(entry)
    return opened if found else entries


def looks_like_entries(candidates: list[HtmlElement]) -> bool:
    """Whether these are a section's entries rather than one entry's fields.

    Both look identical to a count: a lone language entry holds two children,
    "French" and "Professional working proficiency", and a wrapper around three
    languages holds three. Descending into the first splits one entry's fields
    into two entries -- which is what an earlier version of this did.

    Two tells, and both are needed.

    **Depth.** A field is a leaf: a <p> or <span> holding text and nothing
    else. An entry is a container, with its own markup inside it.

    **Sameness.** LinkedIn renders a repeated level from one template, so its
    entries share a tag. A grouped experience does not: it is a <div> naming
    the employer beside a <ul> of roles at that employer, and descending into
    it turns one entry into two -- the company header, then a role list with no
    company on it.
    """
    if len(candidates) < 2:
        return False
    if len({candidate.tag for candidate in candidates}) != 1:
        return False
    return all(
        any(isinstance(child.tag, str) for child in candidate)
        for candidate in candidates
    )


def _skills(values: list[str]) -> tuple[list[str], int | None]:
    """Skill names from a "Skills: A, B, +1 skill" line, and what it withheld."""
    names: list[str] = []
    more: int | None = None
    for value in values:
        for name in (piece.strip() for piece in value.split(",")):
            if not name:
                continue
            if match := SKILLS_MORE.match(name):
                more = int(match.group("count"))
            elif name not in names:
                names.append(name)
    return names, more


def _with_skills_folded(values: list[str], skills: list[str]) -> list[str]:
    """Return the runs with the skills line pulled out into `skills`.

    LinkedIn writes the label and the names as two runs -- "Skills:" then
    "Machine Learning, AI Agents, +3 skills" -- so a bare label claims the run
    after it and nothing else. Treating everything after the label as skills
    swallows whatever follows, which here is a link's own text.
    """
    kept: list[str] = []
    index = 0
    while index < len(values):
        value = values[index]
        if match := SKILLS.match(value):
            if match.group("skills"):
                skills.append(match.group("skills"))
            elif index + 1 < len(values):
                skills.append(values[index + 1])
                index += 1
        else:
            kept.append(value)
        index += 1
    return kept


def _links(element: HtmlElement, source_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for anchor in element.xpath(".//a[@href]"):
        url = _url(anchor.get("href"), source_url)
        text = " ".join(anchor.text_content().split())
        if not url or OVERLAY in url or SKILLS.match(text):
            continue
        entry = {"text": text, "url": url} if text else {"url": url}
        if entry not in links:
            links.append(entry)
    return links


def _certification(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    skills_lines: list[str] = []
    issuer: str | None = None
    # "Skills:" and the names it introduces arrive as two runs, so a bare label
    # takes the run after it rather than everything that follows.
    rest = _with_skills_folded(values[1:], skills_lines)
    for value in rest:
        clauses = METADATA_SEPARATOR.split(value)
        # A middle dot is also ordinary prose.  It is structural only when a
        # later clause begins with a certification metadata label.
        if len(clauses) == 1 or not any(
            ISSUED.match(clause)
            or EXPIRY.match(clause)
            or CREDENTIAL_ID.match(clause)
            for clause in clauses[1:]
        ):
            clauses = [value]
        for clause in clauses:
            if match := ISSUED.match(clause):
                entry["issued_label"] = clause
                if when := _date_part(match.group("when")):
                    entry["issued_on"] = when
            elif match := EXPIRY.match(clause):
                entry["expiry_label"] = clause
                if when := _date_part(match.group("when")):
                    entry["expires_on"] = when
            elif match := CREDENTIAL_ID.match(clause):
                entry["credential_id"] = match.group("id")
            elif CREDENTIAL_LABEL.match(clause):
                continue
            elif issuer is None:
                issuer = clause

    if issuer:
        organisation: dict[str, object] = {"name": issuer}
        for link in element.xpath(".//a[@href]"):
            url = _url(link.get("href"), source_url) or ""
            if "/company/" in url or "/school/" in url:
                organisation["url"] = url
                break
        entry["issuer"] = organisation
    for label in element.xpath(".//a[@href]"):
        if CREDENTIAL_LABEL.match(" ".join(label.text_content().split())):
            if url := _url(label.get("href"), source_url):
                entry["credential_url"] = url
            break
    names, more = _skills(skills_lines)
    if names:
        entry["skills"] = names
    if more is not None:
        entry["skills_more"] = more
    if logos := element.xpath(".//img/@src"):
        entry["logo_url"] = logos[0]
    return entry


def _course(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    for value in values[1:]:
        if match := ASSOCIATED.match(value):
            entry["associated_with"] = match.group("with")
        elif "number" not in entry and COURSE_NUMBER.match(value):
            entry["number"] = value
    return entry


def _language(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    for value in values[1:]:
        if PROFICIENCY.search(value):
            entry["proficiency"] = value
            break
    return entry


def _project(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    description: list[str] = []
    skills_lines: list[str] = []
    links = _links(element, source_url)
    # A link's own label is already carried by `links`; repeating it as a line
    # of the write-up would read as part of the description.
    labels = {link.get("text") for link in links}
    for value in _with_skills_folded(values[1:], skills_lines):
        if value in labels:
            continue
        if match := ASSOCIATED.match(value):
            entry["associated_with"] = match.group("with")
        elif "date_range" not in entry and DATE_RANGE.match(value):
            entry["date_range"] = value
            entry.update(_date_metadata(value))
        else:
            description.append(value)
    if description:
        # Project write-ups are bulleted and sectioned; the breaks are content.
        entry["description"] = "\n".join(description)
    names, more = _skills(skills_lines)
    if names:
        entry["skills"] = names
    if more is not None:
        entry["skills_more"] = more
    if links:
        entry["links"] = links
    return entry


def _shown_url(element: HtmlElement, source_url: str) -> str | None:
    """Where "Show patent" / "Show publication" points, past the interstitial."""
    for anchor in element.xpath(".//a[@href]"):
        if SHOW_LABEL.match(" ".join(anchor.text_content().split())):
            return _destination(urljoin(source_url, (anchor.get("href") or "").strip()))
    return None


def _patent(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    description: list[str] = []
    for value in values[1:]:
        if match := PATENT_META.match(value):
            entry["office"] = match.group("office")
            entry["number"] = match.group("number")
            # Kept as written rather than checked against a list: LinkedIn
            # offers Pending as well as Issued, and may offer more.
            entry["status"] = match.group("status")
            entry["issued_label"] = f"{match.group('status')} {match.group('when')}"
            if when := _full_date(match.group("when")):
                entry["issued_on"] = when
        elif SHOW_LABEL.match(value) or OTHER_AUTHORS.match(value) or value == "Other authors":
            continue
        else:
            description.append(value)
    if description:
        entry["description"] = "\n".join(description)
    if url := _shown_url(element, source_url):
        entry["url"] = url
    if count := _other_authors(values):
        entry["other_authors"] = count
    return entry


def _publication(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    description: list[str] = []
    for value in values[1:]:
        if match := PUBLICATION_META.match(value):
            entry["publisher"] = match.group("publisher")
            entry["published_label"] = match.group("when")
            if when := _full_date(match.group("when")):
                entry["published_on"] = when
        elif SHOW_LABEL.match(value) or OTHER_AUTHORS.match(value) or value == "Other authors":
            continue
        else:
            description.append(value)
    if description:
        entry["description"] = "\n".join(description)
    if url := _shown_url(element, source_url):
        entry["url"] = url
    if count := _other_authors(values):
        entry["other_authors"] = count
    return entry


def _other_authors(values: list[str]) -> int | None:
    """How many co-authors the entry says it has but does not name.

    The names sit behind an overlay this parser never opens, so the count is
    the whole of what the page states.
    """
    for value in values:
        if match := OTHER_AUTHORS.match(value):
            return int(match.group("count"))
    return None


def _honor(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    """An award: who gave it, when, and what it was for."""
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    description: list[str] = []
    for value in values[1:]:
        if match := ISSUED_BY.match(value):
            entry["issuer"] = match.group("issuer")
            entry["issued_label"] = match.group("when")
            if when := _date_part(match.group("when")):
                entry["issued_on"] = when
        elif "issued_label" not in entry and _date_part(value):
            # Some awards carry a date and name no issuer.
            entry["issued_label"] = value
            entry["issued_on"] = _date_part(value)
        elif match := ASSOCIATED.match(value):
            entry["associated_with"] = match.group("with")
        elif BARE_URL.match(value):
            entry["url"] = value
        else:
            description.append(value)
    if description:
        entry["description"] = "\n".join(description)
    return entry


def _organization(element: HtmlElement, source_url: str) -> dict[str, object] | None:
    """A membership: the body, the role held in it, and for how long."""
    values = parts(element)
    if not values:
        return None
    entry: dict[str, object] = {"name": values[0]}
    description: list[str] = []
    for value in values[1:]:
        if BARE_URL.match(value):
            entry["url"] = value
        elif "role" not in entry and (match := ROLE_DATES.match(value)) and DATE_RANGE.match(match.group("range")):
            entry["role"] = match.group("role")
            entry["date_range"] = match.group("range")
            entry.update(_date_metadata(match.group("range")))
        elif "date_range" not in entry and DATE_RANGE.match(value):
            entry["date_range"] = value
            entry.update(_date_metadata(value))
        elif match := ASSOCIATED.match(value):
            entry["associated_with"] = match.group("with")
        else:
            description.append(value)
    if description:
        entry["description"] = "\n".join(description)
    return entry


READERS = {
    "certifications": _certification,
    "courses": _course,
    "languages": _language,
    "projects": _project,
    "patents": _patent,
    "publications": _publication,
    "honors": _honor,
    "organizations": _organization,
}


def extract_details(
    name: str,
    document: HtmlElement,
    source_url: str,
    *,
    elements: list[HtmlElement] | None = None,
) -> list[dict[str, object]]:
    """Read one of the four sections into typed entries."""
    reader = READERS[name]
    found = entry_elements(document, name) if elements is None else elements
    entries = [reader(element, source_url) for element in found]
    # Some layouts put the heading among the entries it heads. It names the
    # section, so an entry called "Courses" in `courses` is the heading and not
    # a course somebody took.
    return [
        entry for entry in entries
        if entry and entry.get("name") and not _is_heading(name, entry["name"])
    ]


def _is_heading(section_name: str, value: object) -> bool:
    from .extractors import SECTION_HEADING_ALIASES, _heading_key

    aliases = SECTION_HEADING_ALIASES.get(section_name, (section_name,))
    return _heading_key(str(value)) in aliases
