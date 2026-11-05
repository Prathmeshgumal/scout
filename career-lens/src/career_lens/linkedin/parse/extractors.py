"""Semantic extractors for LinkedIn profile and section documents."""

from __future__ import annotations

import re
from copy import deepcopy
from urllib.parse import urljoin, urlsplit

from lxml.html import HtmlElement

from .activity import (
    empty_activity,
    extract_activity,
    extract_articles,
    states_it_is_empty,
)
from .cascade import ENTITY_LINK, repeating_entries
from .contracts import (
    ParserDrift, absent_everywhere, require_unanimous,
)
from .details import (
    READERS as DETAIL_READERS, descend as details_descend, entry_elements,
    extract_details,
    _without_batches as details_without_batches,
    looks_like_entries as details_looks_like_entries,
    text_bearing as details_text_bearing,
)
from .. import derive
from .dom import clean_text, primary_content, unique_links
from .featured import extract_featured
from .interests import extract_interests


SPACE = re.compile(r"\s+")
COUNT = {
    "connections": re.compile(r"([\d,]+)\+?\s+connections?", re.IGNORECASE),
    "followers": re.compile(r"([\d,]+)\s+followers?", re.IGNORECASE),
}
SECTION_HEADINGS = {
    "about",
    "activity",
    "education",
    "experience",
    "featured",
    "honors & awards",
    "interests",
    "languages",
    "licenses & certifications",
    "volunteering",
    "volunteer experience",
    "test scores",
    "services",
    "connected apps",
    "recommendations",
    "skills",
}
# The heading LinkedIn renders above each section on the main profile page. A
# section absent from this map, or whose heading is absent from the page, has no
# inline content -- see `_entry_elements`.
SECTION_HEADING_ALIASES: dict[str, tuple[str, ...]] = {
    "experience": ("experience",),
    "education": ("education",),
    "skills": ("skills", "top skills"),
    "certifications": (
        "licenses & certifications",
        "licenses and certifications",
        "certifications",
    ),
    "projects": ("projects",),
    "courses": ("courses",),
    "languages": ("languages",),
    "honors": ("honors & awards", "honors and awards", "honors"),
    "organizations": ("organizations",),
    "patents": ("patents",),
    "publications": ("publications",),
    "recommendations": ("recommendations",),
    "featured": ("featured",),
    "volunteering": ("volunteering", "volunteer experience"),
    "test-scores": ("test scores",),
    "services": ("services",),
    "connected-apps": ("connected apps",),
}

GENERIC_PROFILE_SECTIONS = frozenset({
    "volunteering", "test-scores", "services", "connected-apps",
})
# Optional metadata must be judged against what the capture actually says,
# not against unrelated profiles in a corpus.  These markers are semantic and
# layout-independent: they survive both separate nodes and collapsed text runs.
DETAIL_FIELD_MARKERS = {
    "certifications": re.compile(r"\bIssued\b", re.IGNORECASE),
    "courses": re.compile(r"\bAssociated\s+with\b", re.IGNORECASE),
}
# Headings carry a total in parentheses once a section grows: "Skills (16)".
HEADING_COUNT = re.compile(r"\s*\(\s*[\d,]+\s*\)\s*$")
RECOMMENDATION_DATE = re.compile(
    r"^(?P<date>"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}"
    r"),\s*(?P<relationship>.+)$",
    re.IGNORECASE,
)
RECOMMENDATION_DATE_ANY = re.compile(
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)
CONNECTION_DEGREE = re.compile(r"^·?\s*(?P<degree>\d(?:st|nd|rd)\+?)\s*$")
# The top card writes the degree as a word, beside pronouns that look like a
# headline to anything reading by position.
DEGREE_WORD = re.compile(r"^(first|second|third)(\+)?$", re.IGNORECASE)
PRONOUNS = re.compile(r"^[A-Za-z]+/[A-Za-z]+(/[A-Za-z]+)?$")
DATE_SEPARATOR = re.compile(r"\s+[-–]\s+")
MONTHS = {
    "jan": "01",
    "january": "01",
    "feb": "02",
    "february": "02",
    "mar": "03",
    "march": "03",
    "apr": "04",
    "april": "04",
    "may": "05",
    "jun": "06",
    "june": "06",
    "jul": "07",
    "july": "07",
    "aug": "08",
    "august": "08",
    "sep": "09",
    "sept": "09",
    "september": "09",
    "oct": "10",
    "october": "10",
    "nov": "11",
    "november": "11",
    "dec": "12",
    "december": "12",
}


def normalized_text(element: HtmlElement) -> str:
    return SPACE.sub(" ", " ".join(element.itertext())).strip()


def _paragraphs(element: HtmlElement) -> list[str]:
    values: list[str] = []
    for paragraph in element.xpath(".//p"):
        text = normalized_text(paragraph)
        if text and text not in values:
            values.append(text)
    return values


def _profile_heading(content: HtmlElement, handle: str) -> HtmlElement | None:
    headings = content.xpath(".//h1 | .//h2")
    for heading in headings:
        hrefs = heading.xpath(".//a/@href")
        if any(f"/in/{handle}" in href for href in hrefs):
            return heading
    return next(
        (
            heading
            for heading in headings
            if normalized_text(heading).lower().removesuffix(" (54)") not in SECTION_HEADINGS
        ),
        None,
    )


def _top_card(heading: HtmlElement) -> HtmlElement:
    for ancestor in heading.iterancestors():
        if ancestor.tag in {"div", "section"} and len(_paragraphs(ancestor)) >= 2:
            return ancestor
    parent = heading.getparent()
    return parent if parent is not None else heading


def _about(content: HtmlElement) -> str | None:
    # Top skills is a nested card, not biography prose. Work on a copy so
    # extracting About cannot remove evidence used by the other fields.
    content = deepcopy(content)
    skills = _bulleted(content, "Top skills")
    if skills:
        card_text = "Top skills " + " • ".join(skills)
        for node in list(content.iter()):
            if node.getparent() is not None and normalized_text(node) == card_text:
                node.drop_tree()
    headings = content.xpath(
        ".//*[self::h1 or self::h2 or self::h3][translate(normalize-space(), "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='about']"
    )
    if not headings:
        return None
    heading = headings[0]
    candidates = [heading.getparent(), *heading.iterancestors()]
    for candidate in candidates:
        if candidate is None or candidate is content:
            continue
        paragraphs = [text for text in _paragraphs(candidate) if text.lower() != "about"]
        if paragraphs:
            return "\n\n".join(paragraphs)
    return None


