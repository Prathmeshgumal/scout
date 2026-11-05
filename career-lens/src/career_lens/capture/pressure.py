"""What stood between a run and the page, and whether it stopped us.

A scrape that succeeds says nothing about what it had to get through. The
runtime dismisses overlays and detects walls all the way along, and a run that
cleared six banners and a CAPTCHA is otherwise filed exactly like one that
walked onto a clean page.

That difference is the thing worth watching. When a destination starts putting
challenges in front of us more often, it has formed a view about what we are
doing, and the only place that shows today is one attempt's progress log.

Only kinds in `TARGETED` are recorded. `INTERVENTIONS` stays the full
vocabulary because the database's CHECK constraint is built from it and rows
predating this rule still have to read back.

This module is the vocabulary and the ambient recorder. It deliberately holds
no storage: `activate` is handed a callable by whoever owns the attempt, which
is what lets the test suite exercise detection without writing anything, the
same arrangement `trace.py` uses for its sink.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Callable

# Every kind we detect or dismiss. Cookie banners and consent prompts are in
# here alongside the anti-bot vendors: the marker answers "was anything in the
# way", and the count over time is what carries the meaning.
INTERVENTIONS = frozenset({
    "akamai", "blocked", "captcha", "cloudflare", "consent", "cookie_banner",
    "datadome", "forbidden", "generic_login", "generic_modal",
    "google_consent", "google_location", "google_sorry", "imperva", "kasada",
    "linkedin_authwall", "linkedin_signin_modal", "linkedin_survey",
    "login_gate", "paywall", "perimeterx", "rate_limited", "unauthorised",
    "unavailable", "unavailable_legal", "view_limit",
})

# A sorted tuple, not the set: `enum_check` builds its CHECK expression by
# iterating, and a frozenset's order is not stable across runs -- the generated
# DDL would differ between a migration and a schema comparison.
INTERVENTION_KINDS = tuple(sorted(INTERVENTIONS))

#: dismissed -- an overlay rule matched and its control was clicked.
#: passed    -- a challenge was detected and the flow continued through it.
#: blocked   -- a challenge was detected and it stopped the attempt.
#: The last two are the point of recording an outcome at all: without them a
#: row cannot tell a run that sailed past a CAPTCHA from one it stopped.
INTERVENTION_OUTCOMES = ("blocked", "dismissed", "passed")

# Aimed at us rather than at every visitor, and the only kinds recorded.
#
# The distinction is the whole point of the feature. A cookie banner says
# nothing about whether a destination has noticed us -- every visitor gets one,
# and dismissing it is ordinary work. A CAPTCHA, an authwall or a 429 is the
# other side reacting. Recording both put a warning triangle on a row because
# the Guardian showed its consent dialog, which is noise wearing the costume of
# a signal.
#
# Routine dismissals are not lost: `capture/trace.py` still writes every sweep
# into the journal as free text ("dismissed cookie_banner"), which is where
# they belonged all along.
TARGETED = frozenset({
    "akamai", "blocked", "captcha", "cloudflare", "datadome", "forbidden",
    "google_sorry", "imperva", "kasada", "linkedin_authwall",
    "linkedin_signin_modal", "linkedin_survey", "perimeterx", "rate_limited",
    "unauthorised", "view_limit",
})

#: kind, outcome, host and evidence. Keyword-only, so a recorder can grow a
#: field without every call site moving.
Recorder = Callable[..., None]

_log = logging.getLogger("career_lens.pressure")

# A context variable rather than a module global, which is what `trace` uses.
# Trace can afford one: a native worker is a single process running a single
# attempt, so there is nothing to confuse it with. This cannot. The API serves
# every request, FastAPI runs its sync endpoints in a threadpool, and
# several node executions are interpreted at once -- a module global there
# would file one node's CAPTCHA against another node's attempt. A ContextVar
# is per-thread and per-task, so each execution sees only its own recorder.
_active: contextvars.ContextVar[Recorder | None] = contextvars.ContextVar(
    "career_lens_pressure_recorder", default=None)


def activate(recorder: Recorder | None):
    """Send interventions to `recorder`. Returns the token `restore` wants."""
    return _active.set(recorder)


def close(token=None) -> None:
    """Stop recording.

    With a token from `activate` the previous recorder is restored, which is
    what nesting needs. Without one the slot is simply cleared, which is what
    the native worker's single attempt wants.
    """
    if token is None:
        _active.set(None)
    else:
        _active.reset(token)


def is_active() -> bool:
    return _active.get() is not None


def record(*, kind: str, outcome: str, host: str, evidence: str = "") -> None:
    """Note one thing that stood in the way.

    Nothing here may fail a capture, and the guard matters more than usual:
    the challenge path calls this from inside a predicate, so an exception
    would change an answer the flow depends on rather than merely losing a row.
    """
    recorder = _active.get()
    if recorder is None:
        return
    if kind not in INTERVENTIONS:
        # A kind the database's CHECK would reject. Dropped here, where it is
        # one warning, rather than at the insert, where it would be one per
        # flush for the rest of the run.
        _log.warning("career_lens: unknown intervention kind %r", kind)
        return
    if kind not in TARGETED:
        # Routine: a consent dialog or cookie banner every visitor meets. Known
        # and handled, but not news, so it stays in the journal and off the
        # row. Debug rather than warning -- this is the common case, not a
        # fault.
        _log.debug("career_lens: routine intervention %r not recorded", kind)
        return
    if outcome not in INTERVENTION_OUTCOMES:
        _log.warning("career_lens: unknown intervention outcome %r", outcome)
        return
    try:
        recorder(kind=kind, outcome=outcome, host=host, evidence=evidence)
    except Exception as exc:  # noqa: BLE001 -- see the docstring
        _log.warning("career_lens: dropped intervention %s: %s", kind, exc)
