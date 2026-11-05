"""Shared selectors for profile sections that have not rendered yet."""

# SDUI loading cards contain only divs, including repeated empty bars. Do not
# mistake a card with headings, images, controls or inline text markup for one
# of these placeholders, or match a lone layout spacer. No generated classes.
PROFILE_LOADING_SELECTOR = (
    "main section:has(div:empty ~ div:empty):not(:has(:not(div)))"
)
PROFILE_LOADING_PASSES = 2
PROFILE_LOADING_WAIT_MS = 10_000