def _count(content_text: str, name: str) -> int | None:
    match = COUNT[name].search(content_text)
    if not match:
        return None
    count_str = match.group(1).replace(",", "").strip()
    return int(count_str) if count_str else None


def _bulleted(content: HtmlElement, label: str) -> list[str]:
    """The dot-separated list a top-card block writes under its own label.

    "Top skills" keeps its list inside the same element; "Causes" puts it in the
    next one along. Both are read by finding the label and taking the first text
    from there that is actually a list.
    """
    for node in content.iter():
        if not isinstance(node.tag, str) or " ".join((node.text or "").split()) != label:
            continue
        candidates = [node.getparent(), *(node.getparent().itersiblings() if node.getparent() is not None else [])]
        for candidate in candidates:
            if candidate is None:
                continue
            text = " ".join(candidate.text_content().split()).removeprefix(label).strip()
            if "•" in text:
                return [part.strip() for part in text.split("•") if part.strip()]
    return []


def _causes(content: HtmlElement) -> list[str]:
    """The causes card's list, which is not always a list.

    `_bulleted` finds its entries by the separator LinkedIn puts between them,
    so a profile naming exactly one cause -- nothing to split on -- read as an
    absent card rather than a card with one entry in it. Here the first
    non-empty text after the label is the answer whether or not it splits.
    """
    for node in content.iter():
        if not isinstance(node.tag, str):
            continue
        if " ".join((node.text or "").split()) != "Causes":
            continue
        parent = node.getparent()
        if parent is None:
            continue
        for candidate in (parent, *parent.itersiblings()):
            text = " ".join(candidate.text_content().split())
            text = text.removeprefix("Causes").strip()
            if text:
                return [part.strip() for part in text.split("\u2022") if part.strip()]
    return []


# Where the dialog links to LinkedIn's own explanation of verifications. It
# says nothing this dialog does not already say, and it is on every member's,
# so it is not reported: a link left in `links` is one worth following.
HELP_CENTRE_LINK = re.compile(r"^https?://[^/]*linkedin\.com/help/")

# "Verified using work email in August 2025". The method and the date are the
# only parts of that sentence that are not boilerplate, and LinkedIn writes it
# the same way for every method it supports.
VERIFICATION_SENTENCE = re.compile(r"^Verified using (?P<method>.+?) in (?P<on>.+)$")


def _paired_rows(root: HtmlElement) -> list[tuple[HtmlElement, str, str]]:
    """The label/value pairs under `root`, innermost first.

    Both halves of this dialog are built the same way -- an element holding
    exactly two paragraphs, the first naming the second -- so both are read the
    same way. Only the innermost such element counts: every wrapper above one
    also contains exactly two paragraphs when there is only one row, and would
    otherwise be reported as a second copy of it.
    """
    rows: list[tuple[HtmlElement, str, str]] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        paragraphs = element.xpath(".//p")
        if len(paragraphs) != 2:
            continue
        if any(len(child.xpath(".//p")) == 2 for child in element):
            continue
        label = normalized_text(paragraphs[0])
        value = normalized_text(paragraphs[1])
        if label and value:
            rows.append((element, label, value))
    return rows


def _dialog_heading(content: HtmlElement, label: str) -> HtmlElement | None:
    for node in content.iter():
        if isinstance(node.tag, str) and node.tag in ("h2", "h3", "h4"):
            if normalized_text(node) == label:
                return node
    return None


def _account_history(content: HtmlElement) -> dict[str, object]:
    """When the account was made, and when its details last moved.

    The heading sits beside its rows rather than above a container of them, so
    the rows are read from the heading's own parent.

    Every row is reported verbatim under `rows` and nowhere else. Naming the
    three rows LinkedIn shows today would say the same thing twice, and would
    leave a row it adds tomorrow addressable only through `rows` anyway.
    """
    history: dict[str, object] = {"rows": []}
    heading = _dialog_heading(content, "Account history")
    if heading is None:
        return history
    parent = heading.getparent()
    if parent is None:
        return history
    for _, label, value in _paired_rows(parent):
        history["rows"].append({"label": label, "value": value})
    return history


def _verifications(content: HtmlElement,
                   claimed: set[HtmlElement]) -> list[dict[str, object]]:
    """What LinkedIn says it has checked about this member.

    Unlike Account history the heading is buried several wrappers deep and the
    entries are its uncles, so the container is found by climbing until an
    ancestor holds entries. `claimed` keeps that climb honest: if the structure
    ever changes enough that it reaches the ancestor both halves share, the
    account-history rows are not reported a second time as verifications.
    """
    heading = _dialog_heading(content, "Verifications")
    if heading is None:
        return []
    node: HtmlElement | None = heading.getparent()
    while node is not None:
        rows = [row for row in _paired_rows(node) if row[0] not in claimed]
        if rows:
            return [_verification(element, entity, detail)
                    for element, entity, detail in rows]
        node = node.getparent()
    return []


def _entry_logo(element: HtmlElement) -> str | None:
    """The logo beside a verification, which is not inside it.

    The two paragraphs and the figure are siblings' children rather than one
    subtree, so the logo is found by climbing out of the pair -- but only as
    far as the entry goes, which is the last ancestor still holding one pair.
    """
    node: HtmlElement | None = element
    while node is not None and len(node.xpath(".//p")) == 2:
        logos = node.xpath(".//img/@src")
        if logos:
            return logos[0].strip()
        node = node.getparent()
    return None


def _verification(element: HtmlElement, entity: str,
                  detail: str) -> dict[str, object]:
    match = VERIFICATION_SENTENCE.match(detail)
    return {
        "entity": entity,
        "detail": detail,
        "method": match.group("method") if match else None,
        "verified_on": match.group("on") if match else None,
        "logo_url": _entry_logo(element),
    }


def extract_about_this_member(document: HtmlElement,
                              source_url: str) -> dict[str, object]:
    """The About this member dialog, read as the two lists it is.

    No `text`: this dialog is short and wholly accounted for by the two lists
    below, so the flattened read of it would only repeat them alongside the
    chrome ("Done", the notification count) that is not about this member at
    all. `links` stays, minus LinkedIn's own help page, for a link the dialog
    carries that the lists cannot hold.
    """
    content = primary_content(document)
    history = _account_history(content)
    claimed = set()
    heading = _dialog_heading(content, "Account history")
    if heading is not None and heading.getparent() is not None:
        claimed = {row[0] for row in _paired_rows(heading.getparent())}
    return {
        "account_history": history,
        "verifications": _verifications(content, claimed),
        "links": [link for link in unique_links(content, source_url)
                  if not HELP_CENTRE_LINK.match(str(link.get("url", "")))],
    }


