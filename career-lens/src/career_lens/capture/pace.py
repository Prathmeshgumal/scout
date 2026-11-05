"""How long to wait, and when to stop.

Pacing matters more than any stealth library here. An authenticated session is
not identified by its fingerprint -- the site knows exactly who it is -- so what
marks it as automated is rate and shape: thirty page views in an afternoon is
not a human no matter how naturally the query was typed.

Hence delays, below, which make a session look unhurried, and `SessionCap`,
which stops a single invocation from getting away from you. Both are blunt
and both forget everything the moment the process exits -- which is fine for
shape, but not for the thing that actually keeps an account alive: a limit
that holds across every run an identity ever makes. Those limits are counted
from the event journal instead, in `budget.py`, precisely so they survive the
process ending.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

from . import trace
from .options import TEMPO_NAMES

# Reading speed for dwell time. Deliberately fast -- someone skimming a page for
# one fact is not reading every word -- but scaled to page length so a stub and
# a long article are not dwelt on identically.
CHARS_PER_SECOND = 900
# Scrolling already provides reading time; avoid a long idle pause after capture.
DWELL_BOUNDS = (2.5, 12.0)
# A pause on one visible feed item is a skim, not a second page dwell.
READ_BOUNDS = (3.0, 9.0)


@dataclass(frozen=True)
class Tempo:
    """Named delay profiles. `fast` exists for development runs only."""

    name: str
    between_queries: tuple[float, float]
    between_results: tuple[float, float]
    dwell_scale: float
    # Between one flick of the wheel and the next. Scrolling was the one part of
    # a visit that ran at the same speed whatever tempo was asked for, so
    # 'human' and 'fast' went down a page identically.
    scroll_pause: tuple[float, float] = (0.04, 0.12)

    def scroll_gap(self) -> float:
        low, high = self.scroll_pause
        return random.triangular(low, high, low + (high - low) * 0.4)

    def result_gap(self) -> float:
        return random.uniform(*self.between_results)

    def dwell(self, text_chars: int) -> float:
        seconds = (text_chars / CHARS_PER_SECOND) * self.dwell_scale
        seconds *= random.uniform(0.7, 1.4)
        return min(max(seconds, DWELL_BOUNDS[0] * self.dwell_scale), DWELL_BOUNDS[1])

    def read_pause(self, text_chars: int) -> float:
        """A short, text-aware pause while one visible feed item is read."""
        scale = max(0.2, self.dwell_scale)
        low, high = READ_BOUNDS[0] * scale, READ_BOUNDS[1] * scale
        centre = min(max(text_chars / CHARS_PER_SECOND * self.dwell_scale,
                         low), high)
        return random.triangular(
            max(low, centre * 0.75), min(high, centre * 1.25), centre)


TEMPOS = {
    # For multi-day work against a target that counts, principally LinkedIn.
    "human": Tempo("human", between_queries=(60, 180), between_results=(8, 25), dwell_scale=1.0,
                   scroll_pause=(0.5, 1.2)),
    # The default. Someone researching one company runs their six searches in a
    # couple of minutes, not half an hour -- long gaps between related queries
    # are not more human, just slower.
    "research": Tempo("research", between_queries=(8, 20), between_results=(3, 8),
                      dwell_scale=0.35, scroll_pause=(0.04, 0.12)),
    "brisk": Tempo("brisk", between_queries=(25, 60), between_results=(4, 12), dwell_scale=0.5,
                   scroll_pause=(0.15, 0.4)),
    "fast": Tempo("fast", between_queries=(2, 5), between_results=(1, 3), dwell_scale=0.1,
                  scroll_pause=(0.02, 0.06)),
}

assert tuple(TEMPOS) == TEMPO_NAMES


def sleep_with_reason(seconds: float, reason: str, *, quiet: bool = False,
                      name: str = "wait", detail: str = "") -> None:
    # The timing line says everything this print did and more, so printing both
    # would double every wait in the console.
    if not quiet and not trace.is_active():
        print(f"  … {seconds:.1f}s ({reason})", flush=True)
    with trace.phase(trace.WAIT, name, detail or reason):
        time.sleep(seconds)
