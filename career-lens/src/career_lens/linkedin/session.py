"""Observe and deliberately change the LinkedIn identity in one browser."""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlsplit

from patchright.sync_api import Error as BrowserError
from patchright.sync_api import TimeoutError as BrowserTimeoutError

from ..capture import interaction, trace
from ..capture.detect import detect, is_blocking
from ..capture.discovery import type_text
from ..capture.urls import canonical, classify
from .recovery import recover_transient_error

ME_URL = "https://www.linkedin.com/in/me/"
LOGIN_URL = "https://www.linkedin.com/login"
LOGIN_USERNAME = 'input[autocomplete~="username"]:visible'
LOGIN_PASSWORD = 'input[autocomplete~="current-password"]:visible'
# LinkedIn serves more than one global nav, and the account decides which one.
# Measured 2026-08-14 across three signed-in profiles: one carried an avatar
# button whose aria-label ends in " Me", two carried a `navigation-settings`
# button with no aria-label at all. Matching only the first left sign-out
# unable to find the account menu on two of the three. Both forms are listed
# so a variant change costs a selector rather than a broken clear.
ACCOUNT_MENU = (
    'button[data-view-name="navigation-settings"]:visible, '
    '[aria-label$=" Me"]:visible'
)
LOGIN_CONTROL_TIMEOUT_MS = 10_000
LOGIN_RESULT_TIMEOUT_MS = 30_000
IDENTITY_REDIRECT_TIMEOUT_MS = 5_000
LOGIN_SETTLE_PAUSE_S = (0.7, 1.6)
USERNAME_TO_PASSWORD_PAUSE_S = (0.45, 1.2)
PASSWORD_TO_SUBMIT_PAUSE_S = (0.5, 1.4)
LOGIN_RESULT_PAUSE_S = (1.0, 2.2)
LOGIN_IDLE_MOVE_CHANCE = 0.35
CLEAR_CONTROL_TIMEOUT_MS = 10_000
CLEAR_MENU_PAUSE_S = (0.25, 0.9)
CLEAR_LOGOUT_GRACE_S = 8
LINKEDIN_COOKIE_DOMAIN = re.compile(r"(^|\.)linkedin\.com$")
# `local_storage` is listed rather than cleared through DOMStorage.clear:
# Chrome resolves that command's storage id against currently loaded frames and
# raises "Frame not found for the given storage id" for every origin that is
# not open, which is most of them. clearDataForOrigin has no such requirement.
LINKEDIN_STORAGE_TYPES = (
    "local_storage,indexeddb,cache_storage,service_workers,websql"
)
CONTROL_HAS_VALUE = "el => Boolean(el.value)"
PUBLIC_PROFILE_SIGN_IN = (
    '[data-tracking-control-name="public_profile_nav-header-signin"]:visible'
)


@dataclass(frozen=True)
class SessionObservation:
    state: Literal["signed_out", "signed_in", "challenge", "unknown"]
    profile_url: str | None = None
    detail: str | None = None
    host: str | None = None


def _profile(url: str) -> str | None:
    try:
        value = canonical(url)
    except Exception:
        return None
    return value if classify(value) == "linkedin_person" else None


def profile_identity(value: str | None) -> str | None:
    """Return an exact member path without redirect host/query decoration."""
    if not value:
        return None
    try:
        normalised = canonical(value)
    except ValueError:
        return None
    if classify(normalised) != "linkedin_person":
        return None
    return unquote(urlsplit(normalised).path).rstrip("/").casefold()


def same_profile(first: str | None, second: str | None) -> bool:
    first_identity = profile_identity(first)
    return bool(first_identity and first_identity == profile_identity(second))


def _identity_placeholder(url) -> bool:
    return urlsplit(str(url)).path.rstrip("/").casefold() == "/in/me"


def verified_host(observation: SessionObservation) -> str | None:
    """Return the LinkedIn host that produced a session observation."""
    host = observation.host
    if host is None and observation.profile_url:
        host = urlsplit(observation.profile_url).hostname
    value = (host or "").strip().rstrip(".").lower()
    if value == "linkedin.com" or value.endswith(".linkedin.com"):
        return value
    return None