# The Contact info dialog, by the label above each detail. Keyed on the label
# because the label outlives the markup around it -- but not on headings: the
# dialog renders every label as an ordinary run, and reading it off `h3` found
# nothing at all and reported a member's whole contact card as null.
CONTACT_KEYS = {
    "website": "websites",
    "websites": "websites",
    "email": "email",
    "email address": "email",
    "phone": "phones",
    "phone number": "phones",
    "im": "im",
    "instant messaging": "im",
    "address": "address",
    "birthday": "birthday",
    "connected": "connected_on",
}

# The member's own profile, labelled with their name rather than a fixed word:
# "Jensen's profile". A key could not name that, so it is matched.
CONTACT_PROFILE_LABEL = re.compile(r"(^|\s)profile$", re.I)

# "(Company)", "(Mobile)", "(Skype)". LinkedIn renders the kind of a detail as
# a parenthesised run beside it rather than as a field of its own.
CONTACT_KIND = re.compile(r"^\((?P<label>[^)]+)\)$")

# LinkedIn sells Premium in a row shaped like every detail above it. Its own
# address is what marks it, because the copy moves -- the reply multiplier
# alone has been served as 4.6x and 5.7x.
CONTACT_PROMO = re.compile(r"/premium/products|upsellSlotId=", re.I)


def _contact_rows(content: HtmlElement) -> list[HtmlElement]:
    """The dialog's details, one element each.

    LinkedIn lays them out as the children of a lazy column. Falling back to
    the content area keeps a dialog served without that column readable as one
    row rather than as none.
    """
    for selector in ('.//*[@data-testid="lazy-column"]',
                     './/*[@data-testid="dialog-content"]'):
        found = content.xpath(selector)
        if found:
            return list(found[0])
    return []


def extract_contact_info(document: HtmlElement,
                         source_url: str) -> dict[str, object]:
    """The Contact info dialog, read as the labelled details it is.

    Only what the member published is reported. A fixed shape padded with
    nulls said nothing nine ways and buried the two answers that were real.
    The section's status already says whether the dialog was read, so absence
    here means the member left it empty rather than that nobody looked.
    """
    from .details import _destination

    content = primary_content(document)
    # No `links`: every anchor in this dialog is one of the details below, so
    # a list of them repeats each answer in a worse form -- an outbound site
    # appears there as LinkedIn's interstitial rather than as the address the
    # member gave.
    info: dict[str, object] = {}

    for row in _contact_rows(content):
        anchors = row.xpath(".//a[@href]")
        href = (anchors[0].get("href") or "").strip() if anchors else None
        if href and CONTACT_PROMO.search(href):
            continue
        runs = _visible_runs(row)
        if not runs:
            continue
        label, rest = runs[0], runs[1:]
        kind = None
        words: list[str] = []
        for run in rest:
            match = CONTACT_KIND.match(run)
            if match:
                kind = match.group("label").strip() or None
            else:
                words.append(run)
        value = " ".join(words).strip() or None
        if value is None and href is None:
            continue

        if CONTACT_PROFILE_LABEL.search(label):
            info["profile_url"] = href or value
            continue
        key = CONTACT_KEYS.get(label.strip().rstrip(":").lower())
        if key == "websites":
            # The site the member gave, not the interstitial LinkedIn puts in
            # front of it.
            info.setdefault("websites", []).append(
                {"url": _destination(href) or value, "label": kind})
        elif key == "email":
            info["email"] = value or href
        elif key == "phones":
            info.setdefault("phones", []).append(
                {"number": value, "type": kind})
        elif key == "im":
            info.setdefault("im", []).append(
                {"handle": value, "service": kind})
        elif key in ("address", "birthday", "connected_on"):
            info[key] = value
        elif value is not None:
            info.setdefault("other", []).append(
                {"label": label, "value": value})
    return info


# The Open to Work dialog, by the label above each preference. Keyed on the
# label for the reason the Contact info dialog is: the wording above a value
# outlives the markup around it.
# The Open to Work dialog, by the label beside each preference. Keyed on the
# label for the reason the Contact info dialog is -- and read off paired
# paragraphs rather than headings, because this dialog has none: its own title
# is a plain run, and the only heading in it belongs to the toast region.
OPEN_TO_WORK_KEYS = {
    "job titles": "job_titles",
    "job title": "job_titles",
    "location types": "location_types",
    "location type": "location_types",
    "locations": "locations",
    "location": "locations",
    "start date": "start_date",
    "employment types": "employment_types",
    "employment type": "employment_types",
}

# How the dialog separates one value from the next. Not the comma: a place is
# written "Seattle, WA", and splitting on the comma turns one location into two.
OPEN_TO_WORK_SEPARATOR = "\u00b7"

# The dialog opens by naming the member -- "<name>" / "is Open to Work" -- in
# the same two paragraphs every preference below it is made of. That is the
# dialog saying what it is, not something the member chose.
OPEN_TO_WORK_SUBTITLE = re.compile(r"^is open to work$", re.I)


def _open_to_work_values(value: str) -> list[str]:
    return [part.strip() for part in value.split(OPEN_TO_WORK_SEPARATOR)
            if part.strip()]


def extract_open_to_work(document: HtmlElement,
                         source_url: str) -> dict[str, object]:
    """The Open to Work dialog, read as the preferences it is.

    What a member will take is not on the profile itself -- the card says one
    place and "+4 more" -- so this dialog is the only place the whole answer
    exists.

    Only what they set is reported. Two rows shaped like preferences are not
    preferences: the dialog's own subtitle, and the job-search product
    LinkedIn sells at the foot of it. A pitch asks a question; a field does
    not.
    """
    info: dict[str, object] = {}
    for _, label, value in _paired_rows(primary_content(document)):
        key = OPEN_TO_WORK_KEYS.get(label.strip().rstrip(":").lower())
        if key == "start_date":
            info["start_date"] = value
        elif key is not None:
            info[key] = _open_to_work_values(value)
        elif label.endswith("?") or OPEN_TO_WORK_SUBTITLE.match(value):
            continue
        else:
            info.setdefault("other", []).append(
                {"label": label, "value": value})
    return info


