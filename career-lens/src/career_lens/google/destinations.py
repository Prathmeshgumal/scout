"""Destinations a search run records but does not open.

Google answers many queries with social and user-generated-content sites --
Facebook and Instagram profiles, YouTube channels, TikTok and Snapchat pages,
Pinterest boards, Quora threads. Opening those returns either a login wall or
somebody's personal page, neither of which is what a search is being run for. They are still real results, so the run keeps the rank, the URL,
the title and the snippet, and says in the result's own selection reason that
the scrape was skipped on purpose.

Skipping still consumes a `top_n` worker. `top_n` is a depth into the ranking
rather than a quota of captures: "the top five results" means the five Google
put first, and a run that skipped ranks 1, 2, 4, 5, 6 and 7 to reach a fifth
capture at rank 8 has read the top eight, whatever it was asked for.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


#: The brands whose destinations are recorded rather than opened. Matched as a
#: label of the registrable domain, so `www.instagram.com` and `es.quora.com`
#: are both excluded and `instagram.example.com` is not.
EXCLUDED_BRANDS = (
    "facebook",
    "fb",          # fb.com and fb.watch
    "instagram",
    "youtube",
    "youtu",       # youtu.be
    "tiktok",
    "snapchat",
    "pinterest",
    "quora",
)

#: A brand followed by a public suffix and nothing else -- `.com`, `.be`, but
#: also `.co.uk` and `.com.au`, which these sites all publish under. Anything
#: further to the right of the brand is a different site that merely mentions
#: it, which is why the suffix is spelled out rather than left open.
_EXCLUDED_HOST = re.compile(
    r"^(?:[a-z0-9-]+\.)*(?:" + "|".join(EXCLUDED_BRANDS) + r")"
    r"\.(?:[a-z]{2,}|[a-z]{2,3}\.[a-z]{2})$"
)

#: Why such a result carries no capture. Part of the stored contract.
EXCLUDED_REASON = "excluded_host"


def is_excluded(url: str) -> bool:
    """Whether this destination is recorded without being opened."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and _EXCLUDED_HOST.match(host) is not None


