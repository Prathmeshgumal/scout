"""LinkedIn's two layout variants, as a regression corpus.

LinkedIn serves two different shapes for the same section. They are not a
before and an after: across 30 runs on 2026-08-18, the `research` account saw
only the flat shape (14 of 14) while `research-2` saw both (9 flat, 7 wrapped).
So which shape arrives is not predictable from the account, the profile, or the
day -- the parser has to read both, always.

`flat` and `wrapped` name the shapes, not the accounts. See
docs/superpowers/specs/2026-08-18-parser-corpus-findings.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from career_lens.linkedin.parse import parse_profile_html
from career_lens.linkedin.parse.parser import PROFILE_COLLECTION
from career_lens.linkedin.parse.cascade import (
    announced_counts, linked_sections, offers_activity,
)
from career_lens.linkedin.parse.contracts import ParserDrift, require_announced
from career_lens.linkedin.parse.details import entry_elements, extract_details
from career_lens.linkedin.parse.dom import parse_document
from career_lens.linkedin.parse.extractors import (
    _sufficient, extract_education, extract_section,
)

DRIFT = Path(__file__).parent / "fixtures" / "drift"

AFREEN = "https://www.linkedin.com/in/afreen-munshi/"
TUSHAR = "https://www.linkedin.com/in/tusharbhardwaj19/"
ZAHEER = "https://www.linkedin.com/in/zaheerabbasza/"
RUPINDER = "https://www.linkedin.com/in/rupinder-kaur-12368b9a/"
GULATI = "https://www.linkedin.com/in/gulati-sanya/"


def load(name: str) -> dict[str, str]:
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted((DRIFT / name).glob("*.html"))
    }


def parsed(name: str, url: str) -> dict:
    files = load(name)
    profile = files.pop("profile")
    return parse_profile_html(
        profile, files, profile_url=url,
        section_urls={n: f"{url}details/{n}/" for n in files},
    )


def section(name: str, role: str) -> "object":
    return parse_document(load(name)[role])


def roles(document: dict) -> int:
    """Roles, not entries: an entry may group several roles at one employer."""
    total = 0
    for entry in document["profile"]["experiences"]:
        grouped = entry.get("roles")
        total += len(grouped) if grouped else 1
    return total


# --- the variants are the same data ----------------------------------------

def test_the_two_variants_carry_the_same_underlying_data():
    # Nothing was lost at the scraper. Whatever differs downstream is
    # extraction, which is what makes this a parser problem.
    for role, count in (("experience", 6), ("certifications", 5)):
        for name in ("afreen-flat", "afreen-wrapped"):
            assert load(name)[role].count("/company/") >= count, (name, role)


def test_the_shapes_are_told_apart_by_structure_not_by_a_marker():
    # There is no attribute, class or flag that identifies the variant -- the
    # obvious candidate (data-image-instant-paint) appears on captures of both
    # shapes. Structure is the only tell, which is why the walk has to adapt
    # rather than branch on a detected variant.
    flat = load("afreen-flat")["certifications"]
    wrapped = load("afreen-wrapped")["certifications"]

    assert ("data-image-instant-paint" in flat) == (
        "data-image-instant-paint" in wrapped)


# --- defect 1: the wrapper variant, details.entry_elements ------------------

@pytest.mark.parametrize("variant", ["zaheer-flat", "zaheer-wrapped"])
def test_both_languages_variants_read_the_same_three_languages(variant):
    read = extract_details(
        "languages", section(variant, "languages"), f"{ZAHEER}details/languages/")

    assert [entry["name"] for entry in read] == ["English", "Japanese", "Urdu"]


def test_the_wrapped_variant_does_not_lose_its_first_entry():
    # The failure that appears if the descent is fixed but the heading is still
    # dropped by position: the first entry is eaten at the inner level, where
    # there is no heading to drop.
    read = extract_details(
        "languages", section("zaheer-wrapped", "languages"),
        f"{ZAHEER}details/languages/")

    assert read[0]["name"] == "English"


@pytest.mark.parametrize("variant", ["afreen-flat", "afreen-wrapped"])
def test_both_variants_read_every_certification(variant):
    read = extract_details(
        "certifications", section(variant, "certifications"),
        f"{AFREEN}details/certifications/")

    assert len(read) == 5


def test_a_section_heading_is_never_stored_as_an_entry():
    # How a heading came to be stored as a certification in the first place.
    document = parse_document("""
      <html><body><main><section><div><div>
        <div><p>Licenses &amp; certifications</p></div>
        <div><a href="/company/1/">Issuer</a><p>PMP</p><p>Issued Aug 2025</p></div>
        <div><a href="/company/1/">Issuer</a><p>CSM</p><p>Issued Sep 2025</p></div>
      </div></div></section></main></body></html>
    """)

    found = entry_elements(document, "certifications")

    assert len(found) == 2
    assert all("Licenses" not in e.text_content() for e in found)


def test_an_unnamed_leading_child_is_still_treated_as_the_heading():
    # Not every layout's heading matches the alias table. Dropping the
    # positional rule outright regresses sections this repo already reads.
    document = parse_document("""
      <html><body><main><section><div><div>
        <div><p>A heading we have no alias for</p></div>
        <div><p>Entry one</p><p>detail</p></div>
        <div><p>Entry two</p><p>detail</p></div>
      </div></div></section></main></body></html>
    """)

    assert len(entry_elements(document, "courses")) == 2


def test_a_heading_inside_the_first_lazy_batch_does_not_drop_that_batch():
    """Later lazy batches are siblings of the heading-bearing first batch."""
    document = parse_document("""
      <html><body><main><section aria-label="Primary content">
        <div data-testid="lazy-column">
          <div><div>
            <div><p>Licenses &amp; certifications</p></div>
            <div>
              <div><p>Certificate one</p><p>Issuer</p><p>Issued Jan 2024</p></div>
              <div><p>Certificate two</p><p>Issuer</p><p>Issued Feb 2024</p></div>
            </div>
          </div></div>
          <div><div><div>
            <div><p>Certificate three</p><p>Issuer</p><p>Issued Mar 2024</p></div>
            <div><p>Certificate four</p><p>Issuer</p><p>Issued Apr 2024</p></div>
          </div></div></div>
        </div>
      </section></main></body></html>
    """)

    found = extract_details(
        "certifications", document,
        "https://www.linkedin.com/in/jane/details/certifications/")

    assert [entry["name"] for entry in found] == [
        "Certificate one", "Certificate two", "Certificate three", "Certificate four"
    ]


# --- defect 2: the same flaw on the other walk ------------------------------

@pytest.mark.parametrize("variant", ["tushar-flat", "tushar-wrapped"])
def test_both_variants_read_every_education_entry(variant):
    read = extract_education(
        section(variant, "education"), f"{TUSHAR}details/education/")

    assert len(read) == 4


# --- defect 3: education stored its own heading -----------------------------

def test_education_never_reports_a_school_called_education():
    # This shipped on both accounts and was invisible because nothing compared
    # the count to anything.
    for variant in ("tushar-flat", "tushar-wrapped"):
        read = extract_education(
            section(variant, "education"), f"{TUSHAR}details/education/")

        assert [e for e in read if e["school"].casefold() == "education"] == []


# --- defect 4: a clean read is never discarded ------------------------------

def test_languages_without_a_proficiency_are_still_languages():
    # LinkedIn lets a language be listed with no proficiency. Three correct
    # entries used to be thrown away because none of them carried one.
    document = parsed("rupinder-noproficiency", RUPINDER)

    assert [e["name"] for e in document["profile"]["languages"]] == [
        "English", "Hindi", "Punjabi"]
    assert document["section_status"]["languages"] == "parsed"


# --- defect 5: experience buried below the walk -----------------------------

@pytest.mark.parametrize("variant,expected", [
    ("afreen-flat", 8), ("afreen-wrapped", 8)])
def test_both_variants_read_every_experience_role(variant, expected):
    assert roles(parsed(variant, AFREEN)) == expected


def test_the_recovered_experience_matches_the_other_variant():
    # The strongest statement available: both shapes produce the same
    # employers in the same order, grouped entries intact.
    def employers(name):
        return [e["name"] for e in parsed(name, AFREEN)["profile"]["experiences"]]

    assert employers("afreen-wrapped") == employers("afreen-flat")


@pytest.mark.parametrize("variant", ["gulati-flat", "gulati-wrapped"])
def test_a_deeply_buried_experience_section_is_still_read(variant):
    # 13 roles on one variant, 0 on the other before this work.
    assert roles(parsed(variant, GULATI)) >= 13


# --- the whole document -----------------------------------------------------

def test_neither_variant_reports_drift_on_a_profile_that_parsed():
    for name, url in (("afreen-flat", AFREEN), ("afreen-wrapped", AFREEN)):
        assert parsed(name, url)["drift"] == [], name


# --- the announced-count contract -------------------------------------------

def test_the_profile_page_announces_the_counts_it_states():
    # The href names the section, so no English noun has to be mapped:
    # "5 licenses" lands under `certifications` because the link points at
    # /details/certifications/.
    counts = announced_counts(parse_document(load("afreen-flat")["profile"]))

    assert counts == {"certifications": 5, "honors": 6, "organizations": 4}


def test_experience_announces_no_count_so_it_gets_no_contract():
    # "Show all experiences", with no number. This is why the count contract
    # alone would never have caught the original incident, and why the sweep
    # has to exist.
    counts = announced_counts(parse_document(load("afreen-flat")["profile"]))

    assert "experience" not in counts


def test_a_details_page_that_reads_short_is_drift():
    # This page is what "Show all" opens. It holds all five or it is broken.
    with pytest.raises(ParserDrift) as raised:
        require_announced([{"name": "a"}], 5, section="certifications",
                          complete=True)

    assert raised.value.section == "certifications"
    assert "5" in raised.value.expected
    assert "1" in raised.value.found


def test_a_details_page_that_reads_all_of_them_is_not_drift():
    require_announced([{"name": str(n)} for n in range(5)], 5,
                      section="certifications", complete=True)


def test_an_inline_read_is_only_held_to_being_non_empty():
    # The profile page shows the first few by design -- three of five is what
    # "Show all" means. Holding a subset to the full total would raise drift on
    # every correctly-parsed inline section.
    require_announced([{"name": "a"}, {"name": "b"}], 5,
                      section="certifications", complete=False)

    with pytest.raises(ParserDrift):
        require_announced([], 5, section="certifications", complete=False)


def test_no_announced_count_means_no_contract():
    require_announced([], None, section="experience", complete=True)


def test_a_section_that_cannot_be_read_at_all_is_reported_as_drift():
    # The profile page says 5 licenses; the capture is replaced with markup no
    # rung can read. The document must say so rather than reporting an empty
    # section as a successful read.
    files = load("afreen-flat")
    profile = files.pop("profile")
    files["certifications"] = "<html><body><main></main></body></html>"

    document = parse_profile_html(
        profile, files, profile_url=AFREEN,
        section_urls={n: f"{AFREEN}details/{n}/" for n in files})

    assert document["section_status"]["certifications"] == "drift"
    assert [d["section"] for d in document["drift"]] == ["certifications"]
    # Reading `drift` is optional; reading `certifications` is not.
    assert document["profile"]["certifications"] == []


# --- the count drives the cascade -------------------------------------------

def test_a_rung_is_judged_against_the_announced_count():
    # The design flaw the corpus exposed. Education on the wrapped shape
    # yielded exactly one entry of an announced four, which counted as success
    # under "the first rung with any entry wins", so the next rung never ran.
    #
    # That particular capture is now read correctly by the walk itself, so this
    # asserts the rule rather than the symptom: a short read is not sufficient,
    # and the same read is sufficient when nothing was announced.
    assert _sufficient([{"a": 1}], None) is True
    assert _sufficient([{"a": 1}], 1) is True
    assert _sufficient([{"a": 1}], 4) is False
    assert _sufficient([], None) is False
    # More than announced is still sufficient: LinkedIn's total is a floor we
    # hold it to, not a cap we enforce against it.
    assert _sufficient([{"a": 1}] * 5, 4) is True


def test_no_stored_capture_needs_the_sweep_any_more():
    """The wrapped shape used to reach experience only through the sweep.

    Once the batch-aware descent became the first rung it reads on the
    primary walk, and every capture in this corpus does. Asserted rather
    than left implicit, because the sweep going quiet is the signal that
    the rung above it started doing its job -- and if a future change puts
    a section back on the sweep, that is worth seeing."""
    for name, url in (("afreen-flat", AFREEN), ("afreen-wrapped", AFREEN),
                      ("tushar-wrapped", TUSHAR), ("gulati-wrapped", GULATI)):
        notes = [w for w in parsed(name, url)["warnings"]
                 if w["code"] == "strategy_fallback"]
        assert notes == [], name


def test_the_sweep_still_reads_a_section_the_walk_cannot():
    """D1's mechanism, kept under test now that no fixture exercises it.

    Deleting it with its last example would leave the rung beneath the walk
    unguarded, which is exactly the section that goes unnoticed."""
    from career_lens.linkedin.parse.extractors import _swept_entries

    document = parse_document("""
      <div id="ExperienceDetailsSection"><div><h2>Experience</h2></div>
        <div><div>
          <div><p>Engineer</p><a href="/company/acme/">Acme</a>
            <p>2020 - 2022</p></div>
          <div><p>Architect</p><a href="/company/globex/">Globex</a>
            <p>2022 - 2024</p></div>
        </div></div>
      </div>
    """)

    swept = _swept_entries("experience", document,
                           f"{AFREEN}details/experience/")

    assert [entry["title"] for entry in swept] == ["Engineer", "Architect"]


def test_a_section_the_walk_reads_correctly_never_reaches_the_sweep():
    # The order test: the flat shape parses positionally, so no rung below the
    # first may run. This is what stops the cascade silently migrating to the
    # sweep everywhere, where its looser matching would go unnoticed.
    notes = [
        warning for warning in parsed("afreen-flat", AFREEN)["warnings"]
        if warning["code"] == "strategy_fallback"
    ]

    assert notes == []


# --- a clean read is recorded, not destroyed --------------------------------

def test_a_field_absent_from_a_whole_section_is_noted_not_enforced():
    document = parsed("rupinder-noproficiency", RUPINDER)

    assert [w for w in document["warnings"]
            if w["code"] == "field_absent" and w["section"] == "languages"]
    assert len(document["profile"]["languages"]) == 3


# --- entries versus one entry's fields --------------------------------------
#
# The descent that recovers the wrapped shape can also go one level too far,
# and both mistakes look identical to a count. These pin the discriminator;
# each of them broke a real test before it existed.

def test_a_lone_entry_is_not_split_into_its_own_fields():
    # "French" and "Professional working proficiency" are one language, not
    # two. Both are leaves, which is what separates a field from an entry.
    document = parse_document("""
      <html><body><main><section><div><div>
        <div><p>Languages</p></div>
        <div><p>French</p><p>Professional working proficiency</p></div>
      </div></div></section></main></body></html>
    """)

    read = extract_details("languages", document, f"{ZAHEER}details/languages/")

    assert read == [{"name": "French",
                     "proficiency": "Professional working proficiency"}]


def test_a_grouped_entry_is_not_split_from_its_role_list():
    # A grouped experience is a <div> naming the employer beside a <ul> of
    # roles. Descending turns one entry into two, the second having no employer
    # on it at all -- and unlike a short read, nothing downstream could tell.
    from career_lens.linkedin.parse.details import looks_like_entries
    from lxml.html import fragment_fromstring

    grouped = fragment_fromstring("""
      <div>
        <div><a href="/company/1/">Google</a><p>4 yrs 3 mos</p></div>
        <ul><li><p>Manager</p><p>Jun 2026 - Present</p></li></ul>
      </div>
    """)

    assert looks_like_entries([child for child in grouped]) is False


def test_sibling_entries_of_one_shape_are_recognised():
    from career_lens.linkedin.parse.details import looks_like_entries
    from lxml.html import fragment_fromstring

    level = fragment_fromstring("""
      <div>
        <div><p>English</p><p>Professional</p></div>
        <div><p>Japanese</p><p>Professional</p></div>
        <div><p>Urdu</p><p>Native</p></div>
      </div>
    """)

    assert looks_like_entries([child for child in level]) is True


def test_one_candidate_is_never_a_repeated_level():
    from career_lens.linkedin.parse.details import looks_like_entries
    from lxml.html import fragment_fromstring

    single = fragment_fromstring("<div><div><p>Only</p><p>one</p></div></div>")

    assert looks_like_entries([child for child in single]) is False


# --- lazily-loaded batches --------------------------------------------------
#
# Once the capture layer started loading the whole list, the entries stopped
# arriving as one level: each batch is appended inside its own wrapper, so a
# section of 22 publications came back as three wrappers holding 10, 10 and 2.
# The capture was right and the walk read three entries.

BATCHED = "tushar-batched"


def test_every_entry_of_a_batched_list_is_read():
    read = extract_details(
        "publications", section(BATCHED, "publications"),
        f"{TUSHAR}details/publications/")

    assert len(read) == 22


def test_a_batched_list_keeps_its_order():
    read = extract_details(
        "publications", section(BATCHED, "publications"),
        f"{TUSHAR}details/publications/")

    assert read[0]["name"].startswith("Deep Learning-based Cyber Security")
    # The first entry of the second batch, which must follow the first batch's
    # last rather than being interleaved.
    assert read[10]["name"].startswith("Fuzzy logic-based elasticity")


def test_a_batch_and_a_single_entry_are_told_apart():
    from career_lens.linkedin.parse.details import (
        descend, looks_like_entries, text_bearing)

    document = section(BATCHED, "publications")
    batches = entry_elements(document, "publications")

    # Whatever the walk lands on, expanding must not split a real entry: an
    # entry descends to one child, a batch to many of the same shape.
    for batch in batches:
        inside = text_bearing(descend(batch))
        for entry in inside:
            assert looks_like_entries(text_bearing(descend(entry))) is False


RAUL = "https://www.linkedin.com/in/raulsanz/"
RAUL_BATCHED = "raulsanz-batched"


def test_a_batched_skills_page_reads_every_skill():
    """Skills was the one details section still on the pre-batching walk.

    The same profile read 39 skills an hour earlier, flat. LinkedIn then
    served the batched shape -- four wrappers of ten -- and the old walk
    descended into the page header, found a back button and the word
    "Skills", and reported the section as read-but-empty.
    """
    read = extract_section(
        "skills", section(RAUL_BATCHED, "skills"),
        f"{RAUL}details/skills/")[0]

    assert len(read) == 39
    assert read[0]["name"] == "IT Management"


def test_a_batched_skills_page_is_not_reported_as_read():
    """`fallback` with nothing in it satisfies a freshness check and shadows
    the good older reading the walk would otherwise have kept."""
    _, _, status = extract_section(
        "skills", section(RAUL_BATCHED, "skills"),
        f"{RAUL}details/skills/")

    assert status == "parsed"


def test_a_batched_experience_page_reads_on_the_primary_walk():
    """Eight of fifteen stored experience captures come back empty from the
    positional walk. They read today only because the signature sweep
    rescues them -- correct entries, reached by the recovery path, with a
    `strategy_fallback` warning on every run. The primary walk should find
    them."""
    from career_lens.linkedin.parse.extractors import _entry_elements

    document = section(RAUL_BATCHED, "experience")

    assert len(_entry_elements(document, section_name="experience")) == 6


def test_a_batched_experience_page_needs_no_recovery_warning():
    read = extract_section(
        "experience", section(RAUL_BATCHED, "experience"),
        f"{RAUL}details/experience/")

    assert len(read[0]) == 6
    assert [w["code"] for w in read[1]] == []


def test_expanding_batches_runs_after_descending_not_before():
    # A section wrapped once and batched zero times descends to its entries.
    # Flattening first stops at the first level holding two of anything, which
    # read afreen's five certifications as two.
    read = extract_details(
        "certifications", section("afreen-wrapped", "certifications"),
        f"{AFREEN}details/certifications/")

    assert len(read) == 5


# --- defect 6: a school employer's numeric id ------------------------------

@pytest.mark.parametrize("variant,url", [
    ("afreen-flat", AFREEN),
    ("afreen-wrapped", AFREEN),
    ("gulati-flat", GULATI),
    ("gulati-wrapped", GULATI),
])
def test_a_school_employer_keeps_its_numeric_id(variant, url):
    """A university is an employer like any other.

    LinkedIn serves one under `/school/<id>/` rather than `/company/<id>/`,
    and it is the same numeric entity id: stable across a rename, which is
    the whole reason the id is preferred to the slug. Dropping it leaves the
    employer keyed on a name.
    """
    entries = parsed(variant, url)["profile"]["experiences"]
    schools = [
        entry for entry in entries
        if "/school/" in (entry.get("url") or "")
    ]

    assert schools, f"{variant} has no school-hosted employer to check"
    for entry in schools:
        assert entry["company_id"], (variant, entry["name"], entry["url"])


def test_a_section_nobody_asked_for_is_not_held_to_its_announced_count():
    """A count is a promise about a capture, and there is no capture here.

    A selective request captures only the sections it names, but the profile
    page still announces every section's total. Reading "Show all 14 courses"
    and then holding an uncaptured section to it failed three live attempts of
    a request that asked for education and publications: courses was never
    fetched, so the contract compared 14 against nothing.
    """
    profile = (
        '<html><body><main><h1>Raul</h1>'
        '<a href="/in/raulsanz/details/courses/">Show all 14 courses</a>'
        '</main></body></html>'
    )

    parsed = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/raulsanz/",
        requested_sections=frozenset({"education"}),
    )

    assert parsed["section_status"]["courses"] == "not_requested"
    assert parsed["drift"] == []


@pytest.mark.parametrize("requested", sorted(PROFILE_COLLECTION))
def test_selecting_one_section_holds_only_that_one_to_its_count(requested):
    """Every announced section, asked for one at a time.

    The count contract and selective capture were built on separate branches
    and met in a merge, where nothing exercised them together: a request for
    one section was failed by the announcement of another. This walks the whole
    cross-product the merge left untested, so a status added later cannot slip
    past the check the way `not_requested` did.
    """
    files = load("afreen-flat")
    profile = files.pop("profile")
    kept = {name: html for name, html in files.items() if name == requested}

    document = parse_profile_html(
        profile, kept, profile_url=AFREEN,
        section_urls={n: f"{AFREEN}details/{n}/" for n in kept},
        requested_sections=frozenset({requested}),
    )

    assert [entry["section"] for entry in document["drift"]] == []
    for name in PROFILE_COLLECTION:
        if name != requested:
            assert document["section_status"][name] == "not_requested"


# --- what the profile page proves it does not have --------------------------

def test_the_profile_page_names_every_section_it_links_into():
    """`announced_counts` walks these same anchors and keeps only the ones
    stating a number. The names are what say a section exists at all."""
    linked = linked_sections(parse_document(load("afreen-flat")["profile"]))

    assert linked == {"certifications", "experience", "featured", "honors",
                      "interests", "organizations", "skills"}


def test_a_section_this_member_does_not_have_is_simply_not_linked():
    """Afreen has no patents. There is no patents link to find, which is the
    whole evidence -- LinkedIn does not render an empty section."""
    linked = linked_sections(parse_document(load("afreen-flat")["profile"]))

    assert "patents" not in linked
    assert "publications" not in linked


def test_two_profiles_differ_in_what_they_link():
    """Guards against a rule that would pass by returning a constant."""
    afreen = linked_sections(parse_document(load("afreen-flat")["profile"]))
    gulati = linked_sections(parse_document(load("gulati-flat")["profile"]))

    assert "certifications" in afreen and "certifications" not in gulati
    assert "education" in gulati and "education" not in afreen


def test_a_page_with_no_section_links_at_all_names_nothing():
    """The corroboration signal: an empty result means we could not read this
    page's links, not that the member has nothing."""
    assert linked_sections(parse_document("<html><body>hi</body></html>")) == set()


