"""The running version and the latest CareerLens availability check."""

import asyncio
import re
import subprocess
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from .career_lens import CareerLens


def read_version(root: Path) -> dict:
    unknown = {"commit": None, "local": False, "modified": False}
    try:
        commit = (root / "VERSION").read_text().strip()
    except OSError:
        commit = ""
    if re.fullmatch(r"[0-9a-f]{40,64}", commit):
        return {"commit": commit, "local": False, "modified": False}
    # Do not pick up a parent repository when the app has no Git metadata.
    if not (root / ".git").exists():
        return unknown
    try:
        def git(*args):
            return subprocess.check_output(["git", "-C", str(root), *args], text=True,
                                           stderr=subprocess.DEVNULL, timeout=3).strip()
        commit = git("rev-parse", "HEAD")
        modified = bool(git("status", "--porcelain"))
    except (OSError, subprocess.SubprocessError):
        return unknown
    return {"commit": commit, "local": True, "modified": modified}


DOWN_CHECKS = 2  # one lost check is not an outage
DOWN_REASON = "career-lens is down, so the agents cannot search. Retry when it is back."


class CareerLensStatus:
    def __init__(self, client: CareerLens, on_down: Callable[[str], Awaitable[None]] | None = None):
        self.client = client
        self.on_down = on_down  # stops the goals that work. career-lens down must not look like "no jobs".
        self.snapshot = {"state": "checking" if client.configured else "not_configured", "checked_at": None}
        self._misses = 0

    async def check(self) -> None:
        state = await self.client.health()
        self.snapshot = {"state": state, "checked_at": datetime.now(UTC).isoformat()}
        self._misses = self._misses + 1 if state == "unreachable" else 0
        if self._misses == DOWN_CHECKS and self.on_down is not None:
            await self.on_down(DOWN_REASON)

    async def run(self, interval: float = 30) -> None:
        while True:
            await self.check()
            await asyncio.sleep(interval)