def observe_session(page) -> SessionObservation:
    """Resolve LinkedIn's `/in/me/` redirect into a browser-session fact."""
    challenge = _visible_challenge(page)
    if challenge is not None:
        return challenge
    with trace.phase(trace.NET, "verify LinkedIn session"):
        page.goto(ME_URL, wait_until="domcontentloaded")
        recover_transient_error(page)
        if _identity_placeholder(page.url):
            try:
                page.wait_for_url(
                    lambda url: not _identity_placeholder(url),
                    timeout=IDENTITY_REDIRECT_TIMEOUT_MS)
            except BrowserTimeoutError:
                pass
    with trace.phase(trace.WORK, "read LinkedIn identity"):
        return _read_identity(page)


def _read_identity(page) -> SessionObservation:
    """Classify the LinkedIn document after its identity redirect settles."""
    challenge = _visible_challenge(page)
    if challenge is not None:
        return challenge
    lowered = page.url.lower()
    if any(token in lowered for token in (
            "/checkpoint/", "/challenge/", "/captcha/")):
        return SessionObservation("challenge", detail="LinkedIn challenge")
    challenge = detect(page)
    if challenge is not None and is_blocking(challenge):
        if challenge.kind in {"linkedin_authwall", "generic_login", "login_gate"}:
            return SessionObservation("signed_out", detail=challenge.kind)
        return SessionObservation("challenge", detail=challenge.kind)
    if _public_profile_sign_in(page):
        return SessionObservation(
            "signed_out", detail="LinkedIn public profile sign-in")
    profile_url = _profile(page.url)
    if profile_url is not None and not profile_url.endswith("/in/me/"):
        return SessionObservation(
            "signed_in", profile_url=profile_url,
            host=(urlsplit(page.url).hostname or "").lower() or None)
    if any(token in lowered for token in ("/login", "/uas/login", "/authwall")):
        return SessionObservation("signed_out")
    return SessionObservation("unknown", detail="identity redirect was inconclusive")


def _visible_challenge(page) -> SessionObservation | None:
    """Inspect without navigating away from a pending human verification."""
    host = urlsplit(page.url).hostname or ""
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    if any(token in page.url.lower() for token in (
            "/checkpoint/", "/challenge/", "/captcha/")):
        return SessionObservation("challenge", detail="LinkedIn verification required")
    if not _login_page(page.url) and "/authwall" not in page.url.lower():
        return None
    content = page.inner_text("body").casefold().replace("’", "'")
    if any(marker in content for marker in (
            "let's verify it's you", "enter the verification code",
            "suspicious number of login attempts")):
        return SessionObservation("challenge", detail="LinkedIn verification required")
    return None


def _public_profile_sign_in(page) -> bool:
    """Whether a profile is LinkedIn's logged-out public rendering.

    The tracking marker is the precise signal observed in Chrome.  The short
    header-text check is the backup for markup experiments that rename it.
    Restricting both checks to a profile URL avoids treating an article that
    mentions signing in as an account observation.
    """
    if _profile(getattr(page, "url", "")) is None:
        return False
    try:
        control = page.locator(PUBLIC_PROFILE_SIGN_IN)
        if control.count() and control.first.is_visible():
            return True
    except Exception:
        pass
    try:
        head = page.inner_text("body")[:500].casefold()
    except Exception:
        return False
    return "join now" in head and "sign in" in head


def type_secret(page, locator, value: str, *, timeout: int | None = None,
                rng=random) -> None:
    """Focus and pace a secret without returning or tracing its value."""
    interaction.click(page, locator, timeout=timeout, rng=rng)
    _select_existing_value(page, locator)
    type_text(page, value, rng=rng)


def _select_existing_value(page, locator) -> bool:
    """Select autofilled text without bringing its value into Python."""
    if not locator.evaluate(CONTROL_HAS_VALUE):
        return False
    page.keyboard.press("ControlOrMeta+a")
    return True


