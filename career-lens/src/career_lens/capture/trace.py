"""Where a run's time goes, as a log.

A run is minutes of wall clock and almost none of it is obvious from the
outside: a gap that looks like the site being slow may be a delay we chose, and
a delay that looks deliberate may be a probe loop nobody meant to write. The
first version of this module measured a 27-second overlay probe on pages with no
overlay -- 71% of a run -- which no amount of reading `pace.py` would have found.

So it stays, as ordinary logging rather than instrumentation to be torn out.

Three kinds, because they have different remedies:

  WAIT  a delay we chose. Tunable in pace.py -- this is the column that matters.
  NET   waiting on the far side: navigation, selector waits. Not ours to tune;
        it is the baseline a WAIT should look proportionate against.
  WORK  our own cost: DOM reads, screenshots, file writes, overlay probes.
        Not tunable by tempo, but a WORK phase that dwarfs the waits is a bug.

Levels, chosen so the level is a question about the run rather than about
verbosity:

  DEBUG    every phase, including the sub-second bookkeeping ones
  INFO     the phases worth watching live, plus the closing summary (default)
  WARNING  only phases that ran long or failed -- "show me what went wrong"

Output goes to the console only. Redirect it if you want to keep a run:
`career_lens ... --log-level debug > run.log`.

The level filters the narrative, never the arithmetic: every phase reaches the
closing summary whatever level it logged at. A summary that dropped its DEBUG
phases would have shares that do not add up, and would hide exactly the
cheap-but-numerous phase that turns out to own the run.

Inactive until `activate()` is called: importing this costs nothing and the test
suite never writes a file.
"""

from __future__ import annotations

import logging
import statistics
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

WAIT, NET, WORK = "WAIT", "NET", "WORK"

LOGGER_NAME = "career_lens.timing"

# Column widths. Fixed so the file scans vertically and greps cleanly. Wide
# enough for the longest phase name ("dismiss linkedin_signin_modal"), because a
# single overflowing row shifts the detail column and breaks the scan.
NAME_WIDTH = 28

# Phases that are bookkeeping rather than events: sub-second, uninteresting when
# they behave, and numerous enough to bury the run's narrative. Logged at DEBUG
# so they are there when a number needs explaining and absent when it does not.
ROUTINE = frozenset({
    "extract text", "extract html", "place pointer", "overlay check",
    "find overlay", "wait SERP", "back to SERP",
})

# A phase this long is worth seeing even when you asked for quiet. Not an error
# -- a slow site is not a bug -- but it is the shape of one.
SLOW_S = 5.0

# Level as a name the database stores, mapped explicitly rather than through
# logging.getLevelName: the journal's vocabulary is its own and must not move
# if logging's naming does.
LEVEL_NAMES = {logging.DEBUG: "debug", logging.INFO: "info",
               logging.WARNING: "warning"}

_log = logging.getLogger(LOGGER_NAME)


@dataclass
class Phase:
    """A span being timed. Mutable so a caller can describe it after the fact.

    Detail is set inside the block for the common case where the interesting
    number -- how many chars were scrolled, what status came back -- is only
    known once the work has happened.
    """

    name: str
    kind: str
    detail: str = ""


@dataclass
class Trace:
    started: float = field(default_factory=time.monotonic)
    depth: int = 0
    # name -> durations, top-level spans only (nested ones would double-count).
    spans: dict[str, list[float]] = field(default_factory=dict)
    kinds: dict[str, str] = field(default_factory=dict)
    # Where phases go besides the console. None keeps this module free of any
    # dependency on storage, which is what lets the test suite trace without
    # writing anything.
    sink: Callable[..., None] | None = None

    def stamp(self) -> str:
        elapsed = time.monotonic() - self.started
        return f"{int(elapsed // 60):02d}:{elapsed % 60:04.1f}"

    def record(self, phase: Phase, seconds: float, failed: bool) -> None:
        indent = "  " * self.depth
        name = f"{indent}{phase.name}".ljust(NAME_WIDTH)
        mark = " !!" if failed else ""
        if failed or seconds >= SLOW_S:
            level = logging.WARNING
        elif phase.name in ROUTINE:
            level = logging.DEBUG
        else:
            level = logging.INFO
        _log.log(level, "[%s] %7.2fs  %-4s  %s  %s%s",
                 self.stamp(), seconds, phase.kind, name, phase.detail, mark)
        if self.sink is not None:
            # Deliberately unguarded. A sink that can raise here would break a
            # run, so the sink owns that guarantee -- and a guard in this
            # module would make the sink's own guard untestable.
            self.sink(kind=phase.kind.lower(), phase=phase.name,
                      detail=phase.detail, seconds=seconds, failed=failed,
                      level=LEVEL_NAMES[level],
                      elapsed=time.monotonic() - self.started)
        if self.depth == 0:
            self.spans.setdefault(phase.name, []).append(seconds)
            self.kinds[phase.name] = phase.kind


_active: Trace | None = None


