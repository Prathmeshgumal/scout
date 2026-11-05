"""The dashboard password, the bearer token, and the check that a request comes from this computer.

The web app and its API are one product: nobody else calls this API, so it has no API keys. The
password gives a signed token, and the web app sends the token as a Bearer header. The signing key
comes from the password, so a new password ends all tokens.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

PASSWORD_ENV = "JOB_FINDER_PASSWORD"
SESSION_SECONDS = 12 * 60 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 60
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
OPEN_PATHS = {"/api/auth/login", "/api/health"}


def _now() -> float:
    return time.time()


def _password() -> str:
    return os.environ.get(PASSWORD_ENV, "")


class LoginGuard:
    """Stop all logins for a time after too many wrong passwords."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.failures = 0
        self.locked_until = 0.0

    def locked(self) -> bool:
        return _now() < self.locked_until

    def failed(self) -> None:
        self.failures += 1
        if self.failures >= MAX_FAILURES:
            self.failures = 0
            self.locked_until = _now() + LOCK_SECONDS


guard = LoginGuard()


def _signature(payload: str, password: str) -> str:
    key = hashlib.sha256(b"job-finder-session:" + password.encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def new_token(password: str) -> str:
    payload = f"{int(_now()) + SESSION_SECONDS}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_signature(payload, password)}"


def valid_token(token: str | None, password: str) -> bool:
    if not token or not password:
        return False
    payload, _, signature = token.rpartition(".")
    if not hmac.compare_digest(signature.encode(), _signature(payload, password).encode()):
        return False
    expires = payload.partition(".")[0]
    return expires.isdigit() and _now() < int(expires)


def _hostname(netloc: str) -> str | None:
    try:
        return urlsplit(f"//{netloc}").hostname
    except ValueError:
        return None


def from_this_computer(headers: Headers) -> bool:
    """The Host stops a site that points its own name at this computer. The Origin stops a page of another site."""
    host = headers.get("host", "")
    origin = headers.get("origin")
    return _hostname(host) in LOOPBACK and (origin is None or urlsplit(origin).netloc == host)


def _bearer(headers: Headers) -> str | None:
    scheme, _, token = headers.get("authorization", "").partition(" ")
    return token if scheme.lower() == "bearer" else None


class Protect:
    """Check each request before any route. A route that a later change adds has this check too."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path = scope["path"]
        if not from_this_computer(headers):
            refusal = JSONResponse({"detail": "Open job-finder on the computer that runs it."}, status_code=403)
        elif path.startswith("/api/") and path not in OPEN_PATHS and not valid_token(_bearer(headers), _password()):
            refusal = JSONResponse({"detail": "Sign in to job-finder."}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})
        else:
            await self.app(scope, receive, send)
            return
        await refusal(scope, receive, send)


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


router = APIRouter(prefix="/api", include_in_schema=False)


@router.post("/auth/login")
async def login(body: Login) -> dict:
    password = _password()
    if not password:
        raise HTTPException(503, f"Set {PASSWORD_ENV} in the env file of job-finder, then start it again.")
    if guard.locked():
        raise HTTPException(429, "Too many wrong passwords. Wait one minute.")
    if not hmac.compare_digest(body.password.encode(), password.encode()):
        guard.failed()
        raise HTTPException(401, "The password is not correct.")
    guard.reset()
    return {"access_token": new_token(password), "token_type": "bearer", "expires_in": SESSION_SECONDS}


@router.get("/health")
async def health() -> dict:
    """For the container check. It says only that the app answers."""
    return {"ok": True}
