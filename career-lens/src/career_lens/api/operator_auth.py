"""The operator password and the session cookie.

Only the operator console uses these routes. A customer API key has no place
here: a request with a Bearer header is refused, even with a valid cookie. The
session is signed with a key from the password, so a new password ends all
sessions.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from typing import Annotated

from fastapi import APIRouter, Cookie, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

PASSWORD_ENV = "CAREER_LENS_OPERATOR_PASSWORD"
COOKIE = "career_lens_operator"
COOKIE_PATH = "/operator"
SESSION_SECONDS = 12 * 60 * 60
MAX_FAILURES = 5
LOCK_SECONDS = 60


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
    key = hashlib.sha256(b"career-lens-operator-session:" + password.encode()).digest()
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _new_session(password: str) -> str:
    payload = f"{int(_now()) + SESSION_SECONDS}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_signature(payload, password)}"


def _valid(session: str | None, password: str) -> bool:
    if not session or not password:
        return False
    payload, _, signature = session.rpartition(".")
    if not hmac.compare_digest(signature.encode(), _signature(payload, password).encode()):
        return False
    expires = payload.partition(".")[0]
    return expires.isdigit() and _now() < int(expires)


def require_operator(
    session: Annotated[str | None, Cookie(alias=COOKIE)] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if authorization is not None or not _valid(session, _password()):
        raise HTTPException(401, {"code": "not_signed_in"})


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


router = APIRouter(prefix="/operator/api", include_in_schema=False)


@router.post("/login", status_code=204)
def login(body: Login, request: Request, response: Response) -> None:
    password = _password()
    if not password:
        raise HTTPException(503, {"code": "operator_password_not_set"})
    if guard.locked():
        raise HTTPException(429, {"code": "too_many_attempts"})
    if not hmac.compare_digest(body.password.encode(), password.encode()):
        guard.failed()
        raise HTTPException(401, {"code": "wrong_password"})
    guard.reset()
    response.set_cookie(
        COOKIE, _new_session(password), max_age=SESSION_SECONDS, path=COOKIE_PATH,
        httponly=True, samesite="strict", secure=request.url.scheme == "https",
    )


@router.post("/logout", status_code=204)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE, path=COOKIE_PATH, httponly=True, samesite="strict")


@router.get("/session")
def session(cookie: Annotated[str | None, Cookie(alias=COOKIE)] = None) -> dict:
    return {"signed_in": _valid(cookie, _password())}
