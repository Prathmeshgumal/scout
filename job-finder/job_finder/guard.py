"""One run of each subagent type in a goal. Different types can run at the same time."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

START_LIMIT_S = 120.0  # a start mark that no task message cleared is old after this time


class SubagentGuard:
    def __init__(self, running: Callable[[], Iterable[str]], clock: Callable[[], float] = time.monotonic):
        self._running, self._clock = running, clock
        self._starting: dict[str, tuple[str, float]] = {}  # tool_use_id -> (subagent type, mark time)

    def busy(self, name: str) -> bool:
        now = self._clock()
        self._starting = {k: v for k, v in self._starting.items() if now - v[1] < START_LIMIT_S}
        return name in set(self._running()) or any(n == name for n, _ in self._starting.values())

    def try_start(self, name: str, tool_use_id: str) -> bool:
        """Mark the type as starting, when it is free. No await between the check and the mark."""
        if self.busy(name):
            return False
        self._starting[tool_use_id] = (name, self._clock())
        return True

    def started(self, name: str) -> None:
        """A task of this type started: the session's running tasks show it now."""
        self._starting = {k: v for k, v in self._starting.items() if v[0] != name}

    def failed(self, tool_use_id: str) -> None:
        self._starting.pop(tool_use_id, None)