def configure(level: str | int = logging.INFO, *, stream=None) -> None:
    """Send timing records to the console at `level`.

    Written to stdout rather than the default stderr so it interleaves correctly
    with the run's own output, and with no prefix decoration: these lines carry
    their own timestamp column and a logger name in front of it would push the
    table out of alignment.
    """
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.setLevel(level)
    _log.handlers = [h for h in _log.handlers if not isinstance(h, logging.StreamHandler)]
    _log.addHandler(handler)
    # The logger itself stays wide open and the handlers do the filtering, so a
    # quiet console never costs the file its records.
    _log.setLevel(logging.DEBUG)
    # This logger formats for a human reading a table. Handing the records to
    # the root logger as well would print each line twice under any application
    # that configures logging itself.
    _log.propagate = False


def activate(header: list[str], sink: Callable[..., None] | None = None) -> Trace:
    """Begin a trace: write the banner and start the clock.

    With a sink, every phase is also handed to it -- that is how a run reaches
    the database. Without one this module behaves exactly as it did before.
    """
    global _active
    _active = Trace(sink=sink)
    banner = ["=" * 100, f"career_lens timing · {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    banner += [f"  {line}" for line in header]
    banner += [
        "",
        "  WAIT = a delay we chose (tunable in pace.py)",
        "  NET  = waiting on the far side (navigation, selector waits)",
        "  WORK = our own cost (DOM reads, screenshots, overlay probes)",
        "=" * 100,
        f"[  offset] elapsed  kind  {'phase'.ljust(NAME_WIDTH)}  what produced that number",
    ]
    for line in banner:
        _log.info("%s", line)
    return _active


@contextmanager
def phase(kind: str, name: str, detail: str = "") -> Iterator[Phase]:
    """Time a span, log it, and keep it in the summary. A no-op when inactive."""
    span = Phase(name=name, kind=kind, detail=detail)
    if _active is None:
        yield span
        return
    _active.depth += 1
    started = time.monotonic()
    failed = False
    try:
        yield span
    except BaseException:
        # A phase that raised still burned the time it burned, and the fact that
        # it failed is usually why the number is interesting.
        failed = True
        raise
    finally:
        _active.depth -= 1
        _active.record(span, time.monotonic() - started, failed)


def is_active() -> bool:
    """Whether a trace is running -- call sites use it to suppress their own,
    now-redundant, prints rather than reporting the same wait twice."""
    return _active is not None


def note(text: str) -> None:
    """A zero-duration marker: a group boundary, a challenge, a skip."""
    if _active is not None:
        _log.info("[%s]           --    %s", _active.stamp(), text)
        if _active.sink is not None:
            _active.sink(kind="note", phase=text, detail="", seconds=None,
                         failed=False, level="info",
                         elapsed=time.monotonic() - _active.started)


def close(tail: list[str] | None = None) -> None:
    """Write the summary and stop tracing."""
    global _active
    if _active is None:
        return
    trace, _active = _active, None
    total = time.monotonic() - trace.started

    by_kind: dict[str, float] = {}
    for name, durations in trace.spans.items():
        by_kind[trace.kinds[name]] = by_kind.get(trace.kinds[name], 0.0) + sum(durations)
    accounted = sum(by_kind.values())

    out: list[str] = ["", "=" * 100, f"SUMMARY · {_hms(total)} total"]
    out += [f"  {line}" for line in tail or []]
    out.append("")
    for kind in (WAIT, NET, WORK):
        seconds = by_kind.get(kind, 0.0)
        out.append(f"  {kind:<5} {_hms(seconds):>10}  {_share(seconds, total)}  "
                   f"{_KIND_NOTE[kind]}")
    untraced = total - accounted
    out.append(f"  {'other':<5} {_hms(untraced):>10}  {_share(untraced, total)}  "
               f"untraced (startup, browser launch, operator pauses, gaps between phases)")

    out += ["", f"  {'phase'.ljust(NAME_WIDTH)}  kind  {'count':>5} {'total':>9} "
                f"{'share':>7} {'min':>7} {'med':>7} {'max':>7}",
            "  " + "-" * (NAME_WIDTH + 51)]
    ranked = sorted(trace.spans.items(), key=lambda item: sum(item[1]), reverse=True)
    for name, durations in ranked:
        out.append(
            f"  {name.ljust(NAME_WIDTH)}  {trace.kinds[name]:<4}  {len(durations):>5} "
            f"{_hms(sum(durations)):>9} {_share(sum(durations), total):>7} "
            f"{min(durations):>6.2f}s {statistics.median(durations):>6.2f}s "
            f"{max(durations):>6.2f}s")

    waits = [(n, sum(d)) for n, d in ranked if trace.kinds[n] == WAIT]
    if waits:
        out += ["", "  Tunable in pace.py, biggest first:"]
        out += [f"    {name.ljust(NAME_WIDTH)} {_hms(seconds):>9}  "
                f"{_share(seconds, total)} of the run" for name, seconds in waits]
    out.append("=" * 100)
    for line in out:
        _log.info("%s", line)


_KIND_NOTE = {
    WAIT: "delays we chose -- tune these",
    NET: "the far side's latency -- the baseline waits should look proportionate to",
    WORK: "our own processing -- tempo cannot change this",
}


def _hms(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{seconds % 60:04.1f}s"


def _share(seconds: float, total: float) -> str:
    return f"{(seconds / total * 100 if total else 0):4.1f}%"