def test_a_profile_offering_an_activity_feed_says_so():
    assert offers_activity(parse_document(load("afreen-flat")["profile"]))


def test_a_profile_with_no_activity_link_does_not():
    assert not offers_activity(parse_document("<html><body>hi</body></html>"))


# --- absent, not merely uncaptured ------------------------------------------

def test_a_section_the_profile_does_not_link_is_absent_not_uncaptured():
    """The §9 fix. `not_captured` meant both "the run never got it" and "this
    member has no patents", so a full-profile freshness check chased patents
    that will never exist and re-scraped for ever."""
    statuses = parsed("afreen-flat", AFREEN)["section_status"]

    assert statuses["patents"] == "not_available"
    assert statuses["publications"] == "not_available"
    assert statuses["courses"] == "not_available"


def test_a_section_the_profile_does_link_is_never_called_absent():
    """Afreen links certifications, honors and organizations. Whatever
    happened to their captures, the page says they exist."""
    statuses = parsed("afreen-flat", AFREEN)["section_status"]

    for section_name in ("certifications", "honors", "organizations"):
        assert statuses[section_name] != "not_available", section_name


def test_a_services_page_link_proves_services_exist_even_without_a_capture():
    """Services leaves `/in/<handle>/details/`, but it remains presence
    evidence. Losing that at the route boundary falsely called the section
    empty whenever another ordinary details link enabled absence detection.
    """
    profile = """
      <html><body><main><h1>Ada Lovelace</h1>
        <a href="/in/ada/details/experience/">Show all experience</a>
        <a href="/services/page/400a9931403891b076/">Show all services</a>
      </main></body></html>
    """

    result = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/ada/")

    assert "services" in linked_sections(parse_document(profile))
    assert result["section_status"]["services"] == "not_captured"


