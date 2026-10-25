"""Occasionally reach a LinkedIn profile through an exact Google result.

LinkedIn's half of the referral mechanism: what a LinkedIn target is, what a
person would type to find it, and which results are that same profile. The
search itself, the SERP capture and every way the detour gives up live in
`referral.py`.

The detour is optional: any Google failure falls back to the requested URL.
When a search was reached, its SERP capture remains an observation, but it is
never an input to LinkedIn extraction.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from ..google.browser import SearchResult
# Re-exported, not used here: `session.py` reaches typing through this module
# rather than the Google one, because from its side this is the capture layer's
# keyboard. The redundant alias is how a re-export is spelled so a linter does
# not read it as dead.
from ..google.browser import search as search
from ..google.browser import type_query as type_query
from ..google.browser import type_text as type_text
from . import referral
from .options import DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE

if TYPE_CHECKING:  # `Page` is only ever an annotation here.
    from patchright.sync_api import Page


DEFAULT_GOOGLE_SEARCH_CHANCE = DEFAULT_LINKEDIN_GOOGLE_SEARCH_CHANCE
RESERVED_COMPANIES = frozenset({"setup", "search", "admin", "new"})

#: The attempt type is the shared one. Named here as well because this module's
#: callers and tests have always constructed it by this name, and a second
#: spelling of one type is a second thing to keep in step.
DiscoveryAttempt = referral.ReferralAttempt


@dataclass(frozen=True)
class DiscoveryProfile:
    kind: str
    slug: str

    @property
    def url(self) -> str:
        return f"https://www.linkedin.com/{self.kind}/{self.slug}/"

    @property
    def query(self) -> str:
        name = re.sub(r"[-_]+", " ", self.slug).strip()
        return f"{name} LinkedIn"

    def matches(self, url: str) -> bool:
        return discovery_profile(url) == self


def discovery_profile(url: str) -> DiscoveryProfile | None:
    """Recognise exact person/company roots across LinkedIn country hosts."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    segments = [unquote(segment).strip().lower()
                for segment in parts.path.split("/") if segment.strip()]
    if len(segments) != 2:
        return None
    kind, slug = segments
    if kind not in {"in", "company"} or not slug:
        return None
    if kind == "company" and slug in RESERVED_COMPANIES:
        return None
    return DiscoveryProfile(kind, slug)


def selected_profile(url: str, chance: float, *,
                     rng: Any = random) -> DiscoveryProfile | None:
    profile = discovery_profile(url)
    if profile is None:
        return None
    return profile if referral.selected(chance, rng=rng) else None


def matching_result(profile: DiscoveryProfile | None,
                    results: list[SearchResult]) -> SearchResult | None:
    return referral.matching_result(profile, results)


def discover(page: Page, target: str, out_dir: Path, *,
             chance: float = DEFAULT_GOOGLE_SEARCH_CHANCE,
             rng: Any = random,
             before_search=None) -> DiscoveryAttempt | None:
    """Try Google discovery; never make its failure fatal to the target."""
    profile = selected_profile(target, chance, rng=rng)
    if profile is None:
        return None
    return referral.attempt(page, profile, target, out_dir, rng=rng,
                            before_search=before_search)