def _top_card_facts(content: HtmlElement, profile_url: str) -> dict[str, object]:
    """The current company and school, and where contact details would be.

    Neither is a link on the top card, so they are read from the line the card
    puts them on -- "Google · Narsee Monjee Institute of Management Studies",
    which sits just above the location and just above "Contact info".
    """
    facts: dict[str, object] = {
        "current_company": None,
        "current_school": None,
        "contact_info_url": None,
        "pronouns": None,
    }
    anchors = content.xpath(".//a[contains(@href, 'contact-info')]")
    if not anchors:
        return facts
    facts["contact_info_url"] = urljoin(profile_url, anchors[0].get("href", "").strip())

    card = anchors[0]
    values: list[str] = []
    for _ in range(8):
        parent = card.getparent()
        if parent is None:
            break
        card = parent
        values = _visible_runs(card)
        if "Contact info" in values and len(values) > 4:
            break
    if "Contact info" not in values:
        return facts
    # Backwards from the label: the separator, then the location, then the line
    # naming where this person is now.
    index = values.index("Contact info") - 1
    while index >= 0 and values[index] in {"·", "•"}:
        index -= 1
    location = values[index] if index >= 0 else None
    index -= 1
    if index < 0:
        return facts
    company, separator, school = values[index].partition(" · ")
    facts["current_company"] = company.strip() or None
    facts["current_school"] = school.strip() or None if separator else None
    facts["location"] = location

    # The runs above that line are the name, then any pronouns and connection
    # degree the card shows, then the headline. Read positionally, the pronouns
    # were being stored as the headline and the headline as the location.
    head = [value for value in values[:index] if value not in {"·", "•"}]
    if head:
        facts["name"] = head[0]
    for value in head[1:]:
        if PRONOUNS.match(value):
            facts["pronouns"] = value
        elif DEGREE_WORD.match(value) or CONNECTION_DEGREE.match(value):
            continue
        elif "headline" not in facts:
            facts["headline"] = value
    return facts


def _visible_runs(element: HtmlElement) -> list[str]:
    runs: list[str] = []
    for text in element.itertext():
        value = " ".join(text.split())
        if value and (not runs or runs[-1] != value):
            runs.append(value)
    return runs


def extract_profile(document: HtmlElement, profile_url: str) -> dict[str, object]:
    """Extract stable summary fields from the parent profile capture."""
    content = primary_content(document)
    handle = urlsplit(profile_url).path.strip("/").split("/")[-1]
    heading = _profile_heading(content, handle)
    name = normalized_text(heading) if heading is not None else None
    paragraphs = _paragraphs(_top_card(heading)) if heading is not None else []
    headline = paragraphs[0] if paragraphs else None
    location = next(
        (
            text
            for text in paragraphs[1:]
            if "," in text and not COUNT["connections"].search(text)
        ),
        None,
    )
    all_text = normalized_text(content)
    return {
        "url": profile_url,
        "handle": handle,
        "name": name,
        "headline": headline,
        "location": location,
        "about": _about(content),
        "connections": _count(all_text, "connections"),
        "followers": _count(all_text, "followers"),
        "top_skills": _bulleted(content, "Top skills"),
        # The top card is authoritative where it exists: read positionally, its
        # pronouns were being stored as the headline and the headline as the
        # location. Older captures have no such card and keep the heuristics.
    } | {key: value for key, value in _top_card_facts(content, profile_url).items()
         if value is not None or key in {"current_company", "current_school",
                                         "contact_info_url", "pronouns"}}


def _section_root(document: HtmlElement) -> HtmlElement:
    content = primary_content(document)
    detailed = content.xpath(
        ".//*[contains(@data-testid, 'DetailsSection') "
        "or contains(@data-testid, 'SkillDetails')]"
    )
    return detailed[0] if detailed else content


def _heading_key(text: str) -> str:
    """Normalize a heading for comparison, dropping the "(16)" entry total."""
    return HEADING_COUNT.sub("", text).strip().casefold()


def _find_section_container(
    content: HtmlElement,
    section_name: str,
    *,
    heading_tags: tuple[str, ...] = ("h1", "h2", "h3"),
) -> HtmlElement | None:
    """Locate one section on the main profile page by its heading.

    The main page stacks every section into one tree, so an extractor handed the
    whole page has no way to tell one section's entries from the next. Anchor on
    the heading LinkedIn renders above the section, then take the sibling that
    holds the entries. Returns None when the profile has no such section, which
    is the signal that there is nothing to extract -- not a reason to widen the
    search, which is how a whole page once became a single education entry.
    """
    aliases = SECTION_HEADING_ALIASES.get(section_name, (section_name,))
    for heading in content.xpath(" | ".join(f".//{tag}" for tag in heading_tags)):
        if _heading_key(normalized_text(heading)) not in aliases:
            continue
        parent = heading.getparent()
        if parent is None:
            return heading
        grandparent = parent.getparent()
        if grandparent is None:
            return parent
        siblings = list(grandparent)
        try:
            index = siblings.index(parent)
        except ValueError:
            return parent
        # The entries live in the first populated div after the heading's own
        # wrapper; a separator may sit between the two.
        for sibling in siblings[index + 1:]:
            if sibling.tag == "hr":
                continue
            if sibling.tag == "div" and len(list(sibling)):
                return sibling
            break
        return parent
    return None


def _find_section_shell(
    content: HtmlElement,
    section_name: str,
    *,
    heading_tags: tuple[str, ...] = ("h1", "h2", "h3"),
) -> HtmlElement | None:
    """Return the section wrapper containing a heading and its sibling content."""
    aliases = SECTION_HEADING_ALIASES.get(section_name, (section_name,))
    for heading in content.xpath(" | ".join(f".//{tag}" for tag in heading_tags)):
        if _heading_key(normalized_text(heading)) not in aliases:
            continue
        parent = heading.getparent()
        if parent is None:
            return heading
        grandparent = parent.getparent()
        return grandparent if grandparent is not None else parent
    return None