def test_absence_is_read_per_profile_not_from_a_table():
    """Gulati has languages and projects and no certifications; afreen is the
    other way round. A rule keyed on anything but the page would invert one."""
    afreen = parsed("afreen-flat", AFREEN)["section_status"]
    gulati = parsed("gulati-flat", GULATI)["section_status"]

    assert afreen["certifications"] != "not_available"
    assert gulati["certifications"] == "not_available"


def test_a_profile_whose_links_we_cannot_read_reports_nothing_absent():
    """The corroboration gate. Calling every section empty on a page we could
    not read would report a whole profile as successfully read, and freshness
    would then be satisfied by nothing at all -- worse than the bug fixed
    here, because it is silent."""
    document = parse_profile_html(
        "<html><body><h1>Ada</h1></body></html>", {},
        profile_url="https://www.linkedin.com/in/ada/")

    assert "not_available" not in set(document["section_status"].values())


def test_a_complete_sparse_profile_reports_unoffered_sections_absent():
    """A base capture that reached the bottom is evidence even when a sparse
    member offers no details links at all. The parser must distinguish that
    complete page from the unread fragment guarded by the test above.
    """
    document = parse_profile_html(
        "<html><body><main><h1>Ada</h1></main></body></html>",
        {},
        profile_url="https://www.linkedin.com/in/ada/",
        profile_capture={"scroll_stopped": "bottom"},
        requested_sections=frozenset({"experience", "education", "skills"}),
    )

    assert document["section_status"]["experience"] == "not_available"
    assert document["section_status"]["education"] == "not_available"
    assert document["section_status"]["skills"] == "not_available"


