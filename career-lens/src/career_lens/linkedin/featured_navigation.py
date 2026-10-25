"""Shared bounds and controls for revealing the Featured detail link."""

from collections.abc import Mapping

MAX_FEATURED_ADVANCES = 12
FEATURED_NEXT = (
    "button[aria-label*='next' i]:not([disabled]):not([aria-disabled='true']), "
    "button[aria-label*='forward' i]:not([disabled]):not([aria-disabled='true'])"
)


def horizontally_clipped(link: object, card: object) -> bool:
    if not isinstance(link, Mapping) or not isinstance(card, Mapping):
        return False
    try:
        x, width = float(link["x"]), float(link["width"])
        left, span = float(card["x"]), float(card["width"])
    except (KeyError, TypeError, ValueError):
        return False
    return width > 0 and span > 0 and (x < left or x + width > left + span)