def _entry_elements(
    document: HtmlElement,
    *,
    section_name: str | None = None,
    inline: bool = False,
) -> list[HtmlElement]:
    """Return one element per entry in a section.

    `inline` marks the main profile page, where sections share one tree and must
    be found by heading. Everything else is a dedicated capture whose whole
    document is the section, so the root stands on its own.

    On a dedicated capture the batch-aware descent goes first. LinkedIn
    appends a lazily-loaded list in batches of ten, each inside its own
    wrapper, and the positional walk below stops at the level above them --
    on the page header, where it finds a back button and the section's name
    and reports the section read and empty. Eight of fifteen stored
    experience captures come back empty from it, reading today only because
    the signature sweep rescues them afterwards.

    The positional walk stays as the rung beneath, because it is what every
    layout this repo already reads was measured against, and a descent that
    finds nothing must not cost a section the older one would have found.
    """
    if inline:
        container = _find_section_container(primary_content(document), section_name or "")
        if container is None:
            return []
        root = container
    else:
        descended = entry_elements(document, section_name)
        if descended:
            return descended
        root = _section_root(document)
        # LinkedIn does not mark every details page with a `*DetailsSection`
        # id. On the ones it misses, the heading and every entry sit inside a
        # single wrapper that would otherwise read as one entry holding the
        # whole section. That lone child is the tell; anchor on the heading,
        # which on these pages is a styled <p> rather than a real one.
        if section_name and len([c for c in root if isinstance(c.tag, str)]) == 1:
            container = _find_section_container(
                root, section_name, heading_tags=("h1", "h2", "h3", "p")
            )
            if container is not None:
                root = container

    entries: list[HtmlElement] = []
    for child in root:
        if not isinstance(child.tag, str):
            continue
        text = normalized_text(child)
        headings = child.xpath(".//h1 | .//h2 | .//h3")
        if not text or (headings and normalized_text(headings[0]) == text):
            continue
        entries.append(child)
    return _past_the_wrapper(entries, section_name)


def _past_the_wrapper(
    entries: list[HtmlElement], section_name: str | None
) -> list[HtmlElement]:
    """Descend when the walk stopped on a wrapper holding the whole section.

    The same second shape `details.entry_elements` learned: the entries sit
    inside one further element that is a sibling of the heading, so the walk
    above returns that wrapper as a single entry. Education read 2 of 4 this
    way -- a different function from the one that lost three languages as one,
    with the same flaw.

    Only a lone entry can be a wrapper: two or more siblings are already the
    repeated level.
    """
    aliases = SECTION_HEADING_ALIASES.get(
        section_name or "", (section_name or "",))
    named = [
        entry for entry in entries
        if _heading_key(normalized_text(entry)) not in aliases
    ]
    # Descend first, then expand batches -- a lazily-loaded list appends each
    # batch inside its own wrapper, so the repeated level can hold wrappers
    # rather than entries. Flattening before descending stops at the first
    # level holding two of anything.
    if len(named) != 1:
        return details_without_batches(named)
    inner = details_descend(named[0])
    children = details_text_bearing(inner)
    if details_looks_like_entries(children):
        return details_without_batches(children)
    return details_without_batches(entries)


def _lines(element: HtmlElement) -> list[str]:
    lines: list[str] = []
    candidates = element.xpath(".//p | .//h3 | .//h4")
    for candidate in candidates:
        text = normalized_text(candidate)
        if text and text not in lines:
            lines.append(text)
    if lines:
        return lines
    return [line for line in clean_text(element).split("\n\n") if line]


def _item_base(element: HtmlElement, source_url: str) -> dict[str, object]:
    return {
        "text": clean_text(element),
        "links": unique_links(element, source_url),
    }