def test_multiple_empty_profile_containers_after_scroll_are_parser_drift():
    profile = """
      <html><body><main>
        <h1>Ada Lovelace</h1>
        <div data-sdui-screen="com.linkedin.sdui.profile.Profile">
          <div data-testid="lazy-column">
            <div componentkey="profileCardsExperience"></div>
            <div componentkey="profileCardsEducation"></div>
            <div componentkey="profileCardsSkills"></div>
          </div>
        </div>
      </main></body></html>
    """

    document = parse_profile_html(
        profile,
        {},
        profile_url="https://www.linkedin.com/in/ada/",
        profile_capture={"scroll_stopped": "bottom"},
        requested_sections=frozenset({"experience", "education", "skills"}),
    )

    assert document["drift"] == [{
        "section": "profile",
        "contract": "profile section containers",
        "expected": "content after the completed profile scroll",
        "found": "3 empty containers",
    }]
    assert document["section_status"]["experience"] == "not_captured"
    assert document["section_status"]["education"] == "not_captured"
    assert document["section_status"]["skills"] == "not_captured"


def test_one_empty_profile_container_can_be_a_sparse_profile():
    profile = """
      <html><body><main>
        <h1>Ada Lovelace</h1>
        <div data-sdui-screen="com.linkedin.sdui.profile.Profile">
          <div data-testid="lazy-column">
            <div componentkey="profileCardsExperience"></div>
          </div>
        </div>
      </main></body></html>
    """

    document = parse_profile_html(
        profile,
        {},
        profile_url="https://www.linkedin.com/in/ada/",
        profile_capture={"scroll_stopped": "bottom"},
        requested_sections=frozenset({"experience"}),
    )

    assert document["drift"] == []
    assert document["section_status"]["experience"] == "not_available"


