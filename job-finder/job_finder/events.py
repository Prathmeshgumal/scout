"""Goal events: stored in Postgres, pushed live to the processes that show them."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

from .store import GoalNotFound, Store

log = logging.getLogger(__name__)


class Hub:
    """In-memory publish/subscribe, one set of queues per goal. One process, so no broker."""

    def __init__(self, max_queue: int = 1000) -> None:
        self._max_queue = max_queue
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._dropped: set[asyncio.Queue] = set()

    def subscribe(self, goal_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.setdefault(goal_id, set()).add(queue)
        return queue

    def unsubscribe(self, goal_id: str, queue: asyncio.Queue) -> None:
        self._dropped.discard(queue)
        queues = self._subscribers.get(goal_id)
        if queues is None:
            return
        queues.discard(queue)
        if not queues:
            del self._subscribers[goal_id]

    def dropped(self, queue: asyncio.Queue) -> bool:
        """True when the queue overflowed and stopped receiving events; its reader must catch up from the store."""
        return queue in self._dropped

    def publish(self, goal_id: str, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(goal_id, ())):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.unsubscribe(goal_id, queue)
                self._dropped.add(queue)


class Recorder:
    def __init__(self, store: Store, hub: Hub):
        self.store = store
        self.hub = hub
        self._muted: set[str] = set()

    @contextlib.contextmanager
    def muted(self, goal_id: str) -> Iterator[None]:
        """Store and show no event of the goal: a delete stops it, and the chat must not show a stop."""
        self._muted.add(goal_id)
        try:
            yield
        finally:
            self._muted.discard(goal_id)

    async def record(self, goal_id: str, type_: str, detail: dict | None = None, **data: Any) -> dict:
        """Store an event and push it live. The detail (a step's input and output) is stored only."""
        if goal_id in self._muted:
            return {"seq": None, "at": None, "type": type_, **data}
        try:
            event = await self.store.append_event(goal_id, type_, data, detail)
        except GoalNotFound:
            log.info("mission %s was deleted: dropped its %s event", goal_id, type_)
            return {"seq": None, "at": None, "type": type_, **data}
        self.hub.publish(goal_id, event)
        return event

    def live(self, goal_id: str, type_: str, **data: Any) -> None:
        if goal_id in self._muted:
            return
        self.hub.publish(goal_id, {"seq": None, "at": None, "type": type_, **data})


async def follow(store: Store, hub: Hub, goal_id: str, after: int = 0,
                 poll_s: float | None = None) -> AsyncIterator[dict | None]:
    """The goal's stored events after seq `after`, then its live events. Each stored event comes once.

    With poll_s, read the store when no live event comes for poll_s seconds: another process (the CLI) can
    drive the goal, and its live events do not reach this hub. Then yield None when nothing is new."""
    # Subscribe before reading the store, so an event recorded in between is not lost;
    # anything delivered twice is dropped by its sequence number.
    queue = hub.subscribe(goal_id)
    try:
        last = after
        for event in await store.list_events(goal_id, after):
            last = event["seq"]
            yield event
        while True:
            if queue.empty() and hub.dropped(queue):
                # The reader fell too far behind and missed live events: resubscribe, then replay from the store.
                hub.unsubscribe(goal_id, queue)
                queue = hub.subscribe(goal_id)
                for event in await store.list_events(goal_id, last):
                    last = event["seq"]
                    yield event
                continue
            if poll_s is None:
                event = await queue.get()
            else:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=poll_s)
                except TimeoutError:
                    stored = await store.list_events(goal_id, last)
                    for event in stored:
                        last = event["seq"]
                        yield event
                    if not stored:
                        yield None
                    continue
            if event.get("seq") is not None:
                if event["seq"] <= last:
                    continue
                last = event["seq"]
            yield event
    finally:
        hub.unsubscribe(goal_id, queue)
