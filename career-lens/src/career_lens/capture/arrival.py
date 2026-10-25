"""Pacing that belongs to a page's arrival and its reading, for any site.

Two small behaviours, lifted out of `capture/driver.py` unchanged. Neither has
anything to do with LinkedIn: one gives a completed navigation a human pause
and a pointer somewhere sensible, the other waits as long as the text on the
page would take to read. A site package that composes the page primitives
directly needs both to honour a tempo, and reaching into the LinkedIn-heavy
driver to get them -- or copying them -- is how two spellings of one pause
start to drift.
"""

from __future__ import annotations

import time

from . import interaction, pace, trace

#: How long a completed navigation is left alone before anything touches it.
ARRIVAL_SETTLE_RANGE = (0.6, 1.6)


def settle_arrival(page, rng) -> None:
    """Give a completed navigation the same pause and pointer state upstream uses."""
    with trace.phase(
            trace.WAIT, "settle",
            f"range {ARRIVAL_SETTLE_RANGE[0]}-{ARRIVAL_SETTLE_RANGE[1]}s"):
        time.sleep(rng.uniform(*ARRIVAL_SETTLE_RANGE))
    with trace.phase(trace.WORK, "place pointer", "fresh tabs only"):
        try:
            interaction.ensure_pointer_in_viewport(page, rng=rng)
        except Exception:
            pass


def dwell(capture, tempo) -> None:
    """Wait as long as reading what was captured would have taken."""
    if tempo is None:
        return
    pace.sleep_with_reason(
        tempo.dwell(capture.text_chars), "reading", name="dwell",
        detail=f"{capture.text_chars} chars · tempo '{tempo.name}'")