def test_about_this_member_is_never_absent():
    """Every member has an account history behind that dialog, so a missing
    one means the dialog would not open -- a failure to read, not an absence.
    Marking it empty would satisfy freshness on a section we never got."""
    statuses = parsed("afreen-flat", AFREEN)["section_status"]

    assert statuses["about-this-member"] == "not_captured"


def test_a_profile_without_a_contact_link_reports_contact_info_absent():
    document = parse_profile_html(
        '<html><body><main><a href="/in/ada/details/skills/">'
        "Show all skills</a></main></body></html>",
        {},
        profile_url="https://www.linkedin.com/in/ada/",
        requested_sections=frozenset({"about-this-member", "contact-info"}),
    )

    assert document["section_status"]["contact-info"] == "not_available"
    assert document["section_status"]["about-this-member"] == "not_captured"


def test_a_profile_not_open_to_work_records_a_successful_absence():
    """Most profiles offer no Open to Work card. That is an answer, not a
    failed capture, and must not make an otherwise complete extraction partial.
    """
    document = parse_profile_html(
        '<html><body><main><a href="/in/ada/details/skills/">'
        "Show all skills</a></main></body></html>",
        {}, profile_url="https://www.linkedin.com/in/ada/")

    assert document["section_status"]["open-to-work"] == "not_available"


