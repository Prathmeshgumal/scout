"""Which images on a profile belong to the profile.

The URLs themselves cannot be stored and read later: LinkedIn signs them with
an expiry a few days out, and every size variant carries its own signature. So
these are found in order to be fetched while the page is still open.

Finding them is the hard half. A person's page carries two dozen member photos
-- everyone in "People also viewed", every author in the activity feed, and the
signed-in operator's own face in the nav -- and the class names are per-build
hashes, so there is nothing to select on. What holds is the structure: the
profile's own photo is the only one inside the card that carries the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml.html import HtmlElement

# The variant segment names the family and the pixel box: a person's photo is
# `profile-displayphoto-shrink_400_400`, framed with a badge it is
# `profile-framedphoto-...`, and the cover behind it is
# `profile-displaybackgroundimage-shrink_350_1400`.
MEDIA = re.compile(
    r"https://media\.licdn\.com/dms/image/v2/(?P<id>[^/]+)/(?P<variant>[^/]+)/")
PERSON_PHOTO = re.compile(r"profile-(displayphoto|framedphoto)")
PERSON_COVER = re.compile(r"profile-displaybackgroundimage")
BOX = re.compile(r"_(\d+)_(\d+)$")
BACKGROUND_URL = re.compile(r"url\(\s*(?P<url>[^)]+?)\s*\)")


@dataclass(frozen=True)
class ImageTargets:
    """The two images worth keeping, as URLs to fetch now."""

    profile_image: str | None = None
    cover_image: str | None = None


def media_id(url: str) -> str | None:
    found = MEDIA.search(url)
    return found.group("id") if found else None


def variant_of(url: str) -> str | None:
    found = MEDIA.search(url)
    return found.group("variant") if found else None


def pixels(url: str) -> int:
    """The variant's pixel count, for choosing between sizes of one image."""
    variant = variant_of(url) or ""
    box = BOX.search(variant)
    return int(box.group(1)) * int(box.group(2)) if box else 0


def largest(url: str, document: HtmlElement) -> str:
    """The biggest URL the page carries for the same image.

    A top card links its photo at 200x200 while the same photo sits at 800x800
    elsewhere in the document. Each variant is signed separately, so the size
    cannot be raised by rewriting the path -- only by finding the URL that is
    already there.
    """
    identifier = media_id(url)
    if identifier is None:
        return url
    return max(
        (candidate for candidate in _all_media(document)
         if media_id(candidate) == identifier),
        key=pixels, default=url)


def _all_media(document: HtmlElement) -> set[str]:
    """Every LinkedIn media URL in the document, from wherever it is written."""
    found = {value for value in document.xpath("//@src") if MEDIA.search(value)}
    for srcset in document.xpath("//@srcset"):
        found |= _srcset(srcset)
    for style in document.xpath("//@style"):
        found |= {match.group("url").strip("'\"")
                  for match in BACKGROUND_URL.finditer(style)
                  if MEDIA.search(match.group("url"))}
    return found


def _srcset(value: str) -> set[str]:
    return {part.strip().split(" ")[0] for part in value.split(",")
            if MEDIA.search(part)}


def person_images(document: HtmlElement,
                  heading: HtmlElement | None) -> ImageTargets:
    """The profile owner's photo and cover, anchored on the name heading.

    `heading` is the element naming the person, from the same reading the rest
    of the profile is extracted with. Without it there is no way to tell the
    owner's photo from the two dozen other faces on the page, so nothing is
    returned rather than a guess.
    """
    if heading is None:
        return ImageTargets()

    # Outwards from the name to the first container holding exactly one member
    # photo. One is the whole test: the card around the name holds the owner's
    # face and nobody else's, while any ancestor wide enough to reach the feed
    # or the sidebar holds many.
    for ancestor in heading.iterancestors():
        photos = [value for value in ancestor.xpath(".//img/@src")
                  if PERSON_PHOTO.search(value)]
        if len(photos) == 1:
            return ImageTargets(
                profile_image=largest(photos[0], document),
                cover_image=_person_cover(ancestor, document))
        if len(photos) > 1:
            break
    return ImageTargets(cover_image=_person_cover_anywhere(document))


def _person_cover(card: HtmlElement, document: HtmlElement) -> str | None:
    """The cover, which is a background on an element rather than an `img`.

    A profile with no cover still has the element, carrying
    `background-image: none`. That is an ordinary profile, not a failure.
    """
    for style in card.xpath(".//@style"):
        for match in BACKGROUND_URL.finditer(style):
            url = match.group("url").strip("'\"")
            if PERSON_COVER.search(url):
                return largest(url, document)
    return _person_cover_anywhere(document)


def _person_cover_anywhere(document: HtmlElement) -> str | None:
    """A cover needs no anchor, because only the owner has one on their page.

    Member photos are everywhere -- one page carries three faces for every
    cover -- but a cover belongs to whoever the page is about. Two distinct
    ones would mean that is no longer true, so nothing is returned rather than
    a coin toss.
    """
    covers = [value for value in _all_media(document) if PERSON_COVER.search(value)]
    if len({media_id(value) for value in covers}) > 1:
        return None
    return max(covers, key=pixels) if covers else None


def company_images(company: dict[str, object],
                   document: HtmlElement) -> ImageTargets:
    """The logo and cover the company parser already found, at full size.

    A company page keeps its semantic class names, so the parser reads both
    reliably from the top card and there is nothing to rediscover here.
    """
    logo = company.get("logo_url")
    cover = company.get("cover_image_url")
    return ImageTargets(
        profile_image=largest(logo, document) if isinstance(logo, str) else None,
        cover_image=largest(cover, document) if isinstance(cover, str) else None)