def _split_pair(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    first, separator, rest = value.partition(" · ")
    return first or None, rest or None if separator else None


def _date_part(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    year_match = re.fullmatch(r"(\d{4})", text)
    if year_match:
        return year_match.group(1)
    month_match = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", text)
    if not month_match:
        return None
    month = MONTHS.get(month_match.group(1).casefold())
    return f"{month_match.group(2)}-{month}" if month else None


def _date_metadata(date_range: str | None) -> dict[str, object]:
    if not date_range:
        return {"started_on": None, "ended_on": None, "is_current": False}
    parts = DATE_SEPARATOR.split(date_range.strip(), maxsplit=1)
    start_text = parts[0] if parts else None
    end_text = parts[1] if len(parts) > 1 else None
    is_current = bool(end_text and end_text.casefold() == "present")
    return {
        "started_on": _date_part(start_text),
        "ended_on": None if is_current else _date_part(end_text),
        "is_current": is_current,
    }


def _skills(value: str) -> list[str]:
    value = re.sub(r"^Skills:\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+and \+\d+ skills?$", "", value, flags=re.IGNORECASE)
    return [part.strip() for part in value.split(",") if part.strip()]


def _experience_details(
    lines: list[str],
) -> tuple[str | None, list[str]]:
    skill_lines = [
        line for line in lines
        if line.lower().startswith("skills:") or line.lower().endswith(" skills")
    ]
    description_lines = [line for line in lines if line not in skill_lines]
    description = "\n\n".join(description_lines) or None
    skills = _skills(skill_lines[0]) if skill_lines else []
    return description, skills


def _experience_role(
    element: HtmlElement,
    source_url: str,
    *,
    grouped: bool,
) -> tuple[dict[str, object], str | None]:
    lines = _lines(element)
    links = unique_links(element, source_url)
    company: str | None = None

    if grouped:
        employment_type = None
        date_index = 1
        if len(lines) > 2 and " · " not in lines[1]:
            employment_type = lines[1]
            date_index = 2
        date_range, duration = _split_pair(
            lines[date_index] if len(lines) > date_index else None
        )
        detail_index = date_index + 1
    else:
        company, employment_type = _split_pair(lines[1] if len(lines) > 1 else None)
        date_range, duration = _split_pair(lines[2] if len(lines) > 2 else None)
        detail_index = 3

    remainder = lines[detail_index:]
    location = None
    if remainder and (
        "," in remainder[0]
        or remainder[0].casefold() in {"hybrid", "on-site", "onsite", "remote"}
        or remainder[0].casefold().endswith((" area", " district", " region"))
    ):
        location = remainder.pop(0)
    description, skills = _experience_details(remainder)
    url = links[0]["url"] if links else None

    role_data = {
        "title": lines[0],
        "date_range": date_range,
        "duration": duration,
        **_date_metadata(date_range),
        "location": location,
        "employment_type": employment_type,
        "description": description,
        "skills": skills,
        "url": url,
        "company_id": derive.company_id(url),
    }

    return role_data, company


def _grouped_experience(
    element: HtmlElement,
    role_list: HtmlElement,
    source_url: str,
) -> dict[str, object] | None:
    header = next(
        (
            child for child in element
            if isinstance(child.tag, str)
            and child is not role_list
            and child.tag != "hr"
            and normalized_text(child)
        ),
        None,
    )
    if header is None:
        return None
    header_lines = _lines(header)
    if not header_lines:
        return None

    employment_type = None
    total_duration = None
    if len(header_lines) > 1:
        employment_type, total_duration = _split_pair(header_lines[1])
        if total_duration is None:
            total_duration = employment_type
            employment_type = None

    roles = [
        _experience_role(role, source_url, grouped=True)[0]
        for role in role_list.xpath("./li")
        if normalized_text(role)
    ]
    header_links = unique_links(header, source_url)
    url = header_links[0]["url"] if header_links else None

    # No `date_range` or `duration` here. A grouped entry's header states the
    # company's tenure as one figure -- "4 yrs 3 mos" -- and no range, so the
    # only candidates are the newest role's, which describe that role and not
    # the company. Borrowing them understates a long tenure by however long ago
    # the current role began. `total_duration` is the company-level fact, and
    # the roles carry the exact dates for anything needing the span.
    return {
        "name": header_lines[0],
        "location": header_lines[2] if len(header_lines) > 2 else None,
        "employment_type": employment_type,
        "roles": roles,
        "url": url,
        "company_id": derive.company_id(url),
        "total_duration": total_duration,
    }


def extract_experience(
    document: HtmlElement,
    source_url: str,
    *,
    inline: bool = False,
    elements: list[HtmlElement] | None = None,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    found = (
        _entry_elements(document, section_name="experience", inline=inline)
        if elements is None else elements
    )
    for element in found:
        if normalized_text(element).casefold() == "experience":
            continue
        role_lists = element.xpath("./ul")
        if role_lists:
            grouped_entry = _grouped_experience(element, role_lists[0], source_url)
            if grouped_entry is not None:
                entries.append(grouped_entry)
            continue

        lines = _lines(element)
        if not lines:
            continue
        role, company = _experience_role(element, source_url, grouped=False)
        links = unique_links(element, source_url)

        entries.append(
            {
                "name": company,
                "title": lines[0],
                "date_range": role.get("date_range"),
                "duration": role.get("duration"),
                "started_on": role.get("started_on"),
                "ended_on": role.get("ended_on"),
                "is_current": role.get("is_current"),
                "location": role.get("location"),
                "employment_type": role.get("employment_type"),
                "description": role.get("description"),
                "skills": role.get("skills"),
                "url": links[0]["url"] if links else None,
                "company_id": role.get("company_id"),
                "total_duration": None,
            }
        )
    return entries


def extract_education(
    document: HtmlElement,
    source_url: str,
    *,
    inline: bool = False,
    elements: list[HtmlElement] | None = None,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    aliases = SECTION_HEADING_ALIASES.get("education", ("education",))
    for element in _education_entry_elements(
        document, inline=inline, elements=elements
    ):
        lines = _lines(element)
        if not lines:
            continue
        # `extract_experience` has always skipped its heading; this never did,
        # so every education section in the store carried a school called
        # "Education". Nothing compared the count to anything, so it was
        # invisible until the profile page's own total was read.
        if _heading_key(lines[0]) in aliases:
            continue
        detail = lines[1] if len(lines) > 1 else ""
        degree, separator, field = detail.partition(",")
        date_range = next((line for line in lines[2:] if re.search(r"\d{4}", line)), None)
        grade = next((line.partition(":")[2].strip() for line in lines if line.lower().startswith("grade:")), None)
        activities = next(
            (line.partition(":")[2].strip() for line in lines if line.lower().startswith("activities")),
            None,
        )
        skill_line = next((line for line in lines if line.lower().startswith("skills:")), "")
        base = _item_base(element, source_url)
        links = base["links"]
        description_lines = [
            line for line in lines[2:]
            if line != date_range
            and not line.lower().startswith(("grade:", "activities", "skills:"))
        ]
        entries.append(
            {
                "school": lines[0],
                "degree": degree.strip() or None,
                "field_of_study": field.strip() if separator else None,
                "date_range": date_range,
                **_date_metadata(date_range),
                "grade": grade,
                "activities": activities,
                "description": "\n\n".join(description_lines) or None,
                "skills": _skills(skill_line) if skill_line else [],
                "url": links[0]["url"] if links else None,
                **base,
            }
        )
    return entries


def _looks_like_education_row(element: HtmlElement) -> bool:
    lines = _lines(element)
    has_school_link = bool(element.xpath(".//a[contains(@href, '/school/')]"))
    has_date = any(re.search(r"\d{4}", line) for line in lines[1:])
    return len(lines) >= 2 and (has_school_link or has_date)


def _education_entry_elements(
    document: HtmlElement,
    *,
    inline: bool = False,
    elements: list[HtmlElement] | None = None,
) -> list[HtmlElement]:
    entries: list[HtmlElement] = []
    found = (
        _entry_elements(document, section_name="education", inline=inline)
        if elements is None else elements
    )
    for element in found:
        row_children = [
            child
            for child in element
            if isinstance(child.tag, str) and _looks_like_education_row(child)
        ]
        entries.extend(row_children if len(row_children) > 1 else [element])
    return entries


def extract_simple(
    document: HtmlElement,
    source_url: str,
    *,
    section_name: str | None = None,
    inline: bool = False,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for element in _entry_elements(document, section_name=section_name, inline=inline):
        lines = _lines(element)
        if lines:
            entries.append(
                {"name": lines[0], "details": lines[1:], **_item_base(element, source_url)}
            )
    return entries


def _skill_lines(element: HtmlElement) -> list[str]:
    lines: list[str] = []
    for candidate in element.xpath(".//p | .//h3 | .//h4 | .//a"):
        text = normalized_text(candidate)
        if text and text not in lines:
            lines.append(text)
    if lines:
        return lines
    return [line for line in clean_text(element).split("\n\n") if line]


def _detail_url(text: str, links: list[dict[str, str]]) -> str | None:
    for link in links:
        if link.get("text") == text:
            return link["url"]
    return None


def extract_skills(
    document: HtmlElement,
    source_url: str,
    *,
    inline: bool = False,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for element in _entry_elements(document, section_name="skills",
                                   inline=inline):
        lines = _skill_lines(element)
        if not lines:
            continue
        links = unique_links(element, source_url)
        entries.append(
            {
                "name": lines[0],
                "details": [
                    {"text": line, "url": _detail_url(line, links)}
                    for line in lines[1:]
                ],
            }
        )
    return entries


def _recommendation_lines(element: HtmlElement) -> list[str]:
    lines: list[str] = []
    for candidate in element.xpath(".//a | .//p | .//h3 | .//h4"):
        text = normalized_text(candidate)
        if candidate.tag == "a" and (
            RECOMMENDATION_DATE_ANY.search(text) or len(text) > 80
        ):
            continue
        if text and text not in lines:
            lines.append(text)
    if lines:
        return lines
    return [line for line in clean_text(element).split("\n\n") if line]


def _recommendation_date_index(lines: list[str]) -> int | None:
    matches = [index for index, line in enumerate(lines) if RECOMMENDATION_DATE.match(line)]
    return matches[0] if len(matches) == 1 else None


def _is_recommendation_card(element: HtmlElement) -> bool:
    lines = _recommendation_lines(element)
    date_index = _recommendation_date_index(lines)
    return date_index is not None and date_index >= 2 and date_index < len(lines) - 1


def _recommendation_scope(
    document: HtmlElement,
    section_name: str,
    *,
    inline: bool,
) -> HtmlElement:
    if inline:
        content = primary_content(document)
        shell = _find_section_shell(content, "recommendations")
        return shell if shell is not None else content
    return _section_root(document)


def _recommendation_cards(
    document: HtmlElement,
    section_name: str,
    *,
    inline: bool,
) -> list[HtmlElement]:
    scope = _recommendation_scope(document, section_name, inline=inline)
    candidates = [
        element
        for element in scope.xpath(".//div | .//li")
        if _is_recommendation_card(element)
    ]
    candidate_ids = {id(element) for element in candidates}
    cards: list[HtmlElement] = []
    for element in candidates:
        descendant_ids = {
            id(descendant)
            for descendant in element.xpath(".//div | .//li")
            if descendant is not element
        }
        if candidate_ids & descendant_ids:
            continue
        cards.append(element)
    return cards


def _split_connection(name_line: str) -> tuple[str, str | None]:
    name, separator, connection = name_line.partition(" · ")
    if separator:
        return name.strip(), connection.strip() or None
    return name_line.strip(), None


def _connection_degree(line: str | None) -> str | None:
    if not line:
        return None
    match = CONNECTION_DEGREE.match(line)
    return match.group("degree") if match else None


def _company_from_headline(headline: str | None) -> str | None:
    if not headline or " at " not in headline:
        return None
    company = headline.rsplit(" at ", 1)[1].strip()
    company = re.split(r"\s+[|·]\s+", company, maxsplit=1)[0].strip()
    return company or None


def _recommendation_profile_url(
    links: list[dict[str, str]],
    name: str,
) -> str | None:
    lowered = name.casefold()
    for link in links:
        if lowered and lowered in link.get("text", "").casefold():
            return link["url"]
    return links[0]["url"] if links else None


def _parse_recommendation(
    element: HtmlElement,
    source_url: str,
    *,
    direction: str,
) -> dict[str, object] | None:
    lines = _recommendation_lines(element)
    date_index = _recommendation_date_index(lines)
    if date_index is None or date_index < 2 or date_index >= len(lines) - 1:
        return None

    name, inline_connection = _split_connection(lines[0])
    line_index = 1
    # The connection degree is read only to step past it. It describes the
    # distance from the *signed-in* account to this person, not anything about
    # them, so it would change with whoever ran the capture and is not emitted.
    if _connection_degree(inline_connection) is None and line_index < date_index:
        if _connection_degree(lines[line_index]) is not None:
            line_index += 1

    headline_lines = lines[line_index:date_index]
    headline = " ".join(headline_lines).strip() or None
    date_match = RECOMMENDATION_DATE.match(lines[date_index])
    if date_match is None:
        return None

    links = unique_links(element, source_url)
    profile_url = _recommendation_profile_url(links, name)
    person_key = "to" if direction == "given" else "from"
    return {
        person_key: {
            "name": name,
            "headline": headline,
            "company": _company_from_headline(headline),
            "profile_url": profile_url,
        },
        "date": date_match.group("date"),
        "relationship": date_match.group("relationship"),
        "text": "\n\n".join(lines[date_index + 1:]),
    }


def extract_recommendations(
    document: HtmlElement,
    source_url: str,
    *,
    direction: str,
    inline: bool = False,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    section_name = "recommendations-given" if direction == "given" else "recommendations"
    for card in _recommendation_cards(document, section_name, inline=inline):
        entry = _parse_recommendation(card, source_url, direction=direction)
        if entry is not None:
            entries.append(entry)
    return entries


def _heading_only(elements: list[HtmlElement], section_name: str) -> bool:
    """Whether the walk found the section's heading and none of its entries."""
    if len(elements) > 1:
        return False
    aliases = SECTION_HEADING_ALIASES.get(section_name, (section_name,))
    return not elements or _heading_key(normalized_text(elements[0])) in aliases


def _sufficient(value: object, announced: int | None) -> bool:
    """Whether a rung's result is good enough to stop at.

    Not simply "did it return anything". The cascade originally stopped at the
    first rung yielding one entry, and the corpus showed why that is wrong:
    education came back with exactly 1 of an announced 4, which passed that
    test, so the next rung never ran even though it finds all four. A rung
    returning 1 of 4 has failed -- it has just not failed all the way to zero.

    Where LinkedIn announces no total, "at least one entry" is the only test
    available. That is a real limit, and it lands hardest on experience, which
    is never counted.
    """
    if not value:
        return False
    if announced is None or not isinstance(value, list):
        return True
    return len(value) >= announced


def _swept_entries(
    name: str,
    document: HtmlElement,
    source_url: str,
) -> object | None:
    """Read a section again from the level LinkedIn repeats.

    Only for a dedicated section capture. On the main profile page the sections
    share one tree, and a sweep scoped to the whole page would happily collect
    a neighbouring section's entries -- neither loud nor partial, which is the
    worst way this design can fail.
    """
    if name not in {"experience", "education"}:
        return None
    elements = repeating_entries(_section_root(document), ENTITY_LINK)
    if not elements:
        return None
    if name == "experience":
        return extract_experience(document, source_url, elements=elements)
    return extract_education(document, source_url, elements=elements)


def extract_section(
    name: str,
    document: HtmlElement,
    source_url: str,
    *,
    inline: bool = False,
    announced: int | None = None,
) -> tuple[object, list[dict[str, str]], str]:
    """Extract one known section, independently from all other captures.

    Set `inline` when `document` is the main profile page rather than a
    dedicated section capture, so the section is located by its heading.
    """
    # Only the typed detail readers produce these; every other branch below
    # returns before reaching the shared tail that carries them.
    detail_notes: list[dict[str, str]] = []
    if name == "experience":
        value = extract_experience(document, source_url, inline=inline)
    elif name == "education":
        value = extract_education(document, source_url, inline=inline)
    elif name == "skills":
        value = extract_skills(document, source_url, inline=inline)
    elif name in GENERIC_PROFILE_SECTIONS:
        value = extract_simple(
            document, source_url, section_name=name, inline=inline)
    elif name in {
        "activity-posts", "activity-comments", "activity-articles", "activity-reactions",
    }:
        if inline:
            return empty_activity(), [], "not_captured"
        # Articles are not feed cards -- no urn, no actor, no counts -- so the
        # tab has its own reader rather than a card reader that finds nothing.
        if name == "activity-articles":
            activity = extract_articles(document, source_url)
        else:
            activity = extract_activity(document, source_url)
        # The envelope is always a dict, so its own truthiness says nothing.
        # Whether any entry was read is what decides parsed from fallback --
        # except on a tab that states it is empty, which is parsed and empty.
        if activity["items"] or states_it_is_empty(document):
            return activity, [], "parsed"
        if activity["loaded"]:
            raise ParserDrift(
                section=name,
                contract="feed activity cards",
                expected="at least one feed-shared-update-v2 card",
                found=f"0 cards while LinkedIn announced {activity['loaded']} posts",
            )
        return activity, [
            {
                "code": "semantic_fallback",
                "section": name,
                "message": f"Could not identify structured {name} entries; content preserved in fallback",
            }
        ], "fallback"
    elif name == "causes":
        # Reads either document: the card as `capture_causes` stored it, or
        # the profile page it was read off. `primary_content` returns the
        # fragment unchanged when there is no <main> around it.
        entries = _causes(primary_content(document))
        return entries, [], "parsed" if entries else "not_captured"
    elif name == "interests" or name.startswith("interests-"):
        if inline:
            return None, [], "not_captured"
        # The capture names itself: the checked radio in the category strip
        # says which tab this is, so a section called `interests` is reported
        # as `top_voices` when that is what was on screen.
        found, entries = extract_interests(document, source_url)
        if entries:
            return {"category": found or name, "entries": entries}, [], "parsed"
        return None, [
            {
                "code": "semantic_fallback",
                "section": name,
                "message": f"Could not identify structured {name} entries; content preserved in fallback",
            }
        ], "fallback"
    elif name in {"recommendations", "recommendations-given"}:
        if inline and name == "recommendations-given":
            return [], [], "not_captured"
        direction = "given" if name.endswith("-given") else "received"
        value = extract_recommendations(
            document, source_url, direction=direction, inline=inline
        )
    elif name in DETAIL_READERS:
        # Typed readers, on the entries each layout actually repeats. `inline`
        # is the main profile page, where the section shares one tree and is
        # found by heading; a dedicated capture stands on its own.
        elements = _entry_elements(document, section_name=name, inline=inline)
        if not inline and _heading_only(elements, name):
            # The newer application nests the entries below the level this
            # walk stops at, so it comes back holding only the heading. That is
            # the tell to let `details` find them by its own descent.
            elements = None
        value = extract_details(name, document, source_url, elements=elements)
        detail_notes = _detail_field_notes(name, value, document)
    elif name == "featured":
        value = extract_featured(document, source_url)
        urns = document.xpath(".//@*[contains(., 'urn:li:activity')]")
        if not value and urns:
            raise ParserDrift(
                section=name,
                contract="featured item anchors",
                expected="at least one featured item anchor",
                found="0 items while the DOM contained an activity URN",
            )
    elif name == "about-this-member":
        if inline:
            return None, [], "not_captured"
        value = extract_about_this_member(document, source_url)
    elif name == "open-to-work":
        # No inline form: the profile card says a place and "+4 more", and the
        # whole answer exists only behind the dialog.
        if inline:
            return None, [], "not_captured"
        # Returned rather than fallen through to `_sufficient`, which asks
        # whether a list came back short. That is the wrong question for a
        # dialog: what the member filled in is their answer, not a failure to
        # read it, and an empty one was being reported as `fallback`.
        return extract_open_to_work(document, source_url), [], "parsed"
    elif name == "contact-info":
        # The dialog has no inline form: the profile carries the link that
        # opens it and nothing of what is behind it.
        if inline:
            return None, [], "not_captured"
        return extract_contact_info(document, source_url), [], "parsed"
    else:
        return None, [
            {
                "code": "unsupported_section",
                "section": name,
                "message": f"No semantic extractor for {name}; content preserved in fallback",
            }
        ], "fallback"
    if _sufficient(value, announced):
        return value, detail_notes, "parsed"
    # The walk came back short, or empty. Before settling for it, read the
    # section from the level LinkedIn repeats -- an inserted wrapper is what
    # stops the walk, and it does not change what an entry contains.
    if not inline:
        swept = _swept_entries(name, document, source_url)
        if swept and len(swept) > len(value or []):
            return swept, [
                {
                    "code": "strategy_fallback",
                    "section": name,
                    "strategy": "signature",
                    "message": (
                        f"Positional read of {name} found "
                        f"{len(value or [])} entries; recovered {len(swept)} "
                        "from the repeated level"
                    ),
                }
            ], "parsed"
    if value:
        return value, detail_notes, "parsed"
    # A section that states it holds nothing was read correctly. Calling that
    # a fallback keeps a snapshot of LinkedIn's "nothing here" line and makes
    # an empty section indistinguishable from a reader that failed.
    if states_it_is_empty(document):
        return value, [], "parsed"
    return value, [
        {
            "code": "semantic_fallback",
            "section": name,
            "message": f"Could not identify structured {name} entries; content preserved in fallback",
        }
    ], "fallback"


def _detail_field_notes(
    name: str, entries: list[dict[str, object]], document: HtmlElement
) -> list[dict[str, str]]:
    """Check optional fields against semantic evidence in this capture.

    A field marker present in the source but absent from every extracted entry
    means the reader lost data and raises drift.  No marker means LinkedIn did
    not state the field; that is a valid profile and only records a note.

    This is deliberately independent of DOM shape.  LinkedIn has served both
    separate metadata nodes and one combined text run, while also legitimately
    omitting certification dates, course associations, and proficiency.
    """
    fields = {
        "certifications": ("issued_on", "certification issued date"),
        "courses": ("associated_with", "'Associated with' course metadata"),
        "languages": ("proficiency", "language proficiency"),
    }
    if name not in fields:
        return []
    field, contract = fields[name]
    marker = DETAIL_FIELD_MARKERS.get(name)
    if marker is not None and marker.search(clean_text(document)):
        require_unanimous(entries, field, section=name, contract=contract)
        return []
    note = absent_everywhere(entries, field, section=name, contract=contract)
    return [note] if note else []
