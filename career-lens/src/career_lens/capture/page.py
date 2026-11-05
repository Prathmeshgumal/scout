"""What we keep from a visited page.

A screenshot alone is close to useless for company research -- you cannot grep a
PNG. Every capture also carries the rendered text and the HTML, and a metadata
row recording what actually happened rather than what we asked for (final_url
after redirects, status, whether a challenge intervened).

The payloads are returned rather than written: the caller stores them by key in
the blob store, so nothing here decides where anything lives. Screenshots are
the exception in shape only -- Playwright's API writes through a path, so a tall
page's bands go to a scratch directory and are read straight back.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from lxml import html

from ..redact import redact_header
from ..linkedin.readiness import PROFILE_LOADING_SELECTOR

from PIL import Image

from . import interaction, overlays, trace
from .limits import FEED_SCROLL_PX as FEED_SCROLL_PX
from .limits import MAX_SCROLL_PX as MAX_SCROLL_PX
from .limits import MAX_SCROLL_STEPS as MAX_SCROLL_STEPS
from .limits import BAND_PX as BAND_PX
from .limits import MAX_BANDS as MAX_BANDS
from .marks import ARTIFACT_ONLY

if TYPE_CHECKING:      # annotations only; nothing here calls into patchright
    from patchright.sync_api import Page, Response

# Past this height the page is captured in bands rather than one image: a single
# tall shot repaints the top of the page part way down instead of continuing it.
# The threshold and the slice are one number by necessity, not by coincidence: a
# slice taller than the height that triggers slicing would never be taken.

# The shortest screen worth taking. The last step of a page is whatever it
# had left, which can be a handful of pixels, and a strip that thin is not a
# picture of anything. Such a shot is grown upwards instead, over ground the one
# above it already covered, so the bottom of a page is never dropped either.
MIN_SCREEN_PX = 200

# How long a shot waits for the pictures on screen. A lazily loaded image is
# asked for as it comes into view and arrives some time after, so a shot taken
# the moment scrolling stops catches the gap and photographs an empty box.
# Bounded, because a feed always holds something that never resolves.
IMAGE_SETTLE_MS = 3000

# A screenshot waits on the page, so a page that is still settling holds it open
# -- measured at over a minute across the first pages of a session. Bounded here
# rather than left to the 30 second default, which was paid twice: once for the
# page and once again for the fallback below it.
SHOT_TIMEOUT_MS = 15000
FALLBACK_TIMEOUT_MS = 8000

# --- waiting for a page that is still getting taller -------------------------
#
# Separate from the placeholder wait above, and it has to be: a section that
# has not mounted yet leaves no placeholder behind on this build -- the page is
# simply shorter, and everything below the gap sits higher up the screen than
# it will in a moment. LinkedIn's footer is the one that shows: while the
# profile is short the footer is genuinely on screen, so a screen taken then
# photographs it, and the stitch keeps it where it was. The result is a footer
# down the middle of the page with the section that finally arrived painted
# over it.
#
# Quiet polls rather than a fixed pause, for the reason the list waits give:
# what settles a page under one tempo starves it under another. Cheap when
# nothing is happening -- a static page leaves after two polls.
#
# Quiet polls alone are not enough on the first screen. A page that has not
# started growing looks exactly like one that has finished, and the first
# screen is taken at the moment that confusion is most likely -- before
# anything below the fold has been asked for. Measured on the fixture: screen
# 1 left after two quiet polls at 0.71s and photographed a footer that moved
# 500ms later, which is the duplicate footer this whole wait exists to stop.
#
# So the first screen does not accept quiet until a floor has passed.
# LAZY_CONFIRM_S is that floor and already means this exact thing elsewhere --
# "how long before believing a page that says it has ended" -- and it is paid
# once per page, not once per screen.
HEIGHT_QUIET_POLL_S = 0.15
HEIGHT_QUIET_POLLS = 2
#: Bounded like every other wait here. A feed that keeps producing must not
#: hold a capture open, and whatever height it had reached is what gets shot.
HEIGHT_SETTLE_BUDGET_S = 2.0

#: How tall the thing being scrolled currently is. The marked scroller when
#: there is one -- `FIND_SCROLLER` sets `data-wf-scroller` -- because on a page
#: whose content lives in an inner element the document's own height never
#: moves.
CONTENT_EXTENT = """() => {
  const scroller = document.querySelector('[data-wf-scroller]');
  if (scroller) return scroller.scrollHeight;
  const doc = document.documentElement;
  return Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0);
}"""

# One wait for the arrived page, before any of it is photographed. Longer than
# IMAGE_SETTLE_MS because this is paid once per page rather than once per
# screen, and a profile's sections mount well after the document is
# interactive.
PAGE_SETTLE_MS = 8000

# Lazy-loaded content only appears if something scrolls, and a feed keeps
# producing more for as long as it is asked. The one number that decides how
# deep to go, in pixels rather than steps: a step is one viewport, which is a
# different distance on every window, and pixels are the unit the screenshot
# limits above already use.
#
# A page that ends is followed to its end. The number is a ceiling against a
# page that turns out not to end after all, not a target, and an ordinary
# document stops long before it.
# A feed that keeps producing for as long as it is asked has no end to reach,
# so it gets a budget instead of a ceiling. Sized to the first handful of posts,
# which is what a company page is read for: a post measured around 500px, so
# this is ten or a dozen of them depending on how many carry images. The caller
# asks for it, because only the caller knows which pages are feeds.
# A backstop on the loop, not a second budget: at roughly a viewport per step
# the distance above binds first on any real page. It is here so that a page
# answering a full wheel with a pixel of movement cannot spin.

# The settle pause is per step and was the single largest fixed cost per page:
# 25 steps at up to 0.6s each is 15 seconds on every visit, which does not scale
# past a handful of companies. Larger steps with a shorter pause cover the same
# page in a third of the time and still give lazy loaders a chance to fire.
SCROLL_SETTLE_RANGE = (0.12, 0.3)
SCROLL_SETTLE_MODE_S = 0.19

SCROLL_FLICK_RATIO = (0.45, 0.95)
SCROLL_FLICK_MODE = 0.72
WHEEL_NOTCH_GAP_S = (0.006, 0.028)
WHEEL_NOTCH_GAP_MODE_S = 0.013

POINTER_MOVE_FLICK_GAP = (3, 4)
POINTER_MOVE_CHANCE = 0.45

READ_FLICK_GAP = (2, 4)
READ_PAUSE_CHANCE = 0.45

# How long to wait before believing a feed that says it has ended. Paid only
# where the page has already grown under the wheel, so an ordinary page never
# waits at all.
LAZY_CONFIRM_S = 1.5

# --- waiting for a lazy list to finish ---------------------------------------
#
# A list that appends as you reach its bottom is done when it stops working,
# not when a timer expires. Three signals say "still working", and any one of
# them is enough to keep waiting:
#
#   1. requests outstanding   -- the batch is on the wire
#   2. the column still growing -- entries are landing (catches a list that
#      pages from data already delivered, where signal 1 never fires)
#   3. a loading indicator on screen -- LinkedIn says so itself
#
# Waiting on a condition rather than a duration is what makes this independent
# of tempo. Under `research` the wheel pauses ~0.08s between flicks and under
# `human` ~0.83s, so a fixed settle that suffices for one starves the other --
# which is how a section announcing 14 courses was stored holding 10.
#
# Every wait is bounded twice: one budget per settle, and a hard ceiling on the
# whole operation. Both return what has loaded rather than raising, because a
# short list is worth keeping and the announced-count contract will say so.
LIST_QUIET_POLL_S = 0.25
#: Consecutive quiet polls before a list is believed finished. Two rather than
#: one: a batch that has landed but not yet triggered the next fetch looks
#: identical to a list that has ended, for about one poll.
LIST_QUIET_POLLS = 2
#: One settle wait. Generous, because it is only paid while something is
#: actually happening -- a finished list leaves after two polls.
LIST_SETTLE_BUDGET_S = 12.0
#: The ceiling on loading one list, across every pass. Whatever has arrived by
#: now is what gets stored.
LIST_TOTAL_BUDGET_S = 45.0

#: Is the page telling us it is still fetching? Scoped to a container so a
#: spinner elsewhere on the page -- the message rail mounts one -- does not
#: hold the capture open.
LIST_LOADING = """(selectors) => {
  let scope = null;
  for (const selector of (selectors || [])) {
    scope = document.querySelector(selector);
    if (scope) break;
  }
  scope = scope || document.body;
  if (!scope) return false;
  if (scope.querySelector('[role="progressbar"]')) return true;
  if (scope.querySelector('[aria-busy="true"]')) return true;
  return Boolean(scope.querySelector(
      '.artdeco-loader, .scaffold-skeleton, [class*="skeleton"]'));
}"""


class RequestsInFlight:
    """How many requests the page currently has outstanding.

    Passive listening only. `page.route` would intercept, which this codebase
    must never do -- Patchright's stealth depends on it -- but `page.on` is the
    same read-only channel `dom.py` already uses to collect Voyager payloads.

    Counting starts and ends rather than tracking identities: the number is
    only ever compared against zero, and a request object that outlives its
    page is a leak this does not need.
    """

    def __init__(self, page: Page):
        self.page = page
        self.started = 0
        self.finished = 0
        self.attached = False

        def began(_request: object) -> None:
            self.started += 1

        def ended(_request: object) -> None:
            self.finished += 1

        self._began, self._ended = began, ended
        try:
            page.on("request", began)
            page.on("requestfinished", ended)
            page.on("requestfailed", ended)
            self.attached = True
        except Exception:
            # A page that cannot be listened to still scrolls; the other two
            # signals carry the wait.
            pass

    @property
    def outstanding(self) -> int:
        return max(0, self.started - self.finished)

    def close(self) -> None:
        if not self.attached:
            return
        for event, handler in (("request", self._began),
                               ("requestfinished", self._ended),
                               ("requestfailed", self._ended)):
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass
        self.attached = False


def is_loading(page: Page, scope: list[str] | None = None) -> bool:
    """Whether the page is displaying a loading indicator.

    `scope` is tried in order, so a caller can name the exact container and
    fall back to a looser one rather than watching the whole document -- a
    spinner in the message rail must not hold a profile capture open.
    """
    try:
        return bool(page.evaluate(LIST_LOADING, scope or []))
    except Exception:
        # Mid-navigation, or a page that will not run script. Treat as quiet:
        # the caller's other signals decide, and a page we cannot ask is not a
        # page we should wait on forever.
        return False


def quiet(outstanding: int, grew: bool, loading: bool) -> bool:
    """Whether nothing is happening. Pure, so the rule can be tested alone."""
    return outstanding == 0 and not grew and not loading


def wait_until_settled(
    page: Page,
    watch: "RequestsInFlight | None",
    measure: Callable[[], int],
    *,
    scope: list[str] | None = None,
    budget_s: float = LIST_SETTLE_BUDGET_S,
    deadline: float | None = None,
) -> tuple[int, str]:
    """Wait for a lazy list to stop working. Returns (size, why it stopped).

    `measure` reports how much the list holds -- characters rather than pixels,
    because that is what we are there to collect. `deadline` is the caller's
    hard ceiling, which this never runs past.
    """
    limit = time.monotonic() + budget_s
    if deadline is not None:
        limit = min(limit, deadline)
    size = measure()
    still = 0
    while time.monotonic() < limit:
        time.sleep(LIST_QUIET_POLL_S)
        grown = measure()
        busy_network = watch.outstanding if watch is not None else 0
        if quiet(busy_network, grown > size, is_loading(page, scope)):
            still += 1
            if still >= LIST_QUIET_POLLS:
                return grown, "quiet"
        else:
            still = 0
        size = max(size, grown)
    return size, "budget"


# Why a scroll stopped. Recorded on the capture, because a page cut short at the
# budget and a page that genuinely ended look identical once they are stored.
BOTTOM = "bottom"
DISTANCE = "distance"
STALLED = "stalled"

# What moved. The two paths fail differently and recover differently, so which
# one ran is part of what a stall means.
WINDOW = "window"
ELEMENT = "element"

# Subpixel layout means a page at its bottom rarely reports exactly its own
# height. The same slack the scroll loops allow when they ask the question.
SETTLED_PX = 2

# A wheel turns in notches of about this much, so a flick is a burst of them.
# One event carrying a whole viewport is not something an input device emits.
WHEEL_NOTCH_PX = 114

# How far past the top to aim the wheel that returns a page there when the
# keyboard cannot. The wheel is sized from where the page actually is, so this
# is only the overshoot that makes it clamp at the top rather than stop short.
MAX_WHEEL_STEP_PX = 4000

# Cheap page metrics, read before every step. `body.scrollHeight` alone is not
# enough: a page whose body is height-constrained reports its viewport height
# and nothing else, so the caller concludes it has already reached the bottom.
PAGE_METRICS = """() => ({
  y: window.scrollY,
  v: window.innerHeight,
  w: Math.max(document.body.scrollWidth, document.documentElement.scrollWidth,
              window.innerWidth),
  h: Math.max(document.body.scrollHeight, document.documentElement.scrollHeight),
})"""

VISIBLE_ITEM_TEXT = """selector => Array.from(document.querySelectorAll(selector))
  .filter(node => {
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0
        && rect.bottom > 0 && rect.top < innerHeight;
  })
  .map(node => node.innerText.trim().length)
  .filter(length => length > 0)"""

# How far down the deepest rendered element actually reaches. Measured only when
# the cheap metrics claim the bottom, because it walks the DOM and forces a
# layout -- too expensive to pay on every step of every page.
#
# Fixed elements are excluded: sticky headers and chat widgets travel with the
# viewport, so their position says nothing about how much page is left.
CONTENT_BOTTOM = """() => {
  let bottom = 0;
  for (const el of document.querySelectorAll('div,section,main,article,footer')) {
    const rect = el.getBoundingClientRect();
    if (rect.height === 0) continue;
    if (getComputedStyle(el).position === 'fixed') continue;
    const edge = rect.bottom + window.scrollY;
    if (edge > bottom) bottom = edge;
  }
  return Math.round(bottom);
}"""

# Some applications scroll an element rather than the window -- LinkedIn puts
# the whole profile inside `main#workspace`, so `window.scrollY` never moves
# however much the page does. Finding that element is what makes the wheel land
# somewhere useful.
#
# Which surface owns the page is a containment question before it is anything
# else, and the answer is decided in two steps.
#
# Outermost first: a rail nested inside a scrolling shell is never the shell,
# whatever share of the screen it happens to occupy on this window. This is what
# replaces ranking candidates by visible scroll area, which let a 464px table of
# contents out-rank the document it sat in -- the capture then wheeled the
# contents list while the article stood still, and stored one screen of it.
#
# Then the document, which wins ties: whichever of the two has further to travel
# owns the page. An element holds the scroll only where the document has less of
# it, which is exactly what separates a profile shell that the window cannot
# move from a sidebar beside a long article.
#
# Nothing here describes a site. It asks only where this page keeps its scroll,
# which is why it needs no rule per site to get an unfamiliar one right.
FIND_SCROLLER = """() => {
  // As wide as the page and most of its height. A surface that owns a page is
  // the size of one; a rail down the side of an article is not, however much
  // it holds.
  const pageSized = (el) => {
    const rect = el.getBoundingClientRect();
    return rect.width >= innerWidth * 0.9 && rect.height >= innerHeight * 0.6;
  };

  // Somewhere to go, and something that lets a reader go there. A shell that
  // clips its own overflow still scrolls programmatically and some apps drive
  // the page that way -- but only when it IS the page. Every other clipped box
  // is a layout wrapper.
  const eligible = (el) => {
    if (el.scrollHeight <= el.clientHeight + 2) return false;
    const style = getComputedStyle(el);
    if (/(auto|scroll|overlay)/i.test(style.overflowY)) return true;
    return style.overflowY === 'hidden' && pageSized(el);
  };

  let best = document.querySelector('[data-wf-scroller]');
  if (!best) {
    // Breadth-first from the body, so the outermost surface is reached before
    // anything nested inside it. A rail inside a scrolling shell never wins.
    const queue = [...document.body.children];
    while (queue.length && !best) {
      const el = queue.shift();
      if (eligible(el)) best = el;
      else queue.push(...el.children);
    }
    if (!best) return null;
    // The document is the other candidate. Both roots are measured, for the
    // reason PAGE_METRICS gives: a root that clips reports its own viewport
    // height and nothing else while the body below it is thousands of pixels
    // tall -- kore.ai reports exactly that until its scroll library finishes
    // starting up.
    const rootLeft = Math.max(document.body.scrollHeight,
                              document.documentElement.scrollHeight)
                     - innerHeight;
    // A document that cannot move at all leaves whatever scrolls as the only
    // way through the page, however narrow it is -- that is the profile shell,
    // and on a compact layout it may be no wider than a column.
    //
    // A document that can move keeps the scroll unless something both the size
    // of the page and with further to go takes it. Size is the half that
    // matters here: a sidebar listing every page on a site routinely has more
    // travel than the article beside it and is still a sidebar.
    if (rootLeft > 2
        && (!pageSized(best) || best.scrollHeight - best.clientHeight <= rootLeft))
      return null;
  }
  if (!best) return null;
  best.setAttribute('data-wf-scroller', '1');
  const rect = best.getBoundingClientRect();
  // The middle of the part on screen, not of the element. One taller than the
  // viewport has its own centre below it, and a wheel delivered off-screen
  // still scrolls the page but dispatches no wheel event for the page to see.
  const x0 = Math.max(rect.left, 0), x1 = Math.min(rect.right, window.innerWidth);
  const y0 = Math.max(rect.top, 0), y1 = Math.min(rect.bottom, window.innerHeight);
  return {
    x: Math.round((x0 + x1) / 2),
    y: Math.round((y0 + y1) / 2),
    top: best.scrollTop,
    height: best.scrollHeight,
    client: best.clientHeight,
    left: Math.round(x0),
    right: Math.round(x1),
    viewportTop: Math.round(y0),
    viewportBottom: Math.round(y1),
    crop: best.hasAttribute('data-wf-scroller-crop'),
  };
}"""

# Taken off once the scrolling is done. It is ours, not the site's, and the HTML
# is read from this page after the sweep -- an attribute we left behind would be
# recorded as though it had been served.
CLEAR_SCROLLER = """() => {
  const el = document.querySelector('[data-wf-scroller]');
  if (el) {
    el.removeAttribute('data-wf-scroller');
    el.removeAttribute('data-wf-scroller-crop');
  }
}"""

SCROLLER_STATE = """() => {
  const el = document.querySelector('[data-wf-scroller]');
  return el ? {top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight} : null;
}"""

# Whichever of the two is doing the scrolling, is it back at the start?
AT_TOP = """() => {
  const el = document.querySelector('[data-wf-scroller]');
  return (el ? el.scrollTop : window.scrollY) <= 2;
}"""

# How far the content has been moved, by whichever of the two moved it. A shot
# has to be placed against the thing that actually scrolled, and the window and
# an inner scroller answer to different properties.
CONTENT_TOP = """() => {
  const el = document.querySelector('[data-wf-scroller]');
  return Math.round(el ? el.scrollTop : window.scrollY);
}"""

# What one screen of the page is. Read separately from PAGE_METRICS, whose
# width is the document's and can be wider than the window: a clip has to stay
# inside what is on screen.
SCREEN = """() => ({w: window.innerWidth, h: window.innerHeight})"""

# Anything pinned to the window travels with it, so it lands in every screen of
# a page photographed a screen at a time. Joined, the same header and the same
# corner widget run down the middle of the page.
#
# Taken out for the screens below the first, where they have already been
# photographed once, in the place a reader sees them. Hidden rather than removed
# so a pinned element that sits in the flow leaves its space behind and nothing
# reflows, and the element that is actually scrolling is left alone whatever it
# is positioned as.
HIDE_PINNED = """() => {
  const scroller = document.querySelector('[data-wf-scroller]');
  let count = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (el.hasAttribute('data-wf-pinned')) continue;
    const style = getComputedStyle(el);
    if (style.position !== 'fixed' && style.position !== 'sticky') continue;
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (scroller && (el === scroller || el.contains(scroller))) continue;
    el.setAttribute('data-wf-pinned', el.getAttribute('style') || '');
    // `visibility` is inherited but a descendant is free to declare itself
    // visible again inside a hidden ancestor, and a chat bubble riding on a
    // pinned container does exactly that. `display` cannot be answered that
    // way. It is only safe on the fixed ones, which are out of the flow
    // already, so taking them away moves nothing; a sticky element holds a
    // place in the flow and losing it would reshape the page under the shot.
    if (style.position === 'fixed') el.style.setProperty('display', 'none', 'important');
    else el.style.setProperty('opacity', '0', 'important');
    count++;
  }
  return count;
}"""

# A playing video is a different picture in every screen, so one straddling the
# join between two of them comes out as two moments of the same face spliced
# together. Paused, every screen sees the same frame. Nothing resumes them: a
# paused video is a property rather than an attribute, so it leaves no mark on
# the HTML read from the page afterwards, and resuming would only let the next
# screen catch it somewhere else.
PAUSE_VIDEOS = """() => {
  let playing = 0;
  for (const video of document.querySelectorAll('video')) {
    if (video.paused) continue;
    video.pause();
    playing++;
  }
  return playing;
}"""

SHOW_PINNED = """() => {
  for (const el of document.querySelectorAll('[data-wf-pinned]')) {
    const before = el.getAttribute('data-wf-pinned');
    el.removeAttribute('data-wf-pinned');
    if (before) el.setAttribute('style', before);
    else el.removeAttribute('style');
  }
}"""

# Pictures on screen the browser has asked for and not yet finished with. Only
# `complete` is tested: it also turns true when a load fails, so a broken image
# cannot hold a shot open for its whole budget. An image with no source is not
# pending but deferred -- nothing has been asked for yet, and waiting on one
# would never clear. Off-screen images are somebody else's shot.
PENDING_IMAGES = """() => {
  const h = window.innerHeight, w = window.innerWidth;
  let pending = 0;
  for (const img of document.images) {
    if (!img.currentSrc || img.complete) continue;
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    if (rect.bottom < 0 || rect.top > h || rect.right < 0 || rect.left > w) continue;
    pending += 1;
  }
  return pending;
}"""

# A placeholder, recognised by what it is rather than by what it is called.
#
# The named selectors below are kept and are still the cheap answer on a page
# that sets them -- but the LinkedIn build in production sets none of them. A
# captured profile contains zero `aria-busy`, zero `role="progressbar"` and
# zero occurrences of the string "skeleton" anywhere, because its class names
# ship as hashes (`_9840af22 _22bf7297`). Matching on a name is matching on
# something the build is free to obfuscate, and it did.
#
# What a placeholder cannot hide is its behaviour: it pulses, it occupies
# space, and it holds no text -- text is the thing it is standing in for. All
# three are required together. Animation alone would catch every decorative
# pulse on the page (LinkedIn's "Open to work" badge animates forever, and
# waiting on it would spend the whole budget on every capture); textless alone
# would catch every spacer and rule.
PLACEHOLDER_MIN_PX = 8

_PENDING_VISUALS = """(scopeSelectors) => {
  const h = window.innerHeight, w = window.innerWidth;
  const onScreen = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden'
        || Number(style.opacity) === 0) return null;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    if (rect.bottom < 0 || rect.top > h || rect.right < 0 || rect.left > w) return null;
    return style;
  };
  // Honour the first available selector in an ordered cascade. A broad
  // fallback is only considered if the more semantic primary scope is absent;
  // otherwise it would re-include sidebars that the scope intentionally omits.
  let scopeRoots = null;
  if (Array.isArray(scopeSelectors)) {
    for (const selector of scopeSelectors) {
      try {
        const roots = [...document.querySelectorAll(selector)];
        if (roots.length) {
          scopeRoots = roots;
          break;
        }
      } catch {
        // An invalid selector must preserve the conservative whole-document
        // behaviour, not make an unfamiliar page appear settled.
      }
    }
  }
  const inScope = (el) => scopeRoots === null
    || scopeRoots.some((root) => root === el || root.contains(el));
  const seen = new Set();
  for (const el of document.querySelectorAll(
      '[role="progressbar"], [aria-busy="true"], [class*="skeleton" i]')) {
    if (inScope(el) && onScreen(el)) seen.add(el);
  }
  const MIN = %d;
  for (const el of document.querySelectorAll('body *')) {
    if (seen.has(el)) continue;
    if (!inScope(el)) continue;
    const style = onScreen(el);
    if (style === null) continue;
    if (style.animationName === 'none' || style.animationIterationCount === '1') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width < MIN || rect.height < MIN) continue;
    // Text anywhere beneath it means it is content that happens to move, not
    // a stand-in for content that has not arrived.
    if ((el.textContent || '').trim().length) continue;
    // An element whose own children are the placeholders is counted once,
    // through them, rather than twice.
    let nested = false;
    for (const child of seen) { if (el.contains(child)) { nested = true; break; } }
    if (nested) continue;
    seen.add(el);
  }
  // Profile loading bars can be static and have no accessibility markers.
  if (/(^|\\.)linkedin\\.com$/.test(location.hostname)
      && /^\\/in\\/[^/]+\\/?$/.test(location.pathname)) {
    for (const el of document.querySelectorAll('%s')) {
      if (inScope(el) && onScreen(el) && !(el.textContent || '').trim()) seen.add(el);
    }
  }
  return seen.size;
}""" % (PLACEHOLDER_MIN_PX, PROFILE_LOADING_SELECTOR)

# Keep the long-standing no-argument expression for generic callers. LinkedIn
# recipes pass a scope through `_pending_visuals_expression` below.
PENDING_VISUALS = f"({_PENDING_VISUALS})(null)"

# Chrome screenshots the document, and a document whose content lives inside a
# clipped element is one viewport tall however much is rendered inside it. Every
# ancestor from the scroller up participates in the clip -- on a LinkedIn
# profile that is eight elements, six of them `overflow: hidden` with a fixed
# height -- so the whole chain has to be opened up, not just the scroller.
#
# The previous inline style is parked on each element so it can be put back:
# the HTML capture runs after the screenshot and should record the page as
# served, not as we bent it.
UNCLIP = """() => {
  const start = document.querySelector('[data-wf-scroller]');
  if (!start) return 0;
  let count = 0;
  for (let el = start; el; el = el.parentElement) {
    el.setAttribute('data-wf-style', el.getAttribute('style') || '');
    el.style.setProperty('height', 'auto', 'important');
    el.style.setProperty('max-height', 'none', 'important');
    el.style.setProperty('overflow', 'visible', 'important');
    count++;
  }
  return count;
}"""

RECLIP = """() => {
  for (const el of document.querySelectorAll('[data-wf-style]')) {
    const previous = el.getAttribute('data-wf-style');
    if (previous) el.setAttribute('style', previous);
    else el.removeAttribute('style');
    el.removeAttribute('data-wf-style');
  }
  for (const el of document.querySelectorAll('[data-wf-scroller]')) {
    el.removeAttribute('data-wf-scroller');
    el.removeAttribute('data-wf-scroller-crop');
  }
}"""


@dataclass
class Scroll:
    """How far a page was travelled, and what ended the journey."""

    pixels: int = 0
    stopped: str = BOTTOM
    # Whether the page grew under the wheel, so more would have arrived had we
    # kept going. Running out of budget only costs something on a page that
    # did: a tall static document is already in the DOM, so the text and the
    # screenshot get all of it however little of it we scrolled past.
    growing: bool = False
    # Which of the two paths travelled it. `defeated` needs this: a stalled
    # window scroll can be recovered by photographing the document instead,
    # and a stalled inner scroller cannot.
    owner: str = WINDOW


def defeated(scroll: Scroll, *, viewport: int, height: int) -> bool:
    """Whether a scroll saw so little of the page that its screens are waste.

    A window scroll that stalled with document still below it did not travel:
    something held the page still -- a consent banner setting `overflow:
    hidden` is the common one -- and every screen after the first photographs
    ground the one above it already holds. The whole document is in the DOM
    regardless, so one full-page shot is strictly more of the page than all of
    those screens together.

    Only the window path. Content inside a clipped element is one viewport tall
    to a full-page screenshot however much rendered inside it, so trading an
    inner scroller's screens for one would store less, not more.
    """
    if scroll.owner != WINDOW or scroll.stopped != STALLED:
        return False
    return height > scroll.pixels + viewport + SETTLED_PX


@dataclass
class PageCapture:
    url_requested: str
    final_url: str | None = None
    status: int | None = None
    request_method: str | None = None
    request_headers: dict[str, str] | None = None
    response_headers: dict[str, str] | None = None
    title: str | None = None
    ok: bool = False
    error: str | None = None
    challenge: dict[str, Any] | None = None
    screenshot: str | None = None
    # Band filenames in page order. A page short enough for one image has one
    # entry; a tall one has up to MAX_BANDS, and `screenshot` is the first.
    screenshot_parts: list[str] = field(default_factory=list)
    screenshot_truncated: bool = False
    screenshot_error: str | None = None
    dismissed_late: list[str] = field(default_factory=list)
    text: str | None = None
    text_chars: int = 0
    scrolled_px: int = 0
    scroll_stopped: str = ""
    scroll_growing: bool = False
    load_ms: int = 0
    captured_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    # Payloads, for the caller to store. Excluded from to_dict: a record is
    # metadata, and these are megabytes.
    screenshot_bytes: list[bytes] = field(default_factory=list)
    html_bytes: bytes | None = None
    # The page's own images, by artifact kind. Fetched by the recipe that knows
    # which images on the page belong to it.
    images: dict[str, Any] = field(default_factory=dict)
    image_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("screenshot_bytes", None)
        data.pop("html_bytes", None)
        data.pop("images", None)
        return data


def navigation_metadata(
        response: Response) -> tuple[str | None, dict[str, str], dict[str, str]]:
    """Read and redact the navigation metadata Chromium makes available."""
    request = response.request

    def headers(values: Any) -> dict[str, str]:
        return {
            str(name).lower(): redact_header(str(name), str(value))
            for name, value in dict(values or {}).items()
        }

    return (
        getattr(request, "method", None),
        headers(getattr(request, "headers", {})),
        headers(getattr(response, "headers", {})),
    )


def _stored_html(source: str) -> bytes:
    """Serialize a page after dropping nodes marked as capture-only noise."""
    if ARTIFACT_ONLY not in source:
        return source.encode("utf-8")
    document = html.document_fromstring(source)
    for node in document.xpath(f"//*[@{ARTIFACT_ONLY}]"):
        parent = node.getparent()
        if parent is not None:
            parent.remove(node)
    return html.tostring(
        document, encoding="utf-8", method="html", doctype="<!DOCTYPE html>")


def scroll_through(page: Page, tempo: Any = None, shoot: Any = None,
                   budget: int = MAX_SCROLL_PX,
                   confirm_lazy: bool = False,
                   read_selector: str | None = None) -> Scroll:
    """Wheel to the bottom, then back to the top, so lazy content loads.

    Uses mouse.wheel rather than JS scrolling: it produces real input events,
    and it avoids evaluating script for something an actual reader does with a
    physical wheel. Height is re-measured each step because loading more content
    moves the bottom.

    A page that scrolls an element instead of the window is handled separately:
    the wheel has to be delivered over that element, and its own scrollTop is
    the only thing that says whether it worked.

    `shoot` is called with each step's distance, and with None for the screen
    the page starts on. Photographing here rather than afterwards is what keeps
    a page to one pass: the content is on screen at the moment it is wanted,
    which is where the browser loads its images, and a second walk down the same
    page to take the pictures would cost as much again.
    """
    with trace.phase(trace.WORK, "scroll page") as span:
        scroller = page.evaluate(FIND_SCROLLER)
        if scroller is not None:
            interaction.reactivate_pointer(
                page, (scroller.get("left", 0),
                       scroller.get("viewportTop", 0),
                       scroller.get("right", scroller["x"] * 2),
                       scroller.get("viewportBottom", scroller["y"] * 2)))
            pixels, steps, stopped, growing = _scroll_element(
                page, scroller, tempo, shoot, budget, confirm_lazy,
                read_selector)
            owner, kind = ELEMENT, "inner scroller"
        else:
            viewport = page.evaluate(PAGE_METRICS)
            interaction.reactivate_pointer(
                page, (0, 0, viewport["w"], viewport["v"]))
            pixels, steps, stopped, growing = _scroll_window(
                page, tempo, shoot, budget, confirm_lazy, read_selector)
            owner, kind = WINDOW, "window"
        span.detail = (f"{kind} · {pixels}/{budget}px in {steps} steps · "
                       f"stopped at {stopped}{', grew as we went' if growing else ''} · "
                       f"{WHEEL_NOTCH_PX}px notches")
    try:
        page.evaluate(CLEAR_SCROLLER)
    except Exception:
        pass
    return Scroll(pixels, stopped, growing, owner)


def _wheel(page: Page, pixels: int) -> None:
    """Deliver `pixels` as a wheel does, in notches rather than one shove."""
    notches = max(1, round(abs(pixels) / WHEEL_NOTCH_PX))
    step = WHEEL_NOTCH_PX if pixels >= 0 else -WHEEL_NOTCH_PX
    for index in range(notches):
        page.mouse.wheel(0, step)
        if index < notches - 1:
            time.sleep(random.triangular(
                *WHEEL_NOTCH_GAP_S, WHEEL_NOTCH_GAP_MODE_S))


def _flick_pixels(viewport: int) -> int:
    """How far one flick of the wheel goes: whole notches, at most one screen.

    At most one screen because a screen is also as much as a shot can hold, and
    a step past it would leave a strip between two pictures that nothing ever
    photographed. Whole notches because that is all the wheel can deliver, and
    a step rounded up would breach the screen it was capped to. The notch it
    varies by is what keeps every flick from being the same length.
    """
    ratio = random.triangular(*SCROLL_FLICK_RATIO, SCROLL_FLICK_MODE)
    notches = max(1, round(viewport * ratio / WHEEL_NOTCH_PX))
    return min(notches * WHEEL_NOTCH_PX,
               max(WHEEL_NOTCH_PX,
                   viewport // WHEEL_NOTCH_PX * WHEEL_NOTCH_PX))


def _flick_gap(tempo: Any) -> float:
    """How long to wait between one flick of the wheel and the next."""
    return (tempo.scroll_gap() if tempo is not None else random.triangular(
        *SCROLL_SETTLE_RANGE, SCROLL_SETTLE_MODE_S))


def _pointer_move_gap() -> int:
    return random.randint(*POINTER_MOVE_FLICK_GAP)


def _move_pointer_during_scroll(page: Page,
                                bounds: tuple[float, float, float, float],
                                steps: int, next_step: int) -> int:
    if steps < next_step:
        return next_step
    interaction.maybe_move_pointer(
        page, bounds, chance=POINTER_MOVE_CHANCE)
    return steps + _pointer_move_gap()


def _read_gap() -> int:
    return random.randint(*READ_FLICK_GAP)


def _pause_on_visible_item(page: Page, selector: str | None,
                           tempo: Any) -> bool:
    if selector is None or tempo is None or random.random() >= READ_PAUSE_CHANCE:
        return False
    try:
        lengths = page.evaluate(VISIBLE_ITEM_TEXT, selector)
    except Exception:
        return False
    if not lengths:
        return False
    chars = random.choice(lengths)
    seconds = tempo.read_pause(chars)
    with trace.phase(trace.WAIT, "read visible item",
                     f"{chars} chars · tempo '{tempo.name}'"):
        time.sleep(seconds)
    return True


def _read_during_scroll(page: Page, selector: str | None, tempo: Any,
                        steps: int, next_step: int) -> int:
    if steps < next_step:
        return next_step
    _pause_on_visible_item(page, selector, tempo)
    return steps + _read_gap()


def _at_end(state: dict) -> bool:
    return state["top"] + state["client"] >= state["height"] - 2


def _still_growing(page: Page, height: int) -> bool:
    """True if the scroller got taller while we waited, so there is more to come."""
    time.sleep(LAZY_CONFIRM_S)
    state = page.evaluate(SCROLLER_STATE)
    return bool(state and state["height"] > height)


def _extent(page: Page) -> tuple[int, int]:
    """How much page there is: (viewport height, document height).

    `CONTENT_BOTTOM` as well as the reported height, for the same reason the
    scroll loop consults it -- a body held to one viewport reports one however
    much is rendered below it, and that is exactly the page this is asked about.
    """
    try:
        metrics = page.evaluate(PAGE_METRICS)
        return int(metrics["v"]), int(max(metrics["h"],
                                          page.evaluate(CONTENT_BOTTOM)))
    except Exception:
        return 0, 0


def _window_still_growing(page: Page, height: int) -> bool:
    time.sleep(LAZY_CONFIRM_S)
    metrics = page.evaluate(PAGE_METRICS)
    return max(metrics["h"], page.evaluate(CONTENT_BOTTOM)) > height


def _scroll_element(page: Page, scroller: dict, tempo: Any = None,
                    shoot: Any = None, budget: int = MAX_SCROLL_PX,
                    confirm_lazy: bool = False,
                    read_selector: str | None = None) -> tuple[int, int, str, bool]:
    """Wheel an inner scroller, moving the pointer only if it is outside."""
    pointer_bounds = (
        scroller.get("left", 0), scroller.get("viewportTop", 0),
        scroller.get("right", scroller["x"] * 2),
        scroller.get("viewportBottom", scroller["y"] * 2))
    interaction.move_into(
        page, pointer_bounds,
        fallback=(scroller["x"], scroller["y"]))
    start = scroller["top"]
    position = start
    steps = 0
    stopped = DISTANCE
    tallest = scroller["height"]
    lazy = False
    next_pointer_step = _pointer_move_gap()
    next_read_step = _read_gap()
    if shoot is not None:
        configure = getattr(shoot, "for_scroller", None)
        if configure is not None:
            configure(scroller)
        shoot(None)

    for _ in range(MAX_SCROLL_STEPS):
        if position - start >= budget:
            break
        state = page.evaluate(SCROLLER_STATE)
        if state is None:
            stopped = STALLED
            break
        if state["height"] > tallest:
            tallest = state["height"]
            lazy = True
        if _at_end(state):
            # A feed reports the end while its next batch is still in flight,
            # and taking that at face value stops a page part way down while
            # calling it complete. Only a scroller that has already grown under
            # the wheel is given the benefit of the doubt, so a static page pays
            # nothing for the check.
            if not (confirm_lazy or lazy) or not _still_growing(
                    page, state["height"]):
                stopped = BOTTOM
                break
            continue

        _wheel(page, _flick_pixels(state["client"]))
        steps += 1
        time.sleep(_flick_gap(tempo))
        next_pointer_step = _move_pointer_during_scroll(
            page, pointer_bounds, steps, next_pointer_step)

        moved = page.evaluate(SCROLLER_STATE)
        if moved is None or moved["top"] <= position:
            position = moved["top"] if moved else position
            stopped = STALLED
            break
        next_read_step = _read_during_scroll(
            page, read_selector, tempo, steps, next_read_step)
        if shoot is not None:
            shoot(moved["top"] - position)
        position = moved["top"]

    _return_to_top(page)
    return int(max(0, position - start)), steps, stopped, lazy


def _scroll_window(page: Page, tempo: Any = None, shoot: Any = None,
                   budget: int = MAX_SCROLL_PX,
                   confirm_lazy: bool = False,
                   read_selector: str | None = None) -> tuple[int, int, str, bool]:
    total = 0
    steps = 0
    wheeled = False
    stopped = DISTANCE
    tallest = 0
    lazy = False
    next_pointer_step = _pointer_move_gap()
    next_read_step = _read_gap()
    if shoot is not None:
        shoot(None)
    for _ in range(MAX_SCROLL_STEPS):
        if total >= budget:
            break
        metrics = page.evaluate(PAGE_METRICS)
        content_bottom = page.evaluate(CONTENT_BOTTOM)
        extent = max(metrics["h"], content_bottom)
        if extent > tallest:
            lazy, tallest = tallest > 0, extent
        if metrics["y"] + metrics["v"] >= metrics["h"] - 2:
            # The reported height says we are done. Check whether content
            # actually continues past it: a page that constrains its body --
            # what a site does while a modal holds the scroll -- reports one
            # viewport while rendering several, and taking that at face value
            # is why such pages were never scrolled at all.
            if content_bottom <= metrics["y"] + metrics["v"] + 2:
                if ((confirm_lazy or lazy)
                        and _window_still_growing(page, extent)):
                    lazy = True
                    continue
                stopped = BOTTOM
                break

        _wheel(page, _flick_pixels(metrics["v"]))
        wheeled = True
        steps += 1
        time.sleep(_flick_gap(tempo))
        next_pointer_step = _move_pointer_during_scroll(
            page, (0, 0, metrics["w"], metrics["v"]),
            steps, next_pointer_step)

        # What the page actually did, not what we asked for. A page that
        # ignores the wheel would otherwise spend every remaining step
        # discovering that one wheel at a time.
        moved = page.evaluate("() => window.scrollY") - metrics["y"]
        if moved <= 0:
            stopped = STALLED
            break
        next_read_step = _read_during_scroll(
            page, read_selector, tempo, steps, next_read_step)
        if shoot is not None:
            shoot(moved)
        total += moved

    if wheeled:
        _return_to_top(page)
    return total, steps, stopped, lazy


def _return_to_top(page: Page) -> None:
    """Put the page back where it was found, whatever moved.

    Undoing the distance we accumulated is not enough. A wheel can scroll a
    nested container while `window.scrollY` stays at zero -- LinkedIn does
    exactly this -- so the loop above sees no movement, stops, and would leave
    a page that is visibly scrolled with nothing recorded to undo. The
    screenshot then captures the middle of the page.

    Home acts on whichever element is scrolling, so it needs no guess about
    which one that was, and it is one keystroke against the fifty-odd notches
    the same distance costs on the wheel. The wheel stays as the fallback for a
    page that ignores the key, overshooting so it clamps at the top.
    """
    try:
        page.keyboard.press("Home")
        time.sleep(random.uniform(*SCROLL_SETTLE_RANGE))
        if page.evaluate(AT_TOP):
            return
    except Exception:
        pass
    _wheel(page, -(_content_offset(page) + MAX_WHEEL_STEP_PX))
    time.sleep(random.uniform(*SCROLL_SETTLE_RANGE))


BLUR_FOCUS = """() => {
  const active = document.activeElement;
  if (active && active !== document.body && active.blur) active.blur();
  return true;
}"""


def _blur_focus(page: Page) -> None:
    """Take focus off whatever holds it, so Home scrolls the page.

    Opening the clipped "... more" text leaves focus inside the block that was
    expanded. `Home` then moves a caret rather than the page, and the sweep
    that follows starts wherever that block happens to be -- which is how a
    section's screenshot came back showing only its last two entries and the
    footer.
    """
    try:
        page.evaluate(BLUR_FOCUS)
    except Exception:
        pass


def rewind_to_top(page: Page) -> None:
    """Put a page back at its start, from outside a scroll.

    `_return_to_top` is only correct while a scroll is in progress: it asks
    `AT_TOP`, which looks for the `data-wf-scroller` marker, and every
    `scroll_through` clears that marker on its way out. Called between scrolls
    it therefore finds nothing, falls back to `window.scrollY` -- which is zero
    on a page whose content moves inside a container, as LinkedIn's does --
    and concludes it is already at the top while the container sits at the
    bottom.

    So find the scroller first, which re-marks it, and wheel it up over its own
    area. An ordinary discovered marker is taken off again afterwards. A
    caller-marked crop stays for the screenshot sweep that immediately follows;
    that sweep owns clearing it.
    """
    _blur_focus(page)
    try:
        scroller = page.evaluate(FIND_SCROLLER)
    except Exception:
        scroller = None
    try:
        if scroller is not None and scroller.get("top", 0) > 0:
            interaction.move_into(
                page,
                (scroller.get("left", 0), scroller.get("viewportTop", 0),
                 scroller.get("right", scroller["x"] * 2),
                 scroller.get("viewportBottom", scroller["y"] * 2)),
                fallback=(scroller["x"], scroller["y"]))
        _return_to_top(page)
    except Exception:
        # Never the reason a capture fails. A page that cannot be rewound is
        # photographed from wherever it is, which is the old behaviour and
        # still yields text and HTML.
        pass
    finally:
        if scroller is None or not scroller.get("crop"):
            try:
                page.evaluate(CLEAR_SCROLLER)
            except Exception:
                pass


def _content_offset(page: Page) -> int:
    try:
        return int(page.evaluate(CONTENT_TOP))
    except Exception:
        return 0


def _settle_images(page: Page) -> int:
    """Wait for the pictures on screen to arrive. Returns what never did."""
    try:
        page.wait_for_function(f"() => ({PENDING_IMAGES})() === 0",
                               timeout=IMAGE_SETTLE_MS)
        return 0
    except Exception:
        pass
    try:
        return int(page.evaluate(PENDING_IMAGES))
    except Exception:
        return 0


def _settle_height(page: Page, floor_s: float = 0.0) -> int:
    """Wait until the page stops getting taller. Returns how much it grew.

    `floor_s` is a minimum observation window: quiet before it has passed is
    not believed, because a page that has not begun loading its lower half
    reads as quiet too.
    """
    try:
        first = last = int(page.evaluate(CONTENT_EXTENT))
    except Exception:
        return 0
    started = time.monotonic()
    deadline = started + HEIGHT_SETTLE_BUDGET_S + floor_s
    quiet = 0
    while time.monotonic() < deadline:
        time.sleep(HEIGHT_QUIET_POLL_S)
        try:
            now = int(page.evaluate(CONTENT_EXTENT))
        except Exception:
            break
        quiet = quiet + 1 if now == last else 0
        last = now
        if quiet >= HEIGHT_QUIET_POLLS and time.monotonic() - started >= floor_s:
            break
    return max(0, last - first)


def _pending_visuals_expression(scope: Sequence[str] | None = None) -> str:
    """Build a bounded visual-readiness call without exposing page content."""
    selectors = None if scope is None else list(scope)
    return f"({_PENDING_VISUALS})({json.dumps(selectors)})"


def _settle_visuals(
    page: Page,
    timeout_ms: int = IMAGE_SETTLE_MS,
    *,
    scope: Sequence[str] | None = None,
) -> int:
    """Wait for visible progress and skeleton placeholders to clear."""
    expression = _pending_visuals_expression(scope)
    try:
        page.wait_for_function(f"() => ({expression}) === 0",
                               timeout=timeout_ms)
        return 0
    except Exception:
        pass
    try:
        return int(page.evaluate(expression))
    except Exception:
        return 0


def wait_for_visuals(
    page: Page,
    timeout_ms: int = PAGE_SETTLE_MS,
    *,
    scope: Sequence[str] | None = None,
) -> int:
    """Wait once for the arrived page to stop drawing placeholders.

    The per-shot guard inside `Screens` covers each screen as it is taken; this
    is the gate the main page never had. A profile's own sections mount after
    the document is interactive, so the first screen was routinely photographed
    over grey bars -- visible in the picture, and invisible in the journal
    because nothing recorded that the guard had found nothing to wait for.

    Bounded, and returns what was still drawing rather than raising: a page
    held open forever by one stuck placeholder costs the run, and an
    under-settled shot that says so is worth more than no shot at all.
    """
    with trace.phase(trace.NET, "await page content") as span:
        pending = _settle_visuals(page, timeout_ms, scope=scope)
        span.detail = (
            "no placeholders left on screen" if not pending
            else f"{pending} still drawing after {timeout_ms}ms")
    return pending


class Screens:
    """Photographs a page as it is scrolled, a screen at a time.

    A full-page shot asks the browser to stretch the window to the height of the
    whole document, and an application whose layout is sized to the window
    rearranges itself while that happens: the content area stays a screen tall
    with a hole under it, and the bars pinned to the window are painted a second
    time at the bottom. Photographing what is on screen never puts a page in
    that state.

    It also keeps the picture honest about what was read. A full-page shot takes
    in everything below the point the scroll stopped, and the images down there
    were never asked for, so they photograph as empty boxes that no amount of
    waiting will fill. This rides along with the scroll, so it covers the ground
    the scroll covered and nothing else.
    """

    def __init__(self, page: Page, path: Path,
                 strip: Callable[[Page], int] | None = None):
        self.page = page
        self.path = path
        self.strip = strip
        self.taken: list[Path] = []
        self.pinned = 0
        self.paused = 0
        self.stripped = 0
        self.error: str | None = None
        self.scroller: dict[str, int] | None = None
        try:
            screen = page.evaluate(SCREEN)
            self.window, self.width = int(screen["h"]), int(screen["w"])
        except Exception:
            self.window, self.width = 0, 0

    @property
    def usable(self) -> bool:
        return self.window > 0 and self.width > 0

    def for_scroller(self, scroller: dict) -> None:
        """Crop a narrow split-view capture to the panel that actually moves."""
        try:
            left = max(0, int(scroller["left"]))
            right = min(self.width, int(scroller["right"]))
            top = max(0, int(scroller["viewportTop"]))
            bottom = min(self.window, int(scroller["viewportBottom"]))
        except (KeyError, TypeError, ValueError):
            return
        if right <= left or bottom <= top:
            return
        # Specialized split views opt into cropping when they mark their
        # scroller. Generic page shells retain their surrounding context,
        # independent of how wide either one is on this machine.
        if not scroller.get("crop", False):
            return
        self.scroller = {
            "left": left,
            "right": right,
            "top": top,
            "bottom": bottom,
        }

    def __call__(self, moved: int | None) -> None:
        """Take the screen the page is on. `moved` is None for the first."""
        if not self.usable:
            return
        frame = self.scroller
        frame_height = frame["bottom"] - frame["top"] if frame else self.window
        if moved is None:
            height = frame_height
            top = frame["top"] if frame else 0
        else:
            # Only the strip that arrived is new. Taking the whole screen again
            # would repeat what the one above it already holds, which is what
            # makes them join into a page rather than a pile of pictures.
            height = min(max(int(moved), MIN_SCREEN_PX), frame_height)
            top = (frame["bottom"] if frame else self.window) - height
        with trace.phase(trace.WORK, "screenshot") as span:
            # From the first screen, unlike the hiding below: this is not
            # wanted in any of them. Repeated because a page is free to mount
            # more of it while being read.
            self.stripped += _strip(self.page, self.strip)
            # Before every screen and undone after it. A bar that only exists
            # once a page has been scrolled past its own header is not there to
            # be found before it has been, and leaving anything hidden would
            # change the text and HTML that are read from the page later.
            hidden = 0 if moved is None else _hide_pinned(self.page)
            self.pinned += hidden
            self.paused += _pause_videos(self.page)
            try:
                # Height first. The other two wait on things that are on
                # screen, and until the page has stopped growing, what is on
                # screen is not yet what this strip is meant to hold.
                grew = _settle_height(
                    self.page,
                    floor_s=LAZY_CONFIRM_S if moved is None else 0.0)
                unfilled = _settle_images(self.page)
                drawing = _settle_visuals(self.page)
                self._shoot(top, height)
            finally:
                if hidden:
                    try:
                        self.page.evaluate(SHOW_PINNED)
                    except Exception:
                        pass
            # The residuals are named, not swallowed. Both settles returned a
            # count that nothing read, so a screen photographed over grey bars
            # was indistinguishable in the journal from one taken over a page
            # that had finished -- which is how this went unnoticed until the
            # pictures were looked at.
            unsettled = "".join(
                f" · {count} {what}" for count, what in
                ((drawing, "still drawing"), (unfilled, "images unfilled"))
                if count)
            if grew:
                unsettled += f" · grew {grew}px first"
            span.detail = (f"screen {len(self.taken)} · {height}px · "
                           f"{hidden} pinned hidden · {self.paused} videos paused"
                           f" · {self.stripped} stripped{unsettled}")

    def _shoot(self, top: int, height: int) -> None:
        target = _screen_path(self.path, len(self.taken))
        frame = self.scroller
        left = frame["left"] if frame else 0
        width = frame["right"] - left if frame else self.width
        try:
            self.page.screenshot(
                path=str(target),
                clip={"x": left, "y": top, "width": width, "height": height},
                timeout=SHOT_TIMEOUT_MS)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return
        self.taken.append(target)

    def discard(self) -> None:
        """Throw away what was taken, for a sweep that has to be done again."""
        for screen in self.taken:
            screen.unlink(missing_ok=True)
        self.taken = []
        self.error = None

    def join(self) -> tuple[list[str], bool]:
        """The page as one image, and whether it stops short of what was read."""
        if not self.taken:
            return [], False
        return _join_screens(self.taken, self.path), self.error is not None


def _strip(page: Page, strip: Callable[[Page], int] | None) -> int:
    """Take off whatever the caller says must never be stored.

    What that is belongs to the caller. A caller passing nothing gets a page
    left exactly as it was found.
    """
    if strip is None:
        return 0
    try:
        return int(strip(page))
    except Exception:
        return 0


def _hide_pinned(page: Page) -> int:
    try:
        return int(page.evaluate(HIDE_PINNED))
    except Exception:
        return 0


def _pause_videos(page: Page) -> int:
    try:
        return int(page.evaluate(PAUSE_VIDEOS))
    except Exception:
        return 0


def _screen_path(path: Path, index: int) -> Path:
    """Screens are working files: they are joined and then removed."""
    return path.with_name(f"{path.stem}-screen-{index + 1:02d}.png")


def _join_screens(screens: list[Path], target: Path) -> list[str]:
    """One image of the page, from the screens it was read in.

    A page has to be photographed a screen at a time, but nothing downstream
    should have to know that: one page is one picture, the way it was before any
    of this, and the reader scrolls the image rather than a list of them. The
    screens are working files and go once they are in it.
    """
    try:
        images = [Image.open(screen) for screen in screens]
        page = Image.new("RGB", (max(image.width for image in images),
                                 sum(image.height for image in images)), "white")
        top = 0
        for image in images:
            page.paste(image, (0, top))
            top += image.height
            image.close()
        page.save(target)
    except Exception:
        # A picture in pieces beats no picture. The names are returned as they
        # are and the run carries on.
        return [screen.name for screen in screens]
    for screen in screens:
        screen.unlink(missing_ok=True)
    return [target.name]


def _band_path(path: Path, index: int) -> Path:
    """First band keeps the plain name, so a normal page is unchanged."""
    return path if index == 0 else path.with_name(f"{path.stem}-{index + 1:02d}.png")


def _bands(page: Page, path: Path, height: int, width: int) -> tuple[list[str], bool]:
    """Capture a very tall page in slices. Returns the files and whether it ran out."""
    written: list[str] = []
    for index in range(MAX_BANDS):
        top = index * BAND_PX
        if top >= height:
            return written, False
        target = _band_path(path, index)
        try:
            page.screenshot(
                path=str(target), full_page=True,
                clip={"x": 0, "y": top, "width": width,
                      "height": min(BAND_PX, height - top)},
                timeout=SHOT_TIMEOUT_MS,
            )
        except Exception:
            return written, True
        written.append(target.name)
    return written, MAX_BANDS * BAND_PX < height


def _screenshot(page: Page, path: Path) -> tuple[list[str], bool]:
    """Capture the page. Returns the files written, in page order, and whether
    anything was left out.

    This is for a page nothing scrolled, where the whole document in one shot is
    the right answer: there is no ground a scroll could have covered and failed
    to photograph, and no lazy content left unasked for. A page that was
    scrolled is photographed by `Screens`, as it is read.
    """
    # A full-page screenshot captures the document, so content sitting inside a
    # clipped element is not in it however much of it rendered. Opening the clip
    # for the duration of the shot is the difference between one viewport of a
    # LinkedIn profile and the whole thing -- measured 2638px to 5304px.
    try:
        unclipped = page.evaluate(UNCLIP)
    except Exception:
        unclipped = 0
    if unclipped:
        with trace.phase(trace.WAIT, "unclip reflow",
                         f"opened {unclipped} clipping ancestors, letting layout settle"):
            time.sleep(random.uniform(0.4, 0.8))

    try:
        try:
            metrics = page.evaluate(PAGE_METRICS)
            height, width = int(metrics["h"]), int(metrics["w"])
        except Exception:
            height, width = 0, 0

        with trace.phase(trace.WORK, "screenshot") as span:
            if height > BAND_PX and width:
                written, truncated = _bands(page, path, height, width)
                if written:
                    span.detail = f"{len(written)} bands · {height}px tall"
                    return written, truncated

            try:
                page.screenshot(path=str(path), full_page=True,
                                timeout=SHOT_TIMEOUT_MS)
                span.detail = f"full page · {height}px tall"
                return [path.name], False
            except Exception:
                # One retry, viewport only. It answers both ways this fails:
                # compositing a very tall page can give out where a single
                # viewport will not, and a page still settling has had the
                # first attempt's budget to finish doing so.
                page.screenshot(path=str(path), timeout=FALLBACK_TIMEOUT_MS)
                span.detail = f"viewport only · full page failed at {height}px"
                return [path.name], bool(height and height > _viewport(page))
    finally:
        if unclipped:
            # The text and HTML captures run before this now, so the page is
            # already recorded as served; put it back anyway for whatever reads
            # it next.
            try:
                page.evaluate(RECLIP)
            except Exception:
                pass


def _viewport(page: Page) -> int:
    try:
        return int(page.evaluate(PAGE_METRICS)["v"])
    except Exception:
        return 0


def capture_page(
    page: Page,
    out_dir: Path,
    slug: str,
    *,
    url_requested: str,
    response: Response | None = None,
    scroll: bool = True,
    dismiss: Callable[[Page], list[str]] | None = None,
    tempo: Any = None,
    scroll_budget: int = MAX_SCROLL_PX,
    confirm_lazy: bool = False,
    read_selector: str | None = None,
    strip: Callable[[Page], int] | None = None,
    extract_content: Callable[[Page], tuple[str, bytes]] | None = None,
    scroller: Callable[..., Scroll] | None = None,
) -> PageCapture:
    """Screenshot, text and HTML for whatever is currently on `page`.

    `dismiss` runs again after scrolling. Newsletter and exit-intent popups are
    triggered by scrolling, so they do not exist yet when the caller clears the
    banners that arrived with the page.

    `strip` takes off whatever must not be kept. Called before each screen and
    again before the page is read, so that nothing mounting late is stored.
    `extract_content` can narrow text and HTML to the useful part of a split
    view instead of storing the entire document around it.

    `scroller` replaces how the page is travelled, for a site whose scrolling
    does not behave the way this module's default assumes. It takes and returns
    exactly what `scroll_through` does. Omitting it runs `scroll_through` itself,
    unchanged.
    """
    started = time.monotonic()
    out_dir.mkdir(parents=True, exist_ok=True)
    capture = PageCapture(
        url_requested=url_requested,
        captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )

    shot = out_dir / f"{slug}.png"
    screens: Screens | None = None
    try:
        # Before the page is touched, so an unscrolled one is as clean as a
        # scrolled one.
        _strip(page, strip)
        if scroll:
            # Start from the top, whatever left the page scrolled. The sweep
            # photographs each screen as it passes, so it captures the page
            # from wherever it begins -- and by this point the list has been
            # exhausted and every clipped block opened, both of which end far
            # down the page. A section that grew from 10 entries to 22 was
            # photographed from its own footer.
            rewind_to_top(page)
            # The pictures are taken by the sweep rather than by a second walk
            # down the same page: the content is on screen at the moment each is
            # wanted, which is where a browser loads its images.
            screens = Screens(page, shot, strip=strip)
            scroll_options: dict[str, Any] = {
                "budget": scroll_budget,
                "confirm_lazy": confirm_lazy,
            }
            if read_selector is not None:
                scroll_options["read_selector"] = read_selector
            run_scroll = scroller or scroll_through
            scrolled = run_scroll(
                page, tempo, shoot=screens, **scroll_options)
            capture.scrolled_px = scrolled.pixels
            capture.scroll_stopped = scrolled.stopped
            capture.scroll_growing = scrolled.growing
            if dismiss is not None:
                capture.dismissed_late = dismiss(page)
                if capture.dismissed_late:
                    # A modal holding the scroll lock means the pass above
                    # travelled nowhere and no lazy content loaded. Once only,
                    # so a site that spawns a popup per scroll cannot loop.
                    # Its pictures go with it: they are of the modal.
                    screens.discard()
                    scrolled = run_scroll(
                        page, tempo, shoot=screens, **scroll_options)
                    capture.scrolled_px += scrolled.pixels
                    capture.scroll_stopped = scrolled.stopped
                    capture.scroll_growing = scrolled.growing

            viewport, height = _extent(page)
            if defeated(scrolled, viewport=viewport, height=height):
                # Still held, either because nothing here presses whatever is
                # holding it or because pressing it did not let go. The screens
                # are of a page that never moved; dropping them leaves the
                # full-page shot below as the one that runs.
                screens.discard()
                if dismiss is not None and not capture.dismissed_late:
                    # Say what is still there. A rule we do not have is
                    # otherwise invisible, and every capture of that host is
                    # quietly short.
                    overlays.undismissed(page)

        # Again, because the sweep has redrawn the page since the first pass
        # and this read is what decides what the text and the HTML carry.
        _strip(page, strip)

        capture.final_url = page.url
        capture.title = page.title()
        if response is not None:
            capture.status = response.status
            (capture.request_method, capture.request_headers,
             capture.response_headers) = navigation_metadata(response)

        with trace.phase(trace.WORK, "extract text") as span:
            # The rendered text is kept, not just measured. It is garbled --
            # nav, cookie text and footers run together with the content -- and
            # a markdown parse of the stored HTML replaces it. Until then it is
            # what makes captured pages searchable.
            if extract_content is None:
                capture.text = page.inner_text("body")
                extracted_html = None
            else:
                capture.text, extracted_html = extract_content(page)
            capture.text_chars = len(capture.text)
            span.detail = f"{capture.text_chars} chars"

        with trace.phase(trace.WORK, "extract html") as span:
            if extract_content is not None:
                capture.html_bytes = extracted_html
            else:
                capture.html_bytes = _stored_html(page.content())
            span.detail = f"{len(capture.html_bytes)} bytes"

        capture.ok = True
    except Exception as exc:  # a failed page is data, not a crash
        capture.error = f"{type(exc).__name__}: {exc}"

    # Joined here rather than during the sweep, and a page nothing scrolled is
    # photographed here too. Either way it is last and on its own: a screenshot
    # is the one step that waits on the page rather than reading it, so it is
    # the one that can fail, and a failure here must not throw away text and
    # HTML that have already been taken.
    #
    # Playwright screenshots through a path, so the images land in the scratch
    # directory, are read back, and are removed: nothing is left behind in a run
    # directory, because there are no run directories.
    if capture.ok:
        try:
            if screens is not None and screens.taken:
                parts, truncated = screens.join()
                capture.screenshot_error = screens.error
            else:
                parts, truncated = _screenshot(page, shot)
            capture.screenshot = parts[0] if parts else None
            capture.screenshot_parts = parts
            capture.screenshot_truncated = truncated
            for name in parts:
                image = out_dir / name
                capture.screenshot_bytes.append(image.read_bytes())
                image.unlink(missing_ok=True)
        except Exception as exc:
            # A page we could not photograph is still a page we read. Recorded
            # so a missing image is visible as one, rather than looking like a
            # page nobody asked for a screenshot of.
            capture.screenshot_error = f"{type(exc).__name__}: {exc}"

    capture.load_ms = int((time.monotonic() - started) * 1000)
    return capture
