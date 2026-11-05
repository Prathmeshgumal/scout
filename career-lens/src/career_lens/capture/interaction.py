"""Mouse interactions that approach controls instead of jumping to them."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from patchright.sync_api import Locator, Page

# Camoufox dispatches trajectory points roughly 10ms apart. Keep the same order
# of magnitude while bounding each move so crossing a large window cannot hold
# up a run for seconds.
POINT_INTERVAL_S = 0.012
MIN_MOVE_S = 0.07
MAX_MOVE_S = 0.42
POINTER_SPEED_PX_S = 2400.0
MIN_POINTS = 6
MAX_POINTS = 36
CLICK_HOLD_MS = (45, 130)
# Keep clicks away from a control's edge, where a small layout shift can put
# the pointer outside it, while avoiding the same exact centre every time.
CLICK_OFFSET_RATIO = 0.28
MAX_CLICK_OFFSET_X_PX = 24.0
MAX_CLICK_OFFSET_Y_PX = 10.0
EDGE_MARGIN_PX = 6.0
LOOSE_OFFSET_RATIO = 0.34
LOOSE_MAX_OFFSET_X_PX = 140.0
LOOSE_MAX_OFFSET_Y_PX = 48.0
LOOSE_MODE_JITTER = 0.45
POINTER_MARGIN_PX = 12
POINTER_REACTIVATE_PX = (5.0, 14.0)
IDLE_MOVE_CHANCE = 0.22
IDLE_MOVE_DISTANCE_PX = (70.0, 220.0)
IDLE_MOVE_DISTANCE_MODE_PX = 130.0
IDLE_MOVE_MIN_PX = 8.0
WHEEL_APPROACH_PX = 120
WHEEL_NOTCH_PX = 114
MAX_WHEEL_NOTCHES = 7
MAX_SCROLL_FLICKS = 24
SCROLL_GAP_S = (0.10, 0.28)
SCROLL_TIMEOUT_S = 8.0
TARGET_TOP_MARGIN_PX = 24
TARGET_BOTTOM_MARGIN_PX = 80

VIEWPORT = "() => ({w: window.innerWidth, h: window.innerHeight})"
SCROLL_CONTEXT = """(el, pointer) => {
    const bounds = node => {
        const rect = node.getBoundingClientRect();
        return {left: Math.max(0, rect.left), top: Math.max(0, rect.top),
                right: Math.min(innerWidth, rect.right),
                bottom: Math.min(innerHeight, rect.bottom)};
    };
    const canScroll = node => node.scrollHeight > node.clientHeight + 2;
    const acceptsWheel = node =>
        /(auto|scroll|overlay)/i.test(getComputedStyle(node).overflowY);
    const ownsPageScroll = node =>
        node.matches && node.matches('main#workspace, [data-wf-scroller]');
    const selector = node => {
        if (!node) return null;
        let value = node.tagName.toLowerCase();
        if (node.id) value += `#${node.id}`;
        const role = node.getAttribute('role');
        if (role) value += `[role="${role}"]`;
        const testid = node.getAttribute('data-testid');
        if (testid) value += `[data-testid="${testid}"]`;
        return value;
    };
    const describe = node => {
        if (!node) return null;
        const style = getComputedStyle(node);
        return {
            selector: selector(node),
            overflowY: style.overflowY,
            scrollTop: node.scrollTop,
            scrollHeight: node.scrollHeight,
            clientHeight: node.clientHeight,
        };
    };
    const wheelPoint = node => {
        const rect = bounds(node);
        const margin = 12;
        const left = Math.min(rect.right, rect.left + margin);
        const right = Math.max(rect.left, rect.right - margin);
        const top = Math.min(rect.bottom, rect.top + margin);
        const bottom = Math.max(rect.top, rect.bottom - margin);
        if (right <= left || bottom <= top) return null;

        const origin = {
            x: Math.min(right, Math.max(left, pointer?.x ?? (left + right) / 2)),
            y: Math.min(bottom, Math.max(top, pointer?.y ?? (top + bottom) / 2)),
        };
        const belongsToOwner = point => {
            const hit = document.elementFromPoint(point.x, point.y);
            return Boolean(hit && (hit === node || node.contains(hit)));
        };
        if (belongsToOwner(origin)) return origin;

        // A sticky header or sibling can cover the scroller's rectangle. Find
        // the nearest visible point whose hit target is genuinely owned by the
        // scroller so the browser routes wheel input to the intended element.
        let best = null;
        const consider = point => {
            if (!belongsToOwner(point)) return;
            const distance = Math.hypot(point.x - origin.x, point.y - origin.y);
            if (!best || distance < best.distance)
                best = {...point, distance};
        };
        const xCandidates = [origin.x, (left + right) / 2,
                             left + (right - left) / 4,
                             right - (right - left) / 4];
        for (let y = top; y <= bottom; y += 24) {
            for (const x of xCandidates) consider({x, y});
        }
        for (const x of xCandidates) consider({x, y: bottom});
        return best ? {x: best.x, y: best.y} : null;
    };
    const context = node => ({
        ...bounds(node), owner: describe(node), wheelPoint: wheelPoint(node),
    });

    // Route the wheel through the closest ancestor that can actually consume
    // it. This follows DOM ownership, so a narrow results pane and a full-page
    // application shell work identically at every viewport size.
    for (let node = el.parentElement; node; node = node.parentElement) {
        if (canScroll(node) && (acceptsWheel(node) || ownsPageScroll(node)))
            return context(node);
    }
    const root = document.scrollingElement;
    return {left: 0, top: 0, right: innerWidth, bottom: innerHeight,
            owner: describe(root), wheelPoint: wheelPoint(root)};
}"""
PAGE_SCROLL_STATE = """() => {
    const root = document.scrollingElement;
    const marked = document.querySelector('[data-wf-scroller]');
    const state = node => {
        if (!node || node.scrollTop <= 2) return null;
        if (node === root)
            return {scrollTop: node.scrollTop, left: 0, top: 0,
                    right: innerWidth, bottom: innerHeight};
        const rect = node.getBoundingClientRect();
        const left = Math.max(0, rect.left), top = Math.max(0, rect.top);
        const right = Math.min(innerWidth, rect.right);
        const bottom = Math.min(innerHeight, rect.bottom);
        if (right <= left || bottom <= top) return null;
        return {scrollTop: node.scrollTop, left, top, right, bottom,
                area: (right - left) * (bottom - top)};
    };
    const markedState = state(marked);
    if (markedState) return markedState;

    const candidates = [root, ...document.querySelectorAll('*')];
    let best = null;
    for (const node of candidates) {
        if (node !== root) {
            const style = getComputedStyle(node);
            const ownsPageScroll = node.matches('main#workspace');
            if (!ownsPageScroll
                    && !/(auto|scroll|overlay)/i.test(style.overflowY)) continue;
        }
        const candidate = state(node);
        if (!candidate) continue;
        candidate.area ??= innerWidth * innerHeight;
        if (!best || candidate.area > best.area) best = candidate;
    }
    if (best) delete best.area;
    return best;
}"""

@dataclass(frozen=True)
class Aim:
    """How loosely a click is aimed inside the control it is aimed at.

    `ratio` and the two caps set how far from centre a click may land;
    `margin_px` is the strip along each edge it may never land in, and wins
    over the spread on a control too small to give away. `mode_jitter` is how
    much of that spread the hand's own bias may move the cluster by.
    """
    ratio: float
    max_x: float
    max_y: float
    margin_px: float = 0.0
    mode_jitter: float = 0.0


#: What every caller got before aiming was a parameter, and what LinkedIn's
#: whole click surface still gets. Do not widen it here: pass a different Aim.
DEFAULT_AIM = Aim(ratio=CLICK_OFFSET_RATIO, max_x=MAX_CLICK_OFFSET_X_PX,
                  max_y=MAX_CLICK_OFFSET_Y_PX)

#: A hand rather than a caliper. Capped by the control's own size, so it uses
#: a third of a wide button and stays well inside a small one. Nobody clicks
#: the same 13% of a 360px button every time, which is what DEFAULT_AIM does.
LOOSE_AIM = Aim(ratio=LOOSE_OFFSET_RATIO, max_x=LOOSE_MAX_OFFSET_X_PX,
                max_y=LOOSE_MAX_OFFSET_Y_PX, margin_px=EDGE_MARGIN_PX,
                mode_jitter=LOOSE_MODE_JITTER)

_POSITIONS: WeakKeyDictionary[Any, tuple[float, float]] = WeakKeyDictionary()
_AIM_BIAS: WeakKeyDictionary[Any, tuple[float, float]] = WeakKeyDictionary()


def aiming_bias(page: Page, *, rng: Any = random) -> tuple[float, float]:
    """This browser's own aim, as a fraction of the spread in each axis.

    Drawn once and kept, because a person's aim is consistently off-centre in
    one direction rather than re-rolled for every click. Without it each click
    is independently centred and the sequence averages out to dead centre --
    which is the tell, not any single click.
    """
    bias = _AIM_BIAS.get(page)
    if bias is None:
        bias = (rng.triangular(-1.0, 1.0, 0.0), rng.triangular(-1.0, 1.0, 0.0))
        _AIM_BIAS[page] = bias
    return bias


def pointer_position(page: Page) -> tuple[float, float] | None:
    """Return the last virtual pointer position recorded for this page."""
    return _POSITIONS.get(page)


def _point_count(distance: float) -> tuple[int, float]:
    duration = min(MAX_MOVE_S, max(MIN_MOVE_S,
                                   MIN_MOVE_S + distance / POINTER_SPEED_PX_S))
    count = min(MAX_POINTS, max(MIN_POINTS, round(duration / POINT_INTERVAL_S)))
    return count, duration


def trajectory(start: tuple[float, float], end: tuple[float, float], *,
               rng: Any = random) -> list[tuple[float, float]]:
    """Return a curved, eased path ending exactly at ``end``."""
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if distance < 2:
        return [end]

    nx, ny = -dy / distance, dx / distance
    bend = rng.triangular(-min(90.0, distance * 0.16),
                          min(90.0, distance * 0.16), 0.0)
    first = rng.uniform(0.20, 0.38)
    second = rng.uniform(0.62, 0.84)
    tail = rng.uniform(0.35, 0.85)
    c1 = (sx + dx * first + nx * bend,
          sy + dy * first + ny * bend)
    c2 = (sx + dx * second + nx * bend * tail,
          sy + dy * second + ny * bend * tail)

    count, _ = _point_count(distance)
    points = []
    for index in range(1, count + 1):
        t = index / count
        # Smoothstep gives the path a quick middle and slower approach at both
        # ends instead of moving every point at one mechanical speed.
        u = t * t * (3 - 2 * t)
        inverse = 1 - u
        x = (inverse ** 3 * sx
             + 3 * inverse ** 2 * u * c1[0]
             + 3 * inverse * u ** 2 * c2[0]
             + u ** 3 * ex)
        y = (inverse ** 3 * sy
             + 3 * inverse ** 2 * u * c1[1]
             + 3 * inverse * u ** 2 * c2[1]
             + u ** 3 * ey)
        points.append((max(0.0, x), max(0.0, y)))
    points[-1] = end
    return points


def move_to(page: Page, x: float, y: float, *, rng: Any = random,
            pace: float = 1.0) -> None:
    """Move Patchright's virtual pointer to a point along a natural path.

    `pace` multiplies the time spent between points without changing the path.
    Above 1.0 is an unhurried move -- a pointer wandering a page rather than
    one on its way to a control it has already decided to click.
    """
    end = (float(x), float(y))
    start = _POSITIONS.get(page, (0.0, 0.0))
    points = trajectory(start, end, rng=rng)
    distance = math.dist(start, end)
    _, duration = _point_count(distance)
    gap = duration * max(0.0, pace) / max(1, len(points))

    for index, point in enumerate(points):
        page.mouse.move(*point)
        _POSITIONS[page] = point
        if index < len(points) - 1:
            time.sleep(gap * rng.uniform(0.85, 1.15))


def ensure_pointer(page: Page, x: float, y: float, *, rng: Any = random) -> bool:
    """Place a pointer only when this tab has never received mouse input."""
    if page in _POSITIONS:
        return False
    move_to(page, x, y, rng=rng)
    return True


def ensure_pointer_in_viewport(page: Page, *, rng: Any = random) -> bool:
    """Initialize a fresh tab inside its central viewport region."""
    size = page.evaluate(VIEWPORT)
    return ensure_pointer(
        page,
        rng.uniform(size["w"] * 0.25, size["w"] * 0.75),
        rng.uniform(size["h"] * 0.25, size["h"] * 0.75),
        rng=rng,
    )


def reactivate_pointer(page: Page,
                       bounds: tuple[float, float, float, float], *,
                       rng: Any = random) -> bool:
    """Emit a small move for a pointer preserved across document navigation."""
    current = _POSITIONS.get(page)
    if current is None:
        return False

    left, top, right, bottom = bounds
    margin_x = min(POINTER_MARGIN_PX, max(0.0, (right - left) / 3))
    margin_y = min(POINTER_MARGIN_PX, max(0.0, (bottom - top) / 3))
    x0, x1 = left + margin_x, right - margin_x
    y0, y1 = top + margin_y, bottom - margin_y
    distance = rng.uniform(*POINTER_REACTIVATE_PX)
    angle = rng.uniform(0, math.tau)
    x = min(max(current[0] + math.cos(angle) * distance, x0), x1)
    y = min(max(current[1] + math.sin(angle) * distance, y0), y1)

    # At an edge the random direction can clamp both coordinates back to the
    # same point. Move inward in that rare case so the new document receives a
    # real mousemove event and can reveal an opt-in debug cursor.
    if math.dist(current, (x, y)) < 2:
        x = min(max(current[0] + (distance if current[0] <= (x0 + x1) / 2
                                 else -distance), x0), x1)
        y = min(max(current[1] + (distance if current[1] <= (y0 + y1) / 2
                                 else -distance), y0), y1)

    move_to(page, x, y, rng=rng)
    return True


def maybe_move_pointer(page: Page,
                       bounds: tuple[float, float, float, float], *,
                       chance: float = IDLE_MOVE_CHANCE,
                       rng: Any = random, pace: float = 1.0) -> bool:
    """Occasionally drift a known pointer within the visible safe region."""
    current = _POSITIONS.get(page)
    if current is None or rng.random() >= chance:
        return False

    left, top, right, bottom = bounds
    margin_x = min(POINTER_MARGIN_PX, max(0.0, (right - left) / 3))
    margin_y = min(POINTER_MARGIN_PX, max(0.0, (bottom - top) / 3))
    x0, x1 = left + margin_x, right - margin_x
    y0, y1 = top + margin_y, bottom - margin_y
    if x1 <= x0 or y1 <= y0:
        return False
    if not (x0 <= current[0] <= x1 and y0 <= current[1] <= y1):
        return False

    distance = rng.triangular(
        *IDLE_MOVE_DISTANCE_PX, IDLE_MOVE_DISTANCE_MODE_PX)
    angle = rng.uniform(0, math.tau)
    x = min(max(current[0] + math.cos(angle) * distance, x0), x1)
    y = min(max(current[1] + math.sin(angle) * distance, y0), y1)
    if math.dist(current, (x, y)) < IDLE_MOVE_MIN_PX:
        centre_x, centre_y = (x0 + x1) / 2, (y0 + y1) / 2
        scale = IDLE_MOVE_MIN_PX / max(
            IDLE_MOVE_MIN_PX, math.dist(current, (centre_x, centre_y)))
        x = current[0] + (centre_x - current[0]) * scale
        y = current[1] + (centre_y - current[1]) * scale
    if math.dist(current, (x, y)) < 2:
        return False

    move_to(page, x, y, rng=rng, pace=pace)
    return True


def move_into(page: Page, bounds: tuple[float, float, float, float], *,
              fallback: tuple[float, float], rng: Any = random) -> bool:
    """Put the pointer inside a wheel target only when it is currently outside."""
    left, top, right, bottom = bounds
    margin_x = min(POINTER_MARGIN_PX, max(0.0, (right - left) / 3))
    margin_y = min(POINTER_MARGIN_PX, max(0.0, (bottom - top) / 3))
    x0, x1 = left + margin_x, right - margin_x
    y0, y1 = top + margin_y, bottom - margin_y
    current = _POSITIONS.get(page)
    if current is None:
        move_to(page, *fallback, rng=rng)
        return True
    if x0 <= current[0] <= x1 and y0 <= current[1] <= y1:
        return False

    x = min(max(current[0], x0), x1)
    y = min(max(current[1], y0), y1)
    move_to(page, x, y, rng=rng)
    return True


def _scroll_delta(box: dict[str, float], viewport_height: float, *,
                  context_top: float = 0.0,
                  context_bottom: float | None = None) -> float:
    bottom_edge = (viewport_height if context_bottom is None
                   else context_bottom)
    visible_height = max(0.0, bottom_edge - context_top)
    top = context_top + min(float(TARGET_TOP_MARGIN_PX), visible_height / 4)
    bottom = bottom_edge - min(float(TARGET_BOTTOM_MARGIN_PX),
                               visible_height / 4)
    if box["height"] > bottom - top:
        return box["y"] + box["height"] / 2 - (top + bottom) / 2
    if box["y"] < top:
        return box["y"] - top
    if box["y"] + box["height"] > bottom:
        return box["y"] + box["height"] - bottom
    return 0.0


def _wheel_bounds(context: dict[str, float], box: dict[str, float],
                  viewport: dict[str, float]) -> tuple[float, float, float, float]:
    """The visible scroller strip directly above or below an offscreen target."""
    left = max(context["left"], max(0.0, box["x"]))
    right = min(context["right"],
                min(viewport["w"], box["x"] + box["width"]))
    if right - left < 2:
        left, right = context["left"], context["right"]

    top, bottom = context["top"], context["bottom"]
    visible_top = max(top, box["y"])
    visible_bottom = min(bottom, box["y"] + box["height"])
    if visible_bottom - visible_top >= 2:
        top, bottom = visible_top, visible_bottom
    elif box["y"] >= bottom:
        top = max(top, bottom - WHEEL_APPROACH_PX)
    elif box["y"] + box["height"] <= top:
        bottom = min(bottom, top + WHEEL_APPROACH_PX)
    return left, top, right, bottom


def _scroll_failure_detail(context: dict[str, Any]) -> str:
    """Identify the scroll owner without retaining a browser-state dump."""
    owner = context.get("owner") or {}
    selector = owner.get("selector") or "document"
    top = owner.get("scrollTop")
    height = owner.get("scrollHeight")
    client = owner.get("clientHeight")
    if all(isinstance(value, (int, float))
           for value in (top, height, client)):
        return f"owner={selector} · scroll={top}/{max(0, height - client)}"
    return f"owner={selector}"


def scroll_to_target(page: Page, target: Locator, *, timeout: int | None = None,
                     rng: Any = random) -> dict[str, float] | None:
    """Bring a target onscreen with wheel events and return its final box."""
    action = {} if timeout is None else {"timeout": timeout}
    deadline = time.monotonic() + min(
        SCROLL_TIMEOUT_S, timeout / 1000 if timeout is not None else SCROLL_TIMEOUT_S)
    previous_y = None
    stalled = 0

    for _ in range(MAX_SCROLL_FLICKS):
        box = target.bounding_box(**action)
        if not box:
            return None
        viewport = page.evaluate(VIEWPORT)
        try:
            pointer = pointer_position(page)
            context = target.evaluate(
                SCROLL_CONTEXT,
                None if pointer is None else {"x": pointer[0], "y": pointer[1]},
            )
        except Exception:
            context = None
        context = context or {"left": 0, "top": 0,
                              "right": viewport["w"], "bottom": viewport["h"]}
        delta = _scroll_delta(
            box, viewport["h"], context_top=context["top"],
            context_bottom=context["bottom"])
        if abs(delta) <= 2:
            return box
        if time.monotonic() >= deadline:
            detail = _scroll_failure_detail(context)
            raise RuntimeError(
                f"timed out while wheel-scrolling to target: {detail}")

        if previous_y is not None and abs(box["y"] - previous_y) <= 2:
            stalled += 1
            if stalled >= 2:
                detail = _scroll_failure_detail(context)
                raise RuntimeError(
                    f"wheel scrolling did not move the target: {detail}")
        else:
            stalled = 0
        previous_y = box["y"]

        wheel_point = context.get("wheelPoint")
        if wheel_point is not None:
            current = pointer_position(page)
            destination = (float(wheel_point["x"]), float(wheel_point["y"]))
            if current is None or math.dist(current, destination) >= 2:
                move_to(page, *destination, rng=rng)
        else:
            wheel_bounds = _wheel_bounds(context, box, viewport)
            move_into(
                page, wheel_bounds,
                fallback=((wheel_bounds[0] + wheel_bounds[2]) / 2,
                          (wheel_bounds[1] + wheel_bounds[3]) / 2), rng=rng)

        notches = max(1, min(MAX_WHEEL_NOTCHES, round(abs(delta) / WHEEL_NOTCH_PX)))
        direction = 1 if delta > 0 else -1
        for _ in range(notches):
            page.mouse.wheel(0, direction * WHEEL_NOTCH_PX)
        time.sleep(rng.uniform(*SCROLL_GAP_S))

    box = target.bounding_box(**action)
    context = target.evaluate(SCROLL_CONTEXT) if box else {}
    detail = _scroll_failure_detail(context or {})
    raise RuntimeError(
        f"wheel-scrolling to target exceeded its flick limit: {detail}")


def scroll_to_top(page: Page, *, rng: Any = random) -> bool:
    """Return the active page scroller to its top using wheel events."""
    deadline = time.monotonic() + SCROLL_TIMEOUT_S
    previous = None
    stalled = 0

    for _ in range(MAX_SCROLL_FLICKS):
        state = page.evaluate(PAGE_SCROLL_STATE)
        if not state or state["scrollTop"] <= 2:
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError("timed out while wheel-scrolling to top")

        if previous is not None and abs(state["scrollTop"] - previous) <= 2:
            stalled += 1
            if stalled >= 2:
                raise RuntimeError("wheel scrolling did not return the page to top")
        else:
            stalled = 0
        previous = state["scrollTop"]

        move_into(
            page, (state["left"], state["top"], state["right"], state["bottom"]),
            fallback=((state["left"] + state["right"]) / 2,
                      (state["top"] + state["bottom"]) / 2), rng=rng)
        notches = max(1, min(
            MAX_WHEEL_NOTCHES, round(state["scrollTop"] / WHEEL_NOTCH_PX)))
        for _ in range(notches):
            page.mouse.wheel(0, -WHEEL_NOTCH_PX)
        time.sleep(rng.uniform(*SCROLL_GAP_S))

    raise RuntimeError("wheel-scrolling to top exceeded its flick limit")


def _spread(extent: float, aim: Aim, cap: float) -> float:
    """How far from centre this axis may land, edges honoured before ratio."""
    room = max(0.0, extent / 2 - aim.margin_px)
    return min(extent * aim.ratio, cap, room)


def _click_point(box: dict[str, float], *, rng: Any = random,
                 aim: Aim = DEFAULT_AIM,
                 bias: tuple[float, float] = (0.0, 0.0),
                 ) -> tuple[float, float, float, float]:
    """Choose one point inside a control and return page and element coordinates.

    Centre-biased, not centred: the triangular mode is moved off the midpoint
    by this browser's own aim, so the cluster sits where that hand puts it and
    each click varies around that.
    """
    width, height = float(box["width"]), float(box["height"])
    spread_x = _spread(width, aim, aim.max_x)
    spread_y = _spread(height, aim, aim.max_y)
    mode_x = bias[0] * aim.mode_jitter * spread_x
    mode_y = bias[1] * aim.mode_jitter * spread_y
    local_x = width / 2 + rng.triangular(-spread_x, spread_x, mode_x)
    local_y = height / 2 + rng.triangular(-spread_y, spread_y, mode_y)
    return (float(box["x"]) + local_x, float(box["y"]) + local_y,
            local_x, local_y)


def click(page: Page, target: Locator, *, timeout: int | None = None,
          rng: Any = random, aim: Aim = DEFAULT_AIM) -> Any:
    """Approach a locator naturally, then click a safe point inside it."""
    action = {} if timeout is None else {"timeout": timeout}
    viewport = page.evaluate(VIEWPORT)
    maybe_move_pointer(
        page, (0, 0, viewport["w"], viewport["h"]), rng=rng)
    box = scroll_to_target(page, target, timeout=timeout, rng=rng)
    if box is not None and box["width"] > 0 and box["height"] > 0:
        x, y, local_x, local_y = _click_point(
            box, rng=rng, aim=aim, bias=aiming_bias(page, rng=rng))
        move_to(page, x, y, rng=rng)
        action["position"] = {"x": local_x, "y": local_y}

    return target.click(delay=rng.randint(*CLICK_HOLD_MS), **action)


def click_in_place(page: Page, target: Locator, *, timeout: int | None = None,
                   no_wait_after: bool = False,
                   rng: Any = random, aim: Aim = DEFAULT_AIM) -> Any:
    """Move naturally to a fixed or popup control without scrolling it."""
    action = {} if timeout is None else {"timeout": timeout}
    box = target.bounding_box(**action)
    if box is not None and box["width"] > 0 and box["height"] > 0:
        x, y, local_x, local_y = _click_point(
            box, rng=rng, aim=aim, bias=aiming_bias(page, rng=rng))
        move_to(page, x, y, rng=rng)
        action["position"] = {"x": local_x, "y": local_y}
    if no_wait_after:
        action["no_wait_after"] = True

    return target.click(delay=rng.randint(*CLICK_HOLD_MS), **action)
