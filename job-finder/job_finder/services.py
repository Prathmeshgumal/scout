"""The parts of the app that one CLI process starts: store, events, career-lens, goal session and watcher."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import ClaudeSDKClient

from .artifact_store import ArtifactStore
from .career_lens import CareerLens
from .config import Settings
from .db import GoalLock, migrate, open_pool
from .events import Hub, Recorder
from .files import FileStore
from .objects import open_objects
from .session import GoalSession
from .store import GoalNotFound, Store  # noqa: F401 (GoalNotFound: for the callers of open_session)
from .watcher import Watcher


class GoalLocked(Exception):
    def __init__(self, goal_id: str):
        super().__init__("Mission is open in another terminal or in the web app.")
        self.goal_id = goal_id


async def with_pending_note(store: Store, goal_id: str, text: str) -> str:
    """The user's message, with a note when scrapes of the goal are still pending."""
    pending = await store.count_pending(goal_id)
    if not pending:
        return text
    noun = "scrape for this mission is" if pending == 1 else "scrapes for this mission are"
    return f"{text}\n\n(Note: {pending} {noun} still pending; you will be resumed when they finish.)"


class SessionPort:
    """What the watcher needs from a goal: can it take a message, which subagents run, and a way to give it one.
    One live session."""

    def __init__(self, session: GoalSession):
        self.session = session

    async def reachable(self, goal_id: str) -> bool:
        return True

    async def ready(self, goal_id: str) -> bool:
        return not (self.session.main_busy or self.session.tasks)

    def is_running(self, goal_id: str, agent_id: str) -> bool:
        return agent_id in self.session.tasks  # task ids are the subagents' agentIds

    def type_busy(self, goal_id: str, name: str) -> bool:
        return bool(name) and self.session.guard.busy(name)

    def start(self, goal_id: str, message: str, trigger: str) -> asyncio.Task:
        return asyncio.create_task(self.session.send(message, trigger, user=False))


@dataclass
class Services:
    settings: Settings
    store: Store
    artifacts: ArtifactStore
    hub: Hub
    recorder: Recorder
    career_lens: CareerLens
    client_factory: Any = ClaudeSDKClient
    sleep: Any = asyncio.sleep
    files: FileStore | None = None

    @classmethod
    async def open(cls, settings: Settings, *, client_factory=ClaudeSDKClient) -> Services:
        await asyncio.to_thread(migrate, settings.database_url)
        pool = await open_pool(settings.database_url)
        store, hub = Store(pool), Hub()
        recorder = Recorder(store, hub)
        career_lens = CareerLens(settings.career_lens_base_url, settings.career_lens_api_key,
                                 freshness=settings.career_lens_freshness)
        return cls(settings, store, ArtifactStore(pool, settings.research_max_age_days), hub, recorder, career_lens,
                   client_factory, files=FileStore(pool, open_objects(settings)))

    async def close(self) -> None:
        await self.career_lens.aclose()
        await self.store.pool.close()

    async def open_session(self, goal_id: str, *, recover: bool = True) -> tuple[GoalSession, GoalLock]:
        """Lock the goal and open its session. The caller closes both. GoalLocked when another process has it."""
        lock = await GoalLock.acquire(self.settings.database_url, goal_id)
        if lock is None:
            raise GoalLocked(goal_id)
        session = GoalSession(self.settings, self.store, self.recorder, self.career_lens, goal_id,
                              client_factory=self.client_factory, sleep=self.sleep, files=self.files)
        try:
            await session.open(recover=recover)
        except BaseException:
            await session.close()
            await lock.release()
            raise
        return session, lock

    @asynccontextmanager
    async def drive(self, goal_id: str, *, recover: bool = True) -> AsyncIterator[GoalSession]:
        """Hold the goal's lock, open its session and watch its scrapes while the caller drives the goal."""
        session, lock = await self.open_session(goal_id, recover=recover)
        s = self.settings
        watching = None
        try:
            watcher = Watcher(self.store, self.recorder, self.career_lens, SessionPort(session), s.watch_interval_s,
                              s.max_wait_hours, goal_ids={goal_id})
            watching = asyncio.create_task(watcher.run_forever())
            yield session
        finally:
            if watching is not None:
                watching.cancel()
                await asyncio.gather(watching, return_exceptions=True)
            await session.close()
            await lock.release()