def test_an_offered_open_to_work_dialog_that_was_missed_remains_uncaptured():
    """A visible trigger proves there was a dialog to read. Calling that
    absent would hide a real interaction failure from extraction quality.
    """
    document = parse_profile_html(
        '<html><body><main><a href="/in/ada/details/skills/">'
        "Show all skills</a><section><h2>Open to work</h2>"
        "<button>Show details</button></section></main></body></html>",
        {}, profile_url="https://www.linkedin.com/in/ada/")

    assert document["section_status"]["open-to-work"] == "not_captured"


def test_an_unrelated_show_details_control_is_not_open_to_work():
    document = parse_profile_html(
        '<html><body><main><a href="/in/ada/details/skills/">'
        "Show all skills</a><section><h2>Suggested for you</h2>"
        "<button>Show details</button></section></main></body></html>",
        {}, profile_url="https://www.linkedin.com/in/ada/",
    )

    assert document["section_status"]["open-to-work"] == "not_available"


def test_activity_is_absent_only_when_the_feed_is_not_linked():
    """Activity does not live under details/, so it needs its own evidence."""
    linked = parsed("afreen-flat", AFREEN)["section_status"]
    unlinked = parse_profile_html(
        '<html><body><a href="/in/ada/details/skills/">Show all</a>'
        "</body></html>", {}, profile_url="https://www.linkedin.com/in/ada/")

    assert linked["activity-posts"] != "not_available"
    assert unlinked["section_status"]["activity-posts"] == "not_available"


