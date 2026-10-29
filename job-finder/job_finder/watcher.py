"""Polls career-lens for pending scrapes. Sends finished scrapes back to the subagent that asked, through the
main agent, and resumes goals whose own scrapes have all finished or waited too long."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from datetime import datetime, timedelta

from .artifact_store import ArtifactStore
from .career_lens import CareerLens, CareerLensError, jobs_state
from .events import Recorder
from .recap import scrape_line
from .store import Store

log = logging.getLogger(__name__)
SUCCEEDED = {"succeeded", "satisfied"}
FAILED = {"failed", "cancelled"}


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        log.warning("ignoring unreadable next_attempt_at %r", value)
        return None


def resume_message(finished: list[dict]) -> str:
    return "\n".join(["Scrapes finished:", *map(scrape_line, finished),
                      "Read the new data with get_company, get_job_search or get_linkedin_job (or search again) and "
                      "continue working toward the goal."])


def expired_message(finished: list[dict], hours: float) -> str:
    return "\n".join([f"Stopped waiting for scrapes after {hours:g} hours:", *map(scrape_line, finished),
                      "An expired scrape counts as not done. It does not mean \"no jobs\". Finish the goal with what "
                      "you have, and say what is missing."])


def agent_message(name: str | None, agent_id: str, finished: list[dict]) -> str:
    return "\n".join([f"Scrapes finished for {name or 'a subagent'} (agentId {agent_id}):",
                      *map(scrape_line, finished),
                      f"Resume it with SendMessage (to: {agent_id}), and tell it that these scrapes finished. "
                      "If SendMessage is refused, give the same roles to a new task of that subagent."])


class Watcher:
    def __init__(self, store: Store, recorder: Recorder, career_lens: CareerLens, goals, interval_s: float,
                 max_wait_hours: float = 72.0, goal_ids: Collection[str] | None = None):
        self.store = store
        self.recorder = recorder
        self.career_lens = career_lens
        # async ready(goal_id), async reachable(goal_id), is_running(goal_id, agent_id), start(goal_id, message, trigger)
        self.goals = goals
        self.interval_s = interval_s
        self.max_wait_hours = max_wait_hours
        self.goal_ids = goal_ids  # None: resume any goal; else only these (the goals this process drives)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("scrape watcher poll failed")
            await asyncio.sleep(self.interval_s)

    async def poll_once(self) -> None:
        for scrape in await self.store.pending_scrapes():
            try:
                status = await self.career_lens.request_status(scrape["request_id"])
                await self._apply(scrape, status)
            except CareerLensError as exc:
                log.warning("could not check %s: %s", scrape["request_id"], exc)
            except Exception:  # one odd response must not stop the other scrapes
                log.exception("could not update %s", scrape["request_id"])
        expired = await self.store.expire_overdue(timedelta(hours=self.max_wait_hours))
        answers = await self.career_lens.cancel_all(await self.store.unwanted([row["request_id"] for row in expired]))
        for row in expired:
            await self.recorder.record(row["goal_id"], "scrape_finished", request_id=row["request_id"],
                                       status="expired", failure_kind=None,
                                       answer=answers.get(row["request_id"]))
            if row["wants_jobs"]:
                await self._artifacts().record_job_read(row["goal_id"], row["target"], None, "failed")
        await self._notify_agents()
        for goal_id in dict.fromkeys(row["goal_id"] for row in expired):
            await self._resume(goal_id, "wait_expired")
        for goal_id in await self.store.goals_ready_to_resume():
            await self._resume(goal_id, "scrapes_finished")

    async def _notify_agents(self) -> None:
        """One note for each subagent whose scrapes are all finished. A busy main agent gets it next."""
        for row in await self.store.agents_ready_to_notify():
            goal_id, agent_id = row["goal_id"], row["agent_id"]
            if self.goal_ids is not None and goal_id not in self.goal_ids:
                continue
            if self.goals.is_running(goal_id, agent_id):
                continue  # it is still working, and wait_for_scrapes reads its own scrapes
            if self.goals.type_busy(goal_id, row["name"] or ""):
                continue  # another run of its type works: the main agent cannot resume it yet
            if not await self.goals.reachable(goal_id):
                continue  # another process drives the goal: its own watcher sends the note
            finished = await self.store.take_agent_unreported(goal_id, agent_id)
            if finished:
                self.goals.start(goal_id, agent_message(row["name"], agent_id, finished), "scrapes_finished")

    async def _resume(self, goal_id: str, trigger: str) -> None:
        if self.goal_ids is not None and goal_id not in self.goal_ids:
            return
        if not await self.goals.ready(goal_id):  # busy: the goal gets the scrapes on a later poll
            return
        finished = await self.store.take_unreported(goal_id)
        if not finished:
            return  # the subagents' scrapes went out in their own notes
        message = (expired_message(finished, self.max_wait_hours) if trigger == "wait_expired"
                   else resume_message(finished))
        self.goals.start(goal_id, message, trigger)

    def _artifacts(self) -> ArtifactStore:
        return ArtifactStore(self.store.pool)

    async def _read_jobs(self, scrape: dict, final: str) -> None:
        """What a finished company scrape read of the jobs section. A request can succeed with no jobs read: that is a
        failed read, not "no jobs"."""
        url = scrape["target"]
        body = await self.career_lens.get_company(url) if final == "succeeded" else None
        state = jobs_state(body)
        name = (((body or {}).get("data") or {}).get("profile") or {}).get("name")
        read = "not_captured" if state["state"] == "not_requested" else state["state"]
        if final != "succeeded" or body is None:
            read = "failed"
        await self._artifacts().record_job_read(scrape["goal_id"], url, name, read, state["items"],
                                                state["captured_pages"], state["observed_at"])

    async def _apply(self, scrape: dict, status: dict) -> None:
        state = status.get("status")
        goal_id = scrape["goal_id"]
        if state in SUCCEEDED or state in FAILED:
            final = "succeeded" if state in SUCCEEDED else "failed"
            failure = (status.get("failure") or {}).get("kind") if final == "failed" else None
            await self.store.finish_scrape(scrape["id"], final, failure)
            if scrape["wants_jobs"]:
                await self._read_jobs(scrape, final)
            await self.recorder.record(goal_id, "scrape_finished", request_id=scrape["request_id"],
                                       status=final, failure_kind=failure)
            return
        wait_reason = status.get("wait_reason")
        next_attempt = _when(status.get("next_attempt_at"))
        changed = (state, wait_reason, next_attempt) != (
            scrape["last_status"], scrape["wait_reason"], scrape["next_attempt_at"])
        await self.store.update_scrape_progress(scrape["id"], state, wait_reason, next_attempt)
        if changed:
            await self.recorder.record(goal_id, "scrape_status", request_id=scrape["request_id"], status=state,
                                       wait_reason=wait_reason,
                                       next_attempt_at=next_attempt.isoformat() if next_attempt else None)
