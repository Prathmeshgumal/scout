"""Hard delete of a goal, of one goal of a mandate, and of a mandate with its chat and its goals.

The steps of a delete:
1. Lock the mandate, if there is one. The scheduler then cannot start a goal of it.
2. Stop and close the session of each goal, and lock each goal. A goal that another process has gives GoalLocked.
3. Delete the rows in one transaction (Store.delete_goals).
4. Tell the open chats, and delete the SDK transcripts on disk.
5. Cancel in career-lens the pending scrapes of the goals that no other goal waits on. A page that career-lens is
   loading finishes there, and nothing reads it."""

from __future__ import annotations

import asyncio
import contextlib
import time

from .career_lens import CareerLens
from .config import Settings
from .db import GoalLock, mandate_lock_key
from .events import Recorder
from .files import FileStore
from .mandate_store import MandateStore
from .options import remove_transcripts
from .services import GoalLocked
from .store import Store


class MandateChat(Exception):
    """The chat of a mandate goes only with its mandate."""

    def __init__(self, goal_id: str):
        super().__init__("This chat belongs to a schedule. Delete the schedule to delete its chat.")
        self.goal_id = goal_id


class Deleter:
    def __init__(self, settings: Settings, store: Store, mandates: MandateStore, recorder: Recorder,
                 sessions=None, *, career_lens: CareerLens | None = None, wait_s: float = 30.0,
                 files: FileStore | None = None):
        self.settings, self.store, self.mandates, self.recorder = settings, store, mandates, recorder
        self.sessions = sessions  # the web server's GoalSessions; None in the CLI, which opens no session
        self.career_lens = career_lens  # None: nothing is cancelled in career-lens
        self.files = files  # None: no file bytes to remove
        self.wait_s = wait_s  # how long to wait for the scheduler, which holds the mandate while a goal starts

    # What a delete removes: for the warning before it

    async def goal_preview(self, goal_id: str) -> dict:
        return await self.store.delete_preview([goal_id])

    async def mandate_preview(self, mandate_id: str) -> dict | None:
        m = await self.mandates.get(mandate_id)
        if m is None:
            return None
        runs = [r["id"] for r in await self.mandates.runs(mandate_id)]
        preview = await self.store.delete_preview([m["home_goal_id"], *runs])
        return {**preview, "goals": len(runs), "status": m["status"], "next_run_at": m["next_run_at"]}

    # Deletes

    async def _delete_rows(self, goal_ids: list[str]) -> list[str]:
        keys = await self.files.keys_of(goal_ids) if self.files else []
        sessions = await self.store.delete_goals(goal_ids)
        if keys:
            await self.files.remove_objects(keys)
        return sessions

    async def delete_goal(self, goal_id: str) -> bool:
        """Delete a goal, or one goal of a mandate. False when it does not exist."""
        goal = await self.store.get_goal(goal_id)
        if goal is None:
            return False
        if goal["home_of"]:
            raise MandateChat(goal_id)
        mandate_id = goal["mandate_id"]
        async with contextlib.AsyncExitStack() as held:
            number = None
            if mandate_id:
                await self._lock(held, mandate_lock_key(mandate_id), wait=True)
                number = next((r["number"] for r in await self.mandates.runs(mandate_id) if r["id"] == goal_id), None)
            await self._close(held, [goal_id])
            # Read the goal again under the locks: another delete can have come first, or the user confirmed a
            # mandate in this chat before the session closed.
            goal = await self.store.get_goal(goal_id)
            if goal is None:
                return False
            if goal["home_of"]:
                raise MandateChat(goal_id)
            scrapes = await self.store.pending_request_ids([goal_id])
            sessions = await self._delete_rows([goal_id])
        self._after([goal_id], sessions)
        await self._cancel(scrapes)
        if mandate_id and (m := await self.mandates.get(mandate_id)) is not None:
            await self.recorder.record(m["home_goal_id"], "notice", trigger="mandate_run_deleted",
                                       text=f"Mission {number} was deleted by the user. "
                                            "Its spend still counts to the total spend limit.")
        return True

    async def delete_mandate(self, mandate_id: str) -> bool:
        """Delete the mandate, its chat and all its goals. False when it does not exist."""
        async with contextlib.AsyncExitStack() as held:
            await self._lock(held, mandate_lock_key(mandate_id), wait=True)
            m = await self.mandates.get(mandate_id)  # read under the lock: no new goal can start now
            if m is None:
                return False
            goal_ids = [m["home_goal_id"], *(r["id"] for r in await self.mandates.runs(mandate_id))]
            await self._close(held, goal_ids)
            scrapes = await self.store.pending_request_ids(goal_ids)
            sessions = await self._delete_rows(goal_ids)  # the chat's delete removes the mandate too
        self._after(goal_ids, sessions)
        await self._cancel(scrapes)
        return True

    # Internals

    async def _lock(self, held: contextlib.AsyncExitStack, key: str, *, wait: bool) -> None:
        deadline = time.monotonic() + (self.wait_s if wait else 0)
        while (lock := await GoalLock.acquire(self.settings.database_url, key)) is None:
            if time.monotonic() >= deadline:
                raise GoalLocked(key)
            await asyncio.sleep(0.1)
        held.push_async_callback(lock.release)

    async def _close(self, held: contextlib.AsyncExitStack, goal_ids: list[str]) -> None:
        """Lock each goal, so that no other process can open it. Stop and close the goals that are open in this
        process. The stop is silent: the chat does not show "Stopped by the user" for a deleted goal.

        The steps, each in one order for all deletes, so that two deletes do not wait for each other:
        1. Hold each goal in this process. A message or another delete of the goal waits.
        2. Lock the closed goals. When a terminal has one open, the delete fails here and stops nothing.
        3. Stop, close and lock the open goals."""
        ordered = sorted(goal_ids)
        for goal_id in ordered:
            if self.sessions is not None:
                await held.enter_async_context(self.sessions.held(goal_id))
            held.enter_context(self.recorder.muted(goal_id))
        is_open = {goal_id for goal_id in ordered if self.sessions is not None and self.sessions.get(goal_id)}
        for goal_id in ordered:
            if goal_id not in is_open:
                await self._lock(held, goal_id, wait=False)
        for goal_id in ordered:
            if goal_id in is_open:
                await self.sessions.shut(goal_id)
                await self._lock(held, goal_id, wait=False)

    async def _cancel(self, request_ids: list[str]) -> None:
        """After the delete, so that the deleted goals' scrapes no longer count as waiting."""
        if self.career_lens is not None and request_ids:
            await self.career_lens.cancel_all(await self.store.unwanted(request_ids))

    def _after(self, goal_ids: list[str], sessions: list[str]) -> None:
        for goal_id in goal_ids:
            self.recorder.live(goal_id, "goal_deleted")
            if self.sessions is not None:
                self.sessions.forget(goal_id)
        remove_transcripts(self.settings.claude_config_dir, sessions)