def test_an_inline_card_is_read_before_absence_is_concluded():
    """Tushar's causes are on the profile page, not behind a link -- LinkedIn
    offers no causes page at all. Concluding absence before the inline pass
    would mark a section empty while its content sat in the document."""
    document = parsed("tushar-flat", TUSHAR)

    assert document["section_status"]["causes"] == "parsed_inline"
    assert document["profile"]["causes"]


def test_a_section_with_no_link_but_an_inline_card_is_not_absent():
    """Gulati carries no recommendations link and has recommendations anyway:
    a short list is shown inline with no "Show all" to find. Link absence
    alone would have called this empty."""
    document = parsed("gulati-flat", GULATI)

    assert "recommendations" not in linked_sections(
        parse_document(load("gulati-flat")["profile"]))
    assert document["section_status"]["recommendations"] == "parsed_inline"
    assert document["profile"]["recommendations"]["received"]


# --- a tab the feed does not offer ------------------------------------------

STRIP = """
<html><body><main>
  <div role="group">
    <button>Posts</button><button>Comments</button>
    <button>Images</button><button>Reactions</button>
  </div>
  <div><button>Follow</button><button>Like</button></div>
</main></body></html>
"""


def test_the_activity_strip_names_the_tabs_the_feed_offers():
    """LinkedIn builds the strip from what the member has actually produced,
    so a member who has published no articles has no Articles tab. Raul's
    real feed offers Images where another member's offers Articles."""
    from career_lens.linkedin.parse.cascade import activity_tabs

    assert activity_tabs(parse_document(STRIP)) == {
        "posts", "comments", "images", "reactions"}


