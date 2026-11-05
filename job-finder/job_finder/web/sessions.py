"""The goal sessions of the web server. A message opens a goal's session; the server closes it when idle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ..db import GoalLock
from ..services import GoalLocked, Services
from ..session import GoalSession
from ..store import GoalNotFound

log = logging.getLogger(__name__)


@dataclass
class _Open:
    session: GoalSession
    lock: GoalLock
    last_used: float


def _busy(session: GoalSession) -> bool:
    return session.main_busy or bool(session.tasks)


class GoalSessions:
    """Open goal sessions, one for each goal. Also the watcher's port: see Watcher.goals."""

    def __init__(self, services: Services, idle_s: float, *, clock=time.monotonic):
        self.s = services
        self.idle_s = idle_s
        self._clock = clock
        self._open: dict[str, _Open] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)  # one open or close at a time
        self._to_recover: set[str] = set()  # running goals that another process held when this one started
        self._stale: set[str] = set()  # goals whose prompt changed: reopen the session when it is not busy

    def get(self, goal_id: str) -> GoalSession | None:
        entry = self._open.get(goal_id)
        return entry.session if entry else None

    @property
    def count(self) -> int:
        return len(self._open)

    async def _entry(self, goal_id: str, recover: bool) -> _Open:
        """The open session of the goal. Open it if it is closed. Call with the goal's lock."""
        entry = self._open.get(goal_id)
        stale = entry is not None and (goal_id in self._stale or entry.session.needs_reopen)
        if stale and not _busy(entry.session):
            await self._close(goal_id)
            entry = None
        if entry is None:
            self._stale.discard(goal_id)
            session, lock = await self.s.open_session(goal_id, recover=recover)
            entry = self._open[goal_id] = _Open(session, lock, self._clock())
        entry.last_used = self._clock()
        return entry

    async def send(self, goal_id: str, text: str, trigger: str, *, user: bool = True, recover: bool = True) -> None:
        """Give the goal's main agent a message. GoalLocked when another process drives the goal."""
        async with self._locks[goal_id]:
            entry = await self._entry(goal_id, recover)
            await entry.session.send(text, trigger, user=user)

    def busy(self, goal_id: str) -> bool:
        """True while the main agent or a subagent of the goal works."""
        entry = self._open.get(goal_id)
        return entry is not None and _busy(entry.session)

    def refresh(self, goal_id: str) -> None:
        """The goal's prompt changed, e.g. its profile. The next message opens a new session with it."""
        if goal_id in self._open:
            self._stale.add(goal_id)

    async def stop(self, goal_id: str) -> None:
        """Stop all work on the goal. A closed goal opens first, so that its pending scrapes stop too."""
        async with self._locks[goal_id]:
            entry = await self._entry(goal_id, recover=False)
            await entry.session.stop()

    async def halt_running(self, reason: str) -> None:
        """career-lens is down: the open goals that work stop with this reason."""
        for goal_id in list(self._open):
            async with self._locks[goal_id]:
                entry = self._open.get(goal_id)
                if entry is not None and _busy(entry.session):
                    await entry.session.halt(reason)

    async def time_limit(self, goal_id: str, hard: bool) -> None:
        """The scheduler found the goal at its time limit. A closed goal opens first. A goal that another process
        holds is skipped: the next check tries again."""
        async with self._locks[goal_id]:
            try:
                entry = await self._entry(goal_id, recover=False)
            except (GoalLocked, GoalNotFound):
                return
            await entry.session.reach_time_limit(hard=hard)

    @contextlib.asynccontextmanager
    async def held(self, goal_id: str) -> AsyncIterator[None]:
        """Hold the goal in this process while the caller works: a delete. Nothing opens or closes it meanwhile."""
        async with self._locks[goal_id]:
            yield

    async def shut(self, goal_id: str) -> None:
        """Stop and close the goal's session. Call it inside held()."""
        entry = self._open.get(goal_id)
        if entry is not None:
            if _busy(entry.session):
                await entry.session.stop()
            await self._close(goal_id)

    def forget(self, goal_id: str) -> None:
        """The goal was deleted: drop what this process keeps about it."""
        self._stale.discard(goal_id)
        self._to_recover.discard(goal_id)
        self._locks.pop(goal_id, None)

    # The watcher's port

    async def reachable(self, goal_id: str) -> bool:
        """True when this process can give the goal a message. Opens a closed goal. False when another process
        has it."""
        async with self._locks[goal_id]:
            try:
                await self._entry(goal_id, recover=True)
            except (GoalLocked, GoalNotFound):
                return False
            return True

    async def ready(self, goal_id: str) -> bool:
        """True when the goal can take a message now: reachable, and its main agent and subagents are idle."""
        return await self.reachable(goal_id) and not _busy(self._open[goal_id].session)

    def is_running(self, goal_id: str, agent_id: str) -> bool:
        session = self.get(goal_id)
        return session is not None and agent_id in session.tasks  # task ids are the subagents' agentIds

    def type_busy(self, goal_id: str, name: str) -> bool:
        session = self.get(goal_id)
        return session is not None and bool(name) and session.guard.busy(name)

    def start(self, goal_id: str, message: str, trigger: str) -> asyncio.Task:
        return asyncio.create_task(self._start(goal_id, message, trigger))

    async def _start(self, goal_id: str, message: str, trigger: str) -> None:
        try:
            await self.send(goal_id, message, trigger, user=False)
        except GoalNotFound:
            pass  # the user deleted the goal
        except Exception:
            log.exception("goal %s: could not give the watcher's message", goal_id)

    # Open and close

    async def recover(self) -> None:
        """Open the goals that were running when the server stopped. Their restart note resumes the work.

        The old server process can still hold a goal while it stops. reap() tries such a goal again."""
        for goal in await self.s.store.goals_with_status("running"):
            if not goal["agent"]:  # a goal of `job-finder agent` is driven by its command
                await self._recover(goal["id"])

    async def _recover(self, goal_id: str) -> None:
        async with self._locks[goal_id]:
            try:
                await self._entry(goal_id, recover=True)
            except GoalLocked:
                self._to_recover.add(goal_id)
                return
            except Exception:
                log.exception("goal %s: could not reopen it", goal_id)
            self._to_recover.discard(goal_id)

    async def _recover_again(self) -> None:
        for goal_id in list(self._to_recover):
            goal = await self.s.store.get_goal(goal_id)
            if goal is None or goal["status"] != "running" or goal_id in self._open:
                self._to_recover.discard(goal_id)  # someone else continued or ended it
            else:
                await self._recover(goal_id)

    async def reap(self) -> None:
        """Close each session that has been idle for idle_s. Try again the goals that recover() could not open."""
        await self._recover_again()
        now = self._clock()
        for goal_id, entry in list(self._open.items()):
            if _busy(entry.session):
                entry.last_used = now
            elif now - entry.last_used >= self.idle_s:
                async with self._locks[goal_id]:
                    if self._open.get(goal_id) is entry and not _busy(entry.session):
                        await self._close(goal_id)

    async def run_reaper(self, interval_s: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            try:
                await self.reap()
            except Exception:
                log.exception("closing idle sessions failed")

    async def _close(self, goal_id: str) -> None:
        entry = self._open.pop(goal_id)
        try:
            await entry.session.close()
        finally:
            await entry.lock.release()

    async def close_all(self) -> None:
        """Close every session. Running subagents pause; the next open resumes them."""
        for goal_id in list(self._open):
            async with self._locks[goal_id]:
                if goal_id in self._open:
                    await self._close(goal_id)