def _login_pause(page, bounds: tuple[float, float], *, rng) -> None:
    """Pause between login stages and occasionally let the pointer drift."""
    time.sleep(rng.uniform(*bounds))
    try:
        viewport = page.evaluate(interaction.VIEWPORT)
        interaction.maybe_move_pointer(
            page, (0, 0, viewport["w"], viewport["h"]),
            chance=LOGIN_IDLE_MOVE_CHANCE, rng=rng)
    except BrowserError:
        # A URL can change before its new execution context is ready. Pointer
        # drift is optional and must not turn a successful sign-in into a
        # failed worker operation.
        pass


def _control_unavailable(name: str) -> SessionObservation:
    return SessionObservation(
        "signed_out", detail=f"LinkedIn login {name} control unavailable")


def _login_page(url) -> bool:
    return any(token in str(url).lower() for token in ("/login", "/uas/login"))


def login_controls(page):
    """Locate the current visible login form without relying on generated IDs."""
    return (
        page.locator(LOGIN_USERNAME),
        page.locator(LOGIN_PASSWORD),
        page.get_by_role("button", name="Sign in", exact=True),
    )


def login(page, *, username: str, password: str,
          rng=None) -> SessionObservation:
    rng = rng if rng is not None else random.SystemRandom()
    if not _login_page(page.url):
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
    username_field, password_field, submit = login_controls(page)
    password_ready = False
    try:
        username_field.wait_for(
            state="visible", timeout=LOGIN_CONTROL_TIMEOUT_MS)
        interaction.ensure_pointer_in_viewport(page, rng=rng)
        _login_pause(page, LOGIN_SETTLE_PAUSE_S, rng=rng)
        interaction.click(
            page, username_field, timeout=LOGIN_CONTROL_TIMEOUT_MS, rng=rng)
    except BrowserTimeoutError:
        # LinkedIn keeps a remembered-account cookie after ordinary sign-out
        # and then offers a password-only "Welcome back" form.  Its missing
        # username control is a form variant, not a failed login page.
        try:
            password_field.wait_for(
                state="visible", timeout=LOGIN_CONTROL_TIMEOUT_MS)
            password_ready = True
            interaction.ensure_pointer_in_viewport(page, rng=rng)
            _login_pause(page, LOGIN_SETTLE_PAUSE_S, rng=rng)
        except BrowserTimeoutError:
            observation = _read_identity(page)
            if observation.state == "signed_in":
                return observe_session(page)
            if observation.state != "signed_out":
                return observation
            return _control_unavailable("username")
    else:
        _select_existing_value(page, username_field)
        type_text(page, username, rng=rng)
        _login_pause(page, USERNAME_TO_PASSWORD_PAUSE_S, rng=rng)
    try:
        if not password_ready:
            password_field.wait_for(
                state="visible", timeout=LOGIN_CONTROL_TIMEOUT_MS)
        type_secret(
            page, password_field, password,
            timeout=LOGIN_CONTROL_TIMEOUT_MS, rng=rng)
    except BrowserTimeoutError:
        return _control_unavailable("password")
    _login_pause(page, PASSWORD_TO_SUBMIT_PAUSE_S, rng=rng)
    try:
        submit.wait_for(state="visible", timeout=LOGIN_CONTROL_TIMEOUT_MS)
        interaction.click(
            page, submit, timeout=LOGIN_CONTROL_TIMEOUT_MS, rng=rng)
    except BrowserTimeoutError:
        return _control_unavailable("submit")
    try:
        page.wait_for_url(
            lambda url: _profile(str(url)) is not None and not _identity_placeholder(url),
            timeout=LOGIN_RESULT_TIMEOUT_MS)
    except BrowserTimeoutError:
        pass
    _login_pause(page, LOGIN_RESULT_PAUSE_S, rng=rng)
    landed = _read_identity(page)
    if landed.state != "signed_in":
        if landed.state == "signed_out" and _login_page(page.url):
            return SessionObservation("signed_out", detail="LinkedIn login did not advance")
        return landed
    return observe_session(page)


