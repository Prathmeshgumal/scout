"""A token bucket for each API key on the customer API.

A bucket holds up to BURST tokens and gets PER_SECOND tokens back each second. Each customer call
takes one token. With no token, the call gets 429 and a Retry-After. The buckets are in memory: the
API runs as one process, and a restart fills each bucket again.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..store.config import ConfigError
from .security import require_key

BURST_ENV = "CAREER_LENS_RATE_LIMIT_BURST"
RATE_ENV = "CAREER_LENS_RATE_LIMIT_PER_SECOND"
DEFAULT_BURST = 120
DEFAULT_PER_SECOND = 2.0


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    remaining: int
    retry_after: int


class RateLimiter:
    def __init__(self, capacity: int, per_second: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self.per_second = per_second
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key id: (tokens, time of the last fill)
        self._lock = threading.Lock()

    def take(self, key_id: str) -> Decision:
        with self._lock:
            now = self.clock()
            tokens, filled_at = self._buckets.get(key_id, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - filled_at) * self.per_second)
            if tokens >= 1:
                tokens -= 1
                self._buckets[key_id] = (tokens, now)
                return Decision(True, math.floor(tokens), 0)
            self._buckets[key_id] = (tokens, now)
            return Decision(False, 0, math.ceil((1 - tokens) / self.per_second))


def _setting(name: str, default: float, parse: Callable[[str], float]) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = parse(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number above 0") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a number above 0")
    return value


def from_env() -> RateLimiter:
    return RateLimiter(int(_setting(BURST_ENV, DEFAULT_BURST, int)), _setting(RATE_ENV, DEFAULT_PER_SECOND, float))


def limited_key(request: Request, key_id: Annotated[str, Depends(require_key)]) -> str:
    """The key of the call, after the key passes and its bucket gives a token."""
    limiter: RateLimiter = request.app.state.rate_limiter
    decision = limiter.take(key_id)
    request.state.rate_limit = (limiter.capacity, decision.remaining)
    if not decision.allowed:
        raise HTTPException(429, {"code": "rate_limited"}, headers={"Retry-After": str(decision.retry_after)})
    return key_id


class RateLimitHeaders:
    """Tell the limit on each reply that took a token, also on an error and on a reply a route makes itself."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            known = scope.get("state", {}).get("rate_limit")
            if message["type"] == "http.response.start" and known is not None:
                limit, remaining = known
                message["headers"] = [*message.get("headers", []),
                                      (b"ratelimit-limit", str(limit).encode()),
                                      (b"ratelimit-remaining", str(remaining).encode())]
            await send(message)

        await self.app(scope, receive, with_headers)