def test_the_strip_is_read_from_its_group_not_the_whole_feed():
    """Every post carries Like and Follow buttons. Reading those as tabs
    would report every profile as offering every tab, which never concludes
    an absence and would make this evidence worthless."""
    from career_lens.linkedin.parse.cascade import activity_tabs

    assert "follow" not in activity_tabs(parse_document(STRIP))


def test_a_feed_whose_strip_will_not_read_names_no_tabs():
    """The corroboration signal, same as `linked_sections`: empty means we
    could not read the strip, not that the member has no tabs."""
    from career_lens.linkedin.parse.cascade import activity_tabs

    assert activity_tabs(parse_document("<html><body>hi</body></html>")) == set()


def test_a_tab_the_feed_does_not_offer_is_absent_not_uncaptured():
    """The last section that kept a full-profile freshness check unsatisfiable
    for Raul: the feed exists, so the feed link proves nothing about Articles,
    and the strip is the only thing that does."""
    profile = (
        '<html><body><main><a href="/in/raul/details/skills/">Show all</a>'
        '<a href="/in/raul/recent-activity/all/">Show all activity</a>'
        "</main></body></html>")
    document = parse_profile_html(
        profile, {"activity-posts": STRIP},
        profile_url="https://www.linkedin.com/in/raul/",
        section_urls={"activity-posts":
                      "https://www.linkedin.com/in/raul/recent-activity/all/"})

    assert document["section_status"]["activity-articles"] == "not_available"


def test_a_tab_the_feed_does_offer_is_never_called_absent():
    profile = (
        '<html><body><main><a href="/in/raul/details/skills/">Show all</a>'
        '<a href="/in/raul/recent-activity/all/">Show all activity</a>'
        "</main></body></html>")
    document = parse_profile_html(
        profile, {"activity-posts": STRIP},
        profile_url="https://www.linkedin.com/in/raul/",
        section_urls={"activity-posts":
                      "https://www.linkedin.com/in/raul/recent-activity/all/"})

    assert document["section_status"]["activity-comments"] != "not_available"


def test_no_strip_means_no_conclusion_about_any_tab():
    """Without the landing capture there is no strip, and the feed link says
    only that a feed exists. Nothing may be concluded from that."""
    profile = (
        '<html><body><main><a href="/in/raul/details/skills/">Show all</a>'
        '<a href="/in/raul/recent-activity/all/">Show all activity</a>'
        "</main></body></html>")
    document = parse_profile_html(
        profile, {}, profile_url="https://www.linkedin.com/in/raul/")

    assert document["section_status"]["activity-articles"] == "not_captured"
