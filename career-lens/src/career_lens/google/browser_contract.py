"""Google browser definitions shared by native and server-directed recipes.

No browser imports: execution mechanics belong to each transport, while the
selectors and behavioral limits must not acquire independent defaults.
"""

GOOGLE_HOME = "https://www.google.com/"
SEARCH_BOX = "textarea[name='q']"
RESULT_LINKS = "#search a[href]:has(h3)"
RESULT_HEADING = "h3"
SNIPPET_CONTAINER = "div[data-hveid]"
# Google can show this empty-organic-results illustration below an AI overview
# without rendering any of its usual no-results messages.
EMPTY_RESULTS_IMAGE_LABEL = "An image of a yeti fishing for something in the ice"
EMPTY_RESULTS_IMAGE = f'[role="img"][aria-label="{EMPTY_RESULTS_IMAGE_LABEL}"]'
NAV_TIMEOUT_MS = 30_000
RESULT_TIMEOUT_MS = 8_000
# A search-only run resolves opaque `/goto` links among this many leading
# ranks: a full first page. A link left unresolved has no destination a caller
# can use, so a lower bound quietly shortens the page. One attempt of at most
# three seconds each keeps the added latency bounded.
REDIRECT_RESOLUTIONS = 10
