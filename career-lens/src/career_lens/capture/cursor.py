"""Showing Patchright's virtual mouse pointer in the headed browser.

Opt-in. The pointer is what makes a headed run legible -- whether a click
landed, whether the page scrolled under the wheel or ignored it -- and none of
that is visible from a log. It costs one `page.evaluate` per document and
never touches `add_init_script`, so it is not a stealth concern.

The overlay marks itself with `ARTIFACT_ONLY`, which `page._stored_html` drops
before anything is written, so no capture records it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import interaction
from .marks import ARTIFACT_ONLY

if TYPE_CHECKING:
    from patchright.sync_api import BrowserContext, Page

CURSOR_ID = "career-lens-debug-cursor"
CURSOR_SCRIPT = """
settings => {
    const cursorId = settings.cursorId;
    if (document.getElementById(cursorId)) return;
    const host = document.createElement("div");
    host.id = cursorId;
    host.setAttribute("aria-hidden", "true");
    // Ours, not the site's. `_stored_html` drops anything carrying this, so
    // a run with the overlay on stores the same HTML as a run without it,
    // rather than one recording a career_lens div as though LinkedIn served it.
    host.setAttribute(settings.artifactOnly, "1");
    host.style.cssText = [
        "position:fixed", "left:0", "top:0", "width:0", "height:0",
        "z-index:2147483647", "pointer-events:none", "visibility:hidden"
    ].join(";");
    const shadow = host.attachShadow({mode: "closed"});
    const pointer = document.createElement("div");
    pointer.style.cssText = [
        "width:18px", "height:24px", "background:#111",
        "clip-path:polygon(0 0,0 20px,5px 15px,9px 23px,13px 21px,9px 14px,16px 14px)",
        "filter:drop-shadow(1px 0 0 #fff) drop-shadow(-1px 0 0 #fff) drop-shadow(0 1px 0 #fff) drop-shadow(0 -1px 0 #fff)",
        "transform-origin:2px 2px"
    ].join(";");
    shadow.appendChild(pointer);
    document.documentElement.appendChild(host);
    const placeInTopLayer = () => {
        const dialogs = document.querySelectorAll("dialog[open]");
        const parent = dialogs.length ? dialogs[dialogs.length - 1]
                                      : document.documentElement;
        if (host.parentNode !== parent) parent.appendChild(host);
    };
    if (settings.position) {
        placeInTopLayer();
        host.style.transform = `translate3d(${settings.position.x}px, ${settings.position.y}px, 0)`;
        host.style.visibility = "visible";
    }
    document.addEventListener("mousemove", event => {
        placeInTopLayer();
        host.style.transform = `translate3d(${event.clientX}px, ${event.clientY}px, 0)`;
        host.style.visibility = "visible";
    }, true);
    document.addEventListener("mousedown", () => {
        pointer.style.transform = "scale(.82)";
    }, true);
    document.addEventListener("mouseup", () => {
        pointer.style.transform = "scale(1)";
    }, true);
}
"""


def _install_cursor(page: Page) -> None:
    try:
        position = interaction.pointer_position(page)
        page.evaluate(CURSOR_SCRIPT, {
            "cursorId": CURSOR_ID,
            "artifactOnly": ARTIFACT_ONLY,
            "position": (
                {"x": position[0], "y": position[1]}
                if position is not None else None
            ),
        })
    except Exception:
        pass


def enable_cursor(context: BrowserContext) -> None:
    def attach(page: Page) -> None:
        page.on("domcontentloaded", lambda: _install_cursor(page))
        _install_cursor(page)

    context.on("page", attach)
    for page in context.pages:
        attach(page)