def clear_browser_state(page, *, origins=()) -> None:
    """Remove LinkedIn's browser state, and only LinkedIn's.

    Sign-out is not enough on its own. LinkedIn's `li_rm` remember-me token
    outlives it and restores the session on the next visit, and the device
    cookies beside it (`bcookie`, `bscookie`, `li_sugr`, `UserMatchHistory`)
    are what let one profile's next occupant be linked to its last one.

    The origin list is discovered rather than assumed: LinkedIn serves country
    hosts such as `in.linkedin.com`, so the page's own URL, any caller-supplied
    origin, and every registered service worker are all considered alongside
    the two the site always uses. A worker profile also carries unrelated
    browsing, so nothing outside linkedin.com is touched.
    """
    linkedin_origins = {"https://linkedin.com", "https://www.linkedin.com"}
    candidates = [str(page.url), *origins]
    candidates.extend(worker.url for worker in page.context.service_workers)
    for candidate in candidates:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").strip().rstrip(".").lower()
        if (parsed.scheme in {"http", "https"} and
                (host == "linkedin.com" or host.endswith(".linkedin.com"))):
            linkedin_origins.add(f"{parsed.scheme}://{parsed.netloc.lower()}")

    first_error = None

    def attempt(action):
        nonlocal first_error
        try:
            return action()
        except Exception as exc:
            if first_error is None:
                first_error = exc
            return None

    cdp = attempt(lambda: page.context.new_cdp_session(page))
    if cdp is not None:
        # LinkedIn can commit `/m/logout/` and leave its document loading for
        # minutes. Stop that request before touching the context so teardown
        # cannot inherit an unfinished navigation.
        attempt(lambda: cdp.send("Page.stopLoading", {}))
    attempt(lambda: page.context.clear_cookies(domain=LINKEDIN_COOKIE_DOMAIN))
    if cdp is not None:
        for origin in sorted(linkedin_origins):
            attempt(lambda origin=origin: cdp.send(
                "Storage.clearDataForOrigin", {
                    "origin": origin,
                    "storageTypes": LINKEDIN_STORAGE_TYPES,
                }))
        # Do not let the still-open LinkedIn document recreate identifiers
        # after they were cleared. CDP starts this local navigation without
        # waiting for any document lifecycle event.
        attempt(lambda: cdp.send("Page.navigate", {"url": "about:blank"}))
        attempt(cdp.detach)
    if first_error is not None:
        raise first_error


def clear_session(page, *, rng=None) -> SessionObservation:
    """Sign out through LinkedIn's visible account menu, then drop its state."""
    rng = rng if rng is not None else random.SystemRandom()
    signed_in_url = str(page.url)
    logout_attempted = False
    try:
        viewport = page.evaluate(interaction.VIEWPORT)
        interaction.ensure_pointer(
            page, viewport["w"] / 2, viewport["h"] / 2, rng=rng)
        interaction.maybe_move_pointer(
            page, (0, 0, viewport["w"], viewport["h"]), chance=1.0, rng=rng)
        account_menu = page.locator(ACCOUNT_MENU).first
        interaction.click_in_place(
            page, account_menu, timeout=CLEAR_CONTROL_TIMEOUT_MS, rng=rng)
        time.sleep(rng.uniform(*CLEAR_MENU_PAUSE_S))
        sign_out = page.locator('a[href*="logout"]:visible').first
        sign_out.wait_for(
            state="visible", timeout=CLEAR_CONTROL_TIMEOUT_MS)
        logout_attempted = True
        interaction.click_in_place(
            page, sign_out, timeout=CLEAR_CONTROL_TIMEOUT_MS,
            no_wait_after=True, rng=rng)
    except BrowserError:
        # A browser-side failure may mean the navigation already started. Even
        # when LinkedIn changed the controls, clearing local state is safe; the
        # fresh browser observation settles success.
        pass
    finally:
        # Give LinkedIn's server a bounded chance to process its logout
        # endpoint, but never wait for `/m/logout/` itself to finish loading.
        if logout_attempted:
            time.sleep(CLEAR_LOGOUT_GRACE_S)
        clear_browser_state(page, origins=(signed_in_url,))
    return SessionObservation("signed_out")
