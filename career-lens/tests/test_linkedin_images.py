"""Which image on a profile belongs to the profile.

The stakes are the reason these are exhaustive: a person's page carries two
dozen member photos, and storing the wrong one files a stranger's face under
somebody's name. So the rule is that an unidentifiable photo yields nothing.
"""

from __future__ import annotations

from lxml import html

from career_lens.linkedin.parse.dom import primary_content
from career_lens.linkedin.parse.extractors import _profile_heading
from career_lens.linkedin.parse.images import (
    company_images, largest, media_id, person_images, pixels, variant_of,
)

AFREEN = "tests/fixtures/profiles/afreen-munshi/profile.html"
TUSHAR = "tests/fixtures/drift/tushar-flat/profile.html"


def document(path: str):
    return html.parse(path).getroot()


def targets(path: str, handle: str):
    doc = document(path)
    return person_images(doc, _profile_heading(primary_content(doc), handle))


def page(body: str):
    return html.fromstring(f"<html><body>{body}</body></html>")


def media(identifier: str, variant: str) -> str:
    return (f"https://media.licdn.com/dms/image/v2/{identifier}/{variant}"
            f"/0/1631971551199?e=1787788800&v=beta&t=signature")


# ---- reading a URL ---------------------------------------------------------

def test_the_media_id_is_what_two_sizes_of_one_image_share():
    small = media("D4D35AQEFwVwFzFUAvw", "profile-displayphoto-shrink_100_100")
    large = media("D4D35AQEFwVwFzFUAvw", "profile-displayphoto-shrink_800_800")

    assert media_id(small) == media_id(large) == "D4D35AQEFwVwFzFUAvw"


def test_a_url_that_is_not_linkedin_media_has_no_id():
    assert media_id("https://example.com/face.jpg") is None


def test_the_variant_carries_the_pixel_box():
    assert pixels(media("x", "profile-displayphoto-shrink_400_400")) == 160000
    assert pixels(media("x", "profile-displaybackgroundimage-shrink_350_1400")) == 490000


def test_a_variant_with_no_box_counts_as_no_pixels():
    # `company-logo` sometimes appears without one, and it must not sort above
    # a variant whose size is known.
    assert pixels(media("x", "company-logo")) == 0


# ---- choosing between sizes -------------------------------------------------

def test_the_largest_url_the_page_carries_is_chosen():
    """A top card links 200x200 while 800x800 sits elsewhere in the document."""
    small = media("same", "profile-displayphoto-shrink_200_200")
    large = media("same", "profile-displayphoto-shrink_800_800")
    document = page(f'<img src="{small}"><img src="{large}">')

    assert largest(small, document) == large


def test_a_bigger_variant_of_a_different_image_is_not_substituted():
    mine = media("mine", "profile-displayphoto-shrink_200_200")
    theirs = media("theirs", "profile-displayphoto-shrink_800_800")

    assert largest(mine, page(f'<img src="{mine}"><img src="{theirs}">')) == mine


def test_a_url_only_reachable_through_srcset_still_counts():
    small = media("same", "profile-displayphoto-shrink_100_100")
    large = media("same", "profile-displayphoto-shrink_800_800")
    document = page(f'<img src="{small}" srcset="{large} 2x">')

    assert largest(small, document) == large


def test_the_url_is_returned_unchanged_when_nothing_larger_exists():
    only = media("same", "profile-displayphoto-shrink_100_100")

    assert largest(only, page(f'<img src="{only}">')) == only


# ---- the owner's photo, among everyone else's ------------------------------

def test_the_owner_photo_is_read_off_the_card_holding_the_name():
    found = targets(AFREEN, "afreen-munshi")

    assert variant_of(found.profile_image) == "profile-framedphoto-shrink_800_800"


def test_the_photo_chosen_is_the_one_the_top_card_shows():
    """Not merely a large photo: the same image the card links, upgraded."""
    doc = document(AFREEN)
    heading = _profile_heading(primary_content(doc), "afreen-munshi")
    card = next(ancestor for ancestor in heading.iterancestors()
                if len([value for value in ancestor.xpath(".//img/@src")
                        if "profile-framedphoto" in value
                        or "profile-displayphoto" in value]) == 1)
    shown = next(value for value in card.xpath(".//img/@src")
                 if "profile-framedphoto" in value)

    found = person_images(doc, heading)

    assert media_id(found.profile_image) == media_id(shown)


def test_nothing_is_returned_when_the_owner_photo_cannot_be_told_apart():
    """This fixture carries three faces and none of them is the owner's.

    Refusing is the whole point: any answer here would be a stranger filed
    under this person's name.
    """
    found = targets(TUSHAR, "tushar-flat")

    assert found.profile_image is None


def test_a_page_with_no_heading_yields_nothing():
    assert person_images(page("<img src='x'>"), None).profile_image is None


def test_the_operator_own_photo_in_the_navigation_is_not_the_profile():
    """The signed-in account's face is on every page LinkedIn serves."""
    operator = media("operator", "profile-displayphoto-shrink_100_100")
    owner = media("owner", "profile-displayphoto-shrink_400_400")
    document = page(
        f'<nav><img src="{operator}"></nav>'
        f'<main><div><h1>Someone</h1><img src="{owner}"></div></main>')
    heading = document.xpath("//h1")[0]

    found = person_images(document, heading)

    assert media_id(found.profile_image) == "owner"


# ---- the cover -------------------------------------------------------------

def test_the_cover_is_found_though_it_is_a_background_rather_than_an_image():
    found = targets(TUSHAR, "tushar-flat")

    assert variant_of(found.cover_image) == (
        "profile-displaybackgroundimage-shrink_350_1400")


def test_a_profile_with_no_cover_is_ordinary_rather_than_a_failure():
    # This one renders `background-image: none`, which is what having no cover
    # looks like in the DOM.
    assert targets(AFREEN, "afreen-munshi").cover_image is None


def test_two_different_covers_mean_neither_can_be_claimed():
    first = media("one", "profile-displaybackgroundimage-shrink_350_1400")
    second = media("two", "profile-displaybackgroundimage-shrink_350_1400")
    document = page(
        f'<div style="background-image: url({first})"></div>'
        f'<div style="background-image: url({second})"></div>')

    assert person_images(document, None).cover_image is None


# ---- a company, whose parser already found both ----------------------------

def test_a_company_logo_and_cover_are_taken_at_their_largest():
    small = media("logo", "company-logo_100_100")
    large = media("logo", "company-logo_400_400")
    cover = media("cover", "company-background_10000_2000")
    document = page(f'<img src="{small}"><img src="{large}"><img src="{cover}">')

    found = company_images(
        {"logo_url": small, "cover_image_url": cover}, document)

    assert found.profile_image == large
    assert found.cover_image == cover


def test_a_company_with_neither_image_yields_neither():
    found = company_images({"logo_url": None, "cover_image_url": None}, page(""))

    assert found == company_images({}, page(""))
    assert found.profile_image is None and found.cover_image is None
