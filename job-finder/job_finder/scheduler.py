"""Starts the runs of mandates on their schedule. Code, not an agent: see the mandates spec, section 3."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .artifact_store import ArtifactStore
from .db import GoalLock, mandate_lock_key
from .events import Recorder
from .mandate_store import MandateStore
from .mandates import kickoff_message
from .schedule import Rule, can_start_late, newest_due, next_slot, slot_text
from .session import WRAP_UP_USD
from .store import Store
from .time_limit import check_time_limits

log = logging.getLogger(__name__)
TICK_S = 60.0
LATE = timedelta(minutes=5)  # a slot older than this is late: the first goal and "Start a goal now" are not


class Scheduler:
    def __init__(self, store: Store, mandates: MandateStore, artifacts: ArtifactStore, recorder: Recorder, port, *,
                 interval_s: float = TICK_S, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 max_failures: int = 3):
        self.store, self.mandates, self.artifacts, self.recorder = store, mandates, artifacts, recorder
        self.port = port  # GoalSessions: async send(goal_id, text, trigger, *, user) and time_limit(goal_id, hard)
        self.interval_s = interval_s
        self.now = now
        self.max_failures = max_failures  # failed runs in a row that pause the mandate; 0: never
        self._told_waiting: set[tuple[str, datetime]] = set()  # (mandate, slot): the home chat knows that it waits

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("the scheduler tick failed")
            await asyncio.sleep(self.interval_s)

    async def tick(self) -> None:
        # First the time limits: a goal that ends at its limit lets the next goal of its mandate start.
        await check_time_limits(self.store, self.mandates, self.port)
        await self._announce()
        now = self.now()
        for m in await self.mandates.due(now):
            try:
                await self._start(m, now)
            except Exception:  # one bad mandate must not stop the others
                log.exception("schedule %s: could not start a run", m["id"])

    async def _end(self, m: dict, reason: str) -> None:
        await self.mandates.update(m["id"], {"status": "ended"})
        await self.recorder.record(m["home_goal_id"], "notice", trigger="mandate_ended", text=f"Schedule ended: {reason}")

    async def _start(self, m: dict, now: datetime) -> None:
        # A delete of the mandate, or of one of its goals, holds this lock. The next tick tries again.
        lock = await GoalLock.acquire(self.mandates.pool.conninfo, mandate_lock_key(m["id"]))
        if lock is None:
            return
        try:
            await self._start_locked(m, now)
        finally:
            await lock.release()

    async def _start_locked(self, m: dict, now: datetime) -> None:
        if await self.mandates.has_running_run(m["id"]):
            await self._tell_waiting(m)
            return  # the next tick tries again
        if m["ends_on"] and now.astimezone(ZoneInfo(m["timezone"])).date() > m["ends_on"]:
            await self._end(m, "the end date passed.")
            return
        cap = m["max_spend_per_run"]
        if m["max_total_spend"] is not None:
            left = round(m["max_total_spend"] - await self.mandates.total_spend(m["id"]), 2)
            if left <= WRAP_UP_USD:
                await self._end(m, "the total spend limit is reached.")
                return
            cap = min(cap, left)
        rule = Rule.of(m)
        first, last = await self.mandates.slots(m["id"])
        slot = m["next_run_at"]
        if first is not None and last is not None and now - slot > LATE:
            # The server was off, or the goal before took long. Start one goal for the newest missed time, and only
            # on its own day, inside its window. Skip the older times.
            slot = newest_due(rule, first, last, slot, now)
            if not can_start_late(rule, slot, now):
                await self.mandates.claim(m, next_slot(rule, first, last, now))
                return
        # Claim the slot first. The claim fails when the user changed the mandate since this tick read it: then
        # the next tick reads the new settings. A failed start must not start the same slot again.
        if not await self.mandates.claim(m, next_slot(rule, first or slot, slot, now)):
            return
        history = await self.mandates.history(m["id"])
        number = m["goals_started"] + 1
        run = await self.mandates.create_run(m, slot, cap, number)
        if run is None:
            return  # another server started this slot
        await self.recorder.record(m["home_goal_id"], "notice", trigger="mandate_run_started", run_id=run["id"],
                                   text=f"Mission {number} started for {slot_text(slot, m['timezone'])}. "
                                        f"Mission id: {run['id']}.")
        try:
            await self.port.send(run["id"], kickoff_message(m, number, slot, history), "mandate_run",
                                 user=False, recover=False)
        except Exception as exc:
            log.exception("schedule %s: run %s could not start", m["id"], run["id"])
            await self.store.set_status(run["id"], "failed", f"The mission could not start: {exc}")

    async def _tell_waiting(self, m: dict) -> None:
        """A run that does not stop blocks the mandate. Tell the user once for each slot, so that they can stop it."""
        key = (m["id"], m["next_run_at"])
        if key in self._told_waiting:
            return
        self._told_waiting.add(key)
        runs = [r for r in await self.mandates.runs(m["id"]) if r["status"] in ("running", "waiting")]
        which = f"Mission {runs[0]['number']} is" if runs else "A mission is"
        await self.recorder.record(
            m["home_goal_id"], "notice", trigger="mandate_run_waiting",
            text=(f"The mission for {slot_text(m['next_run_at'], m['timezone'])} waits. {which} still in progress. "
                  "The next mission starts when it stops. To start sooner, open that mission and stop it."))

    async def _announce(self) -> None:
        for row in await self.mandates.unannounced_runs():
            m = await self.mandates.get_for_run(row["id"])
            if m is None:
                continue  # the user deleted it
            p = await self.artifacts.pipeline(row["id"])
            await self.recorder.record(
                row["home_goal_id"], "notice", trigger="mandate_run_finished", run_id=row["id"],
                text=(f"Mission {row['number']}, {slot_text(row['run_slot'], m['timezone'])}: {p['fit']} fit of "
                      f"{p['jobs']} found, ${row['spend_usd']:.2f}. Status: {row['status']}. Mission id: {row['id']}."))
            await self.mandates.mark_announced(row["id"])
            if row["status"] == "failed":
                await self._pause_after_failures(m, row["id"])

    async def _pause_after_failures(self, m: dict, failed_id: str) -> None:
        """Pause an active mandate when its failed runs in a row reach a multiple of max_failures. After a resume,
        the mandate pauses again only after as many new failures."""
        if not self.max_failures or m["status"] != "active":
            return
        in_a_row = 0
        for run in reversed(await self.mandates.runs(m["id"])):
            if run["status"] != "failed":
                break
            in_a_row += 1
        if in_a_row % self.max_failures:
            return
        await self.mandates.update(m["id"], {"status": "paused"})
        reason = (await self.store.get_goal(failed_id))["status_reason"] or "unknown"
        await self.recorder.record(
            m["home_goal_id"], "notice", trigger="mandate_paused",
            text=(f"Schedule paused: the last {in_a_row} missions failed. The last reason: {reason} "
                  "Fix the cause, then ask here in this chat to resume the schedule."))
