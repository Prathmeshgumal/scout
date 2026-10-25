"""Dismiss the dialogs that sit between us and the page.

Detection alone was not enough. `detect.py` recognised a consent banner and
classified it as non-blocking, then nothing clicked it -- so the overlay stayed
up, covered the content in every screenshot, and in Google's case hid all but
the first result from the DOM read. A banner that is noticed but never dismissed
is worse than one that is missed, because the run reports success.

Two dialogs seen in practice on a logged-out Indian residential connection:

* "See results closer to you?" -- Google's precise-location prompt, a modal that
  covers the result list.
* Cookie/consent banners on destination sites, in whatever framework they use.

Choices made here: precise location is always declined (we neither need it nor
want to hand it over), and cookie banners are accepted, because that is what the
overwhelming majority of people click and the resulting session is stabler.

Controls are found in two ways, and both matter:

* By the stable ID each consent vendor ships. Precise, and cheap to check.
* By what the control *says*, matched on its accessible name across every tag
  that carries the button role. consentmanager.net renders
  ``<a role="button">I Accept</a>``; a ``button:has-text()`` selector cannot
  match an anchor however right the text is.

Every frame is searched, not just the main one. Sourcepoint, and Didomi in
iframe mode, render the whole dialog into a cross-origin iframe, so the button
is a perfectly ordinary ``<button>`` that ``page.locator`` will never see.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Iterator
from urllib.parse import urlsplit

from patchright.sync_api import Page

from . import detect, interaction, pressure, trace

# (kind, vendor selectors, control labels). Labels are regexes matched against
# the accessible name, case-insensitively, and anchored so that "Accept all"
# does not also match "Do not accept all".
DISMISSALS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "google_location",
        (
            "button:has-text('Not now')",
            "div[role='button']:has-text('Not now')",
            "g-raised-button:has-text('Not now')",
        ),
        (r"^not now$",),
    ),
    (
        # Google's own button, by ID. Deliberately not a generic "Accept all"
        # label: that matched every consent banner on the web and reported it
        # as Google's, which is how sciencedirect.com came to be recorded as
        # google_consent.
        "google_consent",
        ("button#L2AGLb",),
        (),
    ),
    (
        # LinkedIn shows "Sign in to see who you already know" over a company
        # page whose content is fully rendered behind it. Dismissing it is the
        # difference between a screenshot of a login prompt and a screenshot of
        # the company.
        "linkedin_signin_modal",
        (
            "button[data-tracking-control-name*='contextual-sign-in'][aria-label*='Dismiss']",
            "button.modal__dismiss",
            "button[aria-label='Dismiss']",
            "icon[type='cancel-icon']",
        ),
        (),
    ),
    (
        # LinkedIn can inject a Qualtrics survey well after the profile first
        # renders. Qualtrics uses the same popover shell for different kinds of
        # intercepts, so require the survey iframe before pressing its close
        # control.
        "linkedin_survey",
        (
            ".QSIPopOver:has(iframe[title*='survey' i]) "
            "button[aria-label='Close Modal']",
        ),
        (),
    ),
    (
        # Consent platforms, by the stable IDs each vendor ships. Between them
        # these cover most of the managed-CMP market; the labels below catch
        # hand-rolled banners, but only English ones.
        "cookie_banner",
        (
            "#onetrust-accept-btn-handler",                                  # OneTrust
            "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",        # Cookiebot
            "#CybotCookiebotDialogBodyButtonAccept",                         # Cookiebot
            "#didomi-notice-agree-button",                                   # Didomi
            ".qc-cmp2-summary-buttons button[mode='primary']",               # Quantcast
            "button[data-testid='uc-accept-all-button']",                    # Usercentrics
            ".osano-cm-accept-all",                                          # Osano
            ".cky-btn-accept",                                               # CookieYes
            ".iubenda-cs-accept-btn",                                        # iubenda
            "button.t-acceptAllButton",                                      # Termly
            "button.cmplz-accept",                                           # Complianz
            "a.cc-btn.cc-dismiss",                                           # cookieconsent
            "button:has-text('Accept all cookies')",
            "button:has-text('Allow all cookies')",
            "button:has-text('Accept all')",
            "button:has-text('Accept cookies')",
            "button:has-text('I accept')",
            "button:has-text('I agree')",
            "button:has-text('Got it')",
            "button[aria-label*='Accept all']",
        ),
        (
            r"^accept all\b",
            r"^allow all\b",
            r"^accept cookies$",
            # Anchored on purpose. A word boundary here would reach "Accept
            # invitation" on a signed-in profile, which acts on a real account.
            r"^accept$",
            r"^i accept$",
            r"^i agree$",
            r"^i understand$",
            r"^got it$",
        ),
    ),
    (
        # Last resort: a modal that names itself as one and offers a close
        # control. Deliberately last -- accepting cookies is preferable to
        # closing the banner, since closing often leaves the site unusable.
        "generic_modal",
        (
            "[role='dialog'] [aria-label*='close' i]",
            "[role='dialog'] [aria-label*='dismiss' i]",
            "[role='dialog'] button:has(span:text-is('Close'))",
            "[aria-modal='true'] [aria-label*='close' i]",
        ),
        (),
    ),
)

PROBE_MS = 900

# Which hosts each rule is worth checking on. A rule with no entry runs
# everywhere. Looking for Google's location modal on a company blog cost 2.7s
# per page and could never have matched.
ONLY_ON = {
    "google_location": ("google.",),
    "google_consent": ("google.",),
    "linkedin_signin_modal": ("linkedin.com",),
    "linkedin_survey": ("linkedin.com",),
}

# How long to give a banner that is injected after domcontentloaded. Paid once
# per page, not once per candidate -- the banner either appeared inside this
# window or it did not, and asking thirty-odd times does not change that.
GRACE_S = 1.4

# Wall-clock budget for one sweep.
#
# A sweep costs (candidates x frames) locator queries, and the page controls the
# frame count. On an ordinary host 32 candidates apply -- the cookie banner's 28
# plus the generic modal's 4 -- so two frames is ~130 queries and about a
# second. A news article with an embedded video player brings the player's
# iframe plus its ad and analytics frames; at thirty frames the same sweep is
# 1740 queries and several minutes. One such page held a run open for seven
# minutes with every individual call inside its own timeout the whole time.
#
# Bounding the composition is the only thing that helps, which is the same
# reason MAX_SCROLL_STEPS exists. Ordinary pages finish well inside this and
# never notice it. When it does bite it gives up the right work first: rules are
# checked most-valuable first and main frame first, so what goes unchecked is
# the generic modal in the thirtieth iframe, not the cookie banner in the main
# frame.
SWEEP_BUDGET_S = 10.0

# Anything still covering this much of the viewport after dismissal is an
# overlay we do not have a rule for. We cannot click what we do not recognise,
# but we can record that it was there -- otherwise a missing rule is invisible
# and every screenshot of that host is quietly wrong.
UNDISMISSED_PROBE = """() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  for (const el of document.querySelectorAll('div,section,aside,dialog,form')) {
    const s = getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') continue;
    if (parseFloat(s.opacity) === 0) continue;
    if (s.position !== 'fixed') continue;
    const z = parseInt(s.zIndex, 10) || 0;
    if (z < 100) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh) continue;
    const covered = (r.width * r.height) / (vw * vh);
    if (covered < 0.15) continue;
    out.push({
      z, covers: Math.round(covered * 100),
      text: (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120),
    });
  }
  return out.sort((a, b) => b.z - a.z).slice(0, 2);
}"""


def undismissed(page: Page) -> list[dict]:
    """Overlays still covering the page after `dismiss` has run.

    A heuristic, and a deliberately loose one: cookie bars pinned to the bottom
    and sticky nav chrome can trip it. Its purpose is to surface hosts worth
    writing a rule for, not to be correct on every page.
    """
    with trace.phase(trace.WORK, "overlay check") as span:
        try:
            found = page.evaluate(UNDISMISSED_PROBE)
        except Exception:
            found = []
        span.detail = (f"{len(found)} still covering the page" if found
                       else "nothing left covering the page")
        return found


def applicable(page: Page):
    """The rules worth checking on the page's current host.

    A host-specific rule searched every frame of every page for a dialog only
    one site can show. When the host is unknown we check everything: missing a
    banner is worse than the search costing more than it needed to.
    """
    try:
        host = urlsplit(page.url).hostname or ""
    except Exception:
        host = ""
    if not host:
        return DISMISSALS
    return tuple(rule for rule in DISMISSALS
                 if rule[0] not in ONLY_ON or any(h in host for h in ONLY_ON[rule[0]]))


HAS_CONTENT = "() => !!document.body && document.body.childElementCount > 0"


def _frames(page: Page) -> list[Any]:
    """Main frame first, then every other frame with something in it.

    An empty frame cannot hold a dialog, and a signed-in feed carries several:
    ad slots and trackers that are created and never filled. Asking one whether
    it has a body costs a single call, against the whole selector set otherwise
    spent searching it -- 0.45s each, five of them, more than half a sweep.

    A frame that cannot be asked is searched anyway. Missing a dialog is worse
    than the cost of looking for one that was never there.
    """
    try:
        frames = list(page.frames)
    except Exception:
        return [page.main_frame]
    main = page.main_frame
    kept = [main]
    for frame in frames:
        if frame is main:
            continue
        try:
            if not frame.evaluate(HAS_CONTENT):
                continue
        except Exception:
            pass
        kept.append(frame)
    return kept


def _candidates(frame: Any, selectors: tuple[str, ...],
                labels: tuple[str, ...]) -> Iterator[Any]:
    for selector in selectors:
        yield frame.locator(selector)
    for label in labels:
        # get_by_role resolves the ARIA role, so <a role="button">, <input
        # type="button"> and a plain <button> all answer to the same query.
        yield frame.get_by_role("button", name=re.compile(label, re.I))


# Controls that must never be pressed, whatever else matched them. A newsletter
# modal offers a close control and a submit control, and a signed-in profile may
# have the form prefilled, so pressing the wrong one acts on a real account.
NEVER_PRESS = re.compile(
    r"^(subscribe|sign ?up|sign ?in|log ?in|register|submit|send|continue with"
    r"|create account|get started|buy|purchase|donate|apply)\b", re.I)

# The control's own text, not its accessible name. A close button may be
# labelled "Close subscribe popup", and guarding on the name would refuse to
# press the one control that closes the dialog.
OWN_TEXT = "el => (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim()"


def _may_press(target: Any) -> bool:
    try:
        return not NEVER_PRESS.match(target.evaluate(OWN_TEXT))
    except Exception:
        return True


def _press(page: Page, frame: Any, selectors: tuple[str, ...],
           labels: tuple[str, ...], probe_ms: int) -> bool:
    """Click the first visible candidate in this frame. True if one was clicked.

    Counted before waiting. A locator that matches nothing still burns the full
    probe timeout in wait_for, and with this many candidates across this many
    frames that is the difference between milliseconds and half a minute on
    every page that has no banner at all.
    """
    for locator in _candidates(frame, selectors, labels):
        try:
            if not locator.count():
                continue
            target = locator.first
            target.wait_for(state="visible", timeout=probe_ms)
        except Exception:
            continue
        if not _may_press(target):
            continue
        try:
            # A person sees the dialog before reacting to it.
            time.sleep(random.uniform(0.3, 0.8))
            interaction.click_in_place(page, target, timeout=2000)
            time.sleep(random.uniform(0.4, 0.9))
            return True
        except Exception:
            # Visible but unclickable. Trying the next label would press a
            # different control on a dialog that is already misbehaving.
            return False
    return False


def _host(page: Page) -> str:
    """The page's host, for the intervention record. Never raises: a page that
    cannot say where it is must not cost the dismissal that just worked."""
    try:
        return detect.host_of(page.url)
    except Exception:
        return ""


def _where(page: Page, frame: Any) -> str:
    """Which frame the control was found in.

    Worth recording because it is the difference between a site's own banner
    and a consent platform rendering into a cross-origin iframe -- and because
    it is part of what makes two dismissals distinct rather than a repeat.
    """
    try:
        if frame is page.main_frame:
            return "main frame"
        return f"iframe {detect.host_of(frame.url)}"
    except Exception:
        return ""


def _sweep(page: Page, rules, probe_ms: int,
           budget_s: float = SWEEP_BUDGET_S) -> tuple[list[str], bool]:
    """One pass over every applicable rule, in every frame.

    Returns the kinds dismissed and whether the budget ran out before every
    (rule, frame) pair had been looked at.
    """
    deadline = time.monotonic() + budget_s
    dismissed: list[str] = []
    # Enumerated once, not once per rule: the frames cannot change between two
    # rules of one pass, and finding them is no longer free.
    frames = _frames(page)
    for kind, selectors, labels in rules:
        for frame in frames:
            # Checked per frame rather than per candidate: the granularity only
            # has to be fine enough to stop, and a frame is one coherent unit
            # of looking.
            if time.monotonic() >= deadline:
                return dismissed, True
            try:
                if _press(page, frame, selectors, labels, probe_ms):
                    dismissed.append(kind)
                    # Recorded here rather than from the caller's summary
                    # string: this is the point that knows which rule fired and
                    # on which page, and the summary has already lost the URL.
                    pressure.record(kind=kind, outcome="dismissed",
                                    host=_host(page),
                                    evidence=_where(page, frame))
                    break
            except Exception:
                # A frame can detach mid-search, which says nothing about the
                # frames we have not looked at yet.
                continue
    return dismissed, False


def dismiss(page: Page, *, probe_ms: int = PROBE_MS, grace_s: float = GRACE_S,
            budget_s: float = SWEEP_BUDGET_S) -> list[str]:
    """Click away any known overlay. Returns the kinds dismissed.

    Two passes, because a single look is the wrong shape for the problem.
    Counting before waiting means a control that is not in the DOM yet is not
    waited for at all -- which is most of them, since consent platforms mount
    after domcontentloaded. So if the first pass finds nothing, wait once and
    look again. That grace is one wait for the whole page, not one per
    candidate: the banner either appeared inside it or it did not, and asking
    each of thirty-odd candidates separately does not change the answer.

    `grace_s=0` therefore means one pass. The second exists only to catch a
    banner that arrives late, so a caller saying not to wait for one is asking
    for the same search to be run twice back to back.
    """
    rules = applicable(page)
    with trace.phase(trace.WORK, "sweep overlays") as span:
        dismissed, cut = _sweep(page, rules, probe_ms, budget_s)
        span.detail = (f"dismissed {', '.join(dismissed)}" if dismissed
                       else f"nothing yet · {len(rules)}/{len(DISMISSALS)} rules apply here")
        # Said out loud. A sweep that ran out of budget looked at less of the
        # page than the one beside it in the log, and a banner missed for that
        # reason is otherwise indistinguishable from a page that had none.
        if cut:
            span.detail += (f" · gave up after {budget_s}s, "
                            f"{len(page.frames)} frames to search")
    if dismissed or not grace_s:
        return dismissed

    with trace.phase(trace.WAIT, "overlay grace",
                     f"{grace_s}s for a late-injected banner, then look again"):
        time.sleep(grace_s)
    with trace.phase(trace.WORK, "sweep overlays") as span:
        dismissed, cut = _sweep(page, rules, probe_ms, budget_s)
        span.detail = (f"dismissed {', '.join(dismissed)}" if dismissed
                       else "page is clear")
        if cut:
            span.detail += (f" · gave up after {budget_s}s, "
                            f"{len(page.frames)} frames to search")
    return dismissed
