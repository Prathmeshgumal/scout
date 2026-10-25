"""Generate and verify opaque credentials without persisting their secrets."""

from __future__ import annotations

import hashlib
import re
import secrets


_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
_API_KEY_ID = re.compile(r"ak-[a-z0-9-]+")


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def matches(secret: str, expected: str) -> bool:
    return secrets.compare_digest(digest(secret), expected)


def issue_api_key(key_id: str) -> str:
    if _API_KEY_ID.fullmatch(key_id) is None:
        raise ValueError("invalid API key id")
    return f"cl_v1.{key_id}.{secrets.token_urlsafe(32)}"


def parse_api_key(token: str) -> tuple[str, str] | None:
    parts = token.split(".")
    if (len(parts) != 3 or parts[0] != "cl_v1"
            or _API_KEY_ID.fullmatch(parts[1]) is None
            or _SECRET.fullmatch(parts[2]) is None):
        return None
    return parts[1], parts[2]


