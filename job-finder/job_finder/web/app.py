"""The web app: an HTTP API and a live event stream for the browser chat. See docs/architecture.md."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from ..config import NO_LOGIN, ROOT, Settings, env_file
from ..deletion import Deleter, MandateChat
from ..events import Hub, follow
from ..login_status import LoginStatus
from ..mandate_store import MandateStore
from ..models import EFFORT_LABELS, ModelList, effort_label, read_sdk_models
from ..options import retry_message
from ..profile import public_profile
from ..report import build_report, job_detail
from ..report_md import render_markdown
from ..runtime_status import CareerLensStatus, read_version
from ..schedule import known_zone
from ..scheduler import Scheduler
from ..services import GoalLocked, Services, with_pending_note
from ..store import Store, goal_title
from ..time_limit import ContinueError, continue_check, continue_goal
from ..watcher import Watcher
from .sessions import GoalSessions

log = logging.getLogger(__name__)
STATIC_DIR = ROOT / "web" / "dist"
STOPPABLE = {"running", "waiting"}
RETRYABLE = {"failed", "stopped"}


class Text(BaseModel):
    text: str
    timezone: str | None = None  # the time zone of the user, from the browser. An unknown zone becomes None.

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Type a message")
        return value

    @field_validator("timezone")
    @classmethod
    def zone(cls, value: str | None) -> str | None:
        return known_zone(value)


class NewGoal(Text):
    model: str | None = None  # None: the default of .env
    effort: str | None = None


class ModelChoice(BaseModel):
    model: str
    effort: str


def _sse(event: dict) -> str:
    head = f"id: {event['seq']}\n" if event.get("seq") is not None else ""
    return f"{head}data: {json.dumps(event, default=str)}\n\n"


async def sse_stream(store: Store, hub: Hub, goal_id: str, after: int, *, poll_s: float = 2.0,
                     keepalive_s: float = 15.0) -> AsyncIterator[str]:
    """Server-Sent Events: the goal's stored events after `after`, then live ones, with keep-alive comments."""
    sent = time.monotonic()
    async for event in follow(store, hub, goal_id, after, poll_s=poll_s):
        if event is not None:
            sent = time.monotonic()
            yield _sse(event)
            if event["type"] == "goal_deleted":
                return
        elif time.monotonic() - sent >= keepalive_s:
            sent = time.monotonic()
            yield ": ping\n\n"


def create_app(settings: Settings, services: Services | None = None, sessions: GoalSessions | None = None, *,
               model_loader=None) -> FastAPI:
    """The app. Tests give services, sessions and the model list; else the app opens them when it starts."""
    model_list = ModelList(model_loader or (lambda: read_sdk_models(settings)), settings.model, settings.effort)

    version = read_version(ROOT)
    login_status = LoginStatus(settings, env_file())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.sessions is not None:
            yield
            return
        s = await Services.open(settings)
        manager = GoalSessions(s, settings.idle_minutes * 60)
        monitor = CareerLensStatus(s.career_lens, manager.halt_running)
        app.state.career_lens_status = monitor
        app.state.services, app.state.sessions = s, manager
        watcher = Watcher(s.store, s.recorder, s.career_lens, manager, settings.watch_interval_s,
                          settings.max_wait_hours)
        scheduler = Scheduler(s.store, MandateStore(s.store.pool), s.artifacts, s.recorder, manager,
                              max_failures=settings.mandate_max_failures)
        await manager.recover()
        background = [asyncio.create_task(monitor.run()), asyncio.create_task(watcher.run_forever()),
                      asyncio.create_task(manager.run_reaper()),
                      asyncio.create_task(scheduler.run_forever()),
                      asyncio.create_task(model_list.menu())]  # read the models of the SDK before the first page asks
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            await manager.close_all()
            await s.close()

    app = FastAPI(title="job-finder", lifespan=lifespan)
    app.state.services, app.state.sessions = services, sessions
    app.state.career_lens_status = None

    def svc(request: Request) -> Services:
        return request.app.state.services

    def manager(request: Request) -> GoalSessions:
        return request.app.state.sessions

    @app.exception_handler(GoalLocked)
    async def locked(request: Request, exc: GoalLocked) -> JSONResponse:
        return JSONResponse({"detail": "This goal is open in a terminal. Close it there first."}, status_code=409)

    @app.exception_handler(MandateChat)
    async def mandate_chat(request: Request, exc: MandateChat) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    def deleter(request: Request) -> Deleter:
        s = svc(request)
        return Deleter(settings, s.store, MandateStore(s.store.pool), s.recorder, manager(request),
                       career_lens=s.career_lens)

    def need_key() -> None:
        if settings.auth_mode == "none":
            raise HTTPException(503, NO_LOGIN)

    async def goal_or_404(request: Request, goal_id: str) -> dict:
        try:
            uuid.UUID(goal_id)
        except ValueError:
            raise HTTPException(404, "Goal not found") from None
        goal = await svc(request).store.get_goal(goal_id)
        if goal is None:
            raise HTTPException(404, "Goal not found")
        return goal

    async def drive(request: Request, goal_id: str, text: str, trigger: str, *, user: bool = True,
                    recover: bool = True) -> None:
        """Give the goal a message. A session that cannot open gives 503, with the reason."""
        need_key()
        try:
            await manager(request).send(goal_id, text, trigger, user=user, recover=recover)
        except (GoalLocked, HTTPException):
            raise
        except Exception as exc:
            log.exception("goal %s: could not send a message", goal_id)
            raise HTTPException(503, f"Could not reach the agent: {exc}") from exc

    def effort_levels(menu: list[dict]) -> list[str]:
        """All effort levels of the menu, from fast to thorough."""
        found = {e for m in menu for e in m["efforts"]}
        return [e for e in EFFORT_LABELS if e in found] + sorted(found - set(EFFORT_LABELS))

    async def check_choice(model: str | None, effort: str | None) -> None:
        """Only a model of the SDK's list, with an effort level that the SDK gives for it."""
        menu = await model_list.menu()
        chosen = next((m for m in menu if m["id"] == (model or settings.model)), None)
        if chosen is None:
            raise HTTPException(422, f"Unknown model: {model}")
        levels = chosen["efforts"] or effort_levels(menu)  # a model without levels keeps the effort and ignores it
        if effort is not None and effort not in levels:
            raise HTTPException(422, f"{chosen['label']} has these effort levels: {', '.join(levels)}.")

    @app.get("/api/models")
    async def models() -> dict:
        """The models and the effort levels to choose from, and the default of .env."""
        menu = await model_list.menu()
        return {"models": menu, "efforts": [{"id": e, "label": effort_label(e)} for e in effort_levels(menu)],
                "default": {"model": settings.model, "effort": settings.effort}}

    @app.post("/api/goals", status_code=201)
    async def create_goal(body: NewGoal, request: Request) -> dict:
        need_key()
        await check_choice(body.model, body.effort)
        goal = await svc(request).store.create_goal(goal_title(body.text), max_fresh_scrapes=settings.max_fresh_scrapes,
                                                    max_spend_usd=settings.max_spend_usd,
                                                    model=body.model or settings.model,
                                                    effort=body.effort or settings.effort,
                                                    timezone=body.timezone)
        await drive(request, goal["id"], body.text, "user", recover=False)
        return {"goal_id": goal["id"]}

    @app.put("/api/goals/{goal_id}/model")
    async def set_model(goal_id: str, body: ModelChoice, request: Request) -> dict:
        """The model and the effort of the goal. They apply from the next message: the session opens again. While
        the agents work, the user stops them first."""
        await goal_or_404(request, goal_id)
        await check_choice(body.model, body.effort)
        if manager(request).busy(goal_id):
            raise HTTPException(409, "The agents are working. Stop the work to change the model or the effort.")
        await svc(request).store.set_model(goal_id, body.model, body.effort)
        manager(request).refresh(goal_id)
        return {"ok": True}

    @app.get("/api/goals")
    async def list_goals(request: Request) -> list[dict]:
        return await svc(request).store.list_goals()

    @app.get("/api/goals/{goal_id}")
    async def get_goal(goal_id: str, request: Request, events: bool = True) -> dict:
        """The goal and its pipeline. With events (the default), also its whole event history."""
        goal = await goal_or_404(request, goal_id)
        s, session = svc(request), manager(request).get(goal_id)
        detail = {"goal": {**goal, "profile": public_profile(goal.get("profile"))},
                  "pipeline": await s.artifacts.pipeline(goal_id),
                  "tasks": session.running_tasks() if session else [], "busy": bool(session and session.main_busy),
                  "scrapes": {"fresh": (await s.store.usage(goal_id))["fresh_scrapes"],
                              "pending": await s.store.count_pending(goal_id)}}
        detail["time_limit"] = await continue_check(s.store, MandateStore(s.store.pool), goal)
        if events:
            detail["events"] = await s.store.list_events(goal_id)
        return detail

    async def report_of(request: Request, goal_id: str) -> dict:
        s, session = svc(request), manager(request).get(goal_id)
        tasks = [task["name"] for task in session.running_tasks()] if session else []
        return json.loads(json.dumps(await build_report(s.store, s.artifacts, goal_id, tasks), default=str))

    @app.get("/api/goals/{goal_id}/report")
    async def report(goal_id: str, request: Request) -> dict:
        """The goal and its roles: built from the saved data each time. No agent writes it."""
        await goal_or_404(request, goal_id)
        return await report_of(request, goal_id)

    @app.get("/api/goals/{goal_id}/jobs/{job_id}")
    async def job(goal_id: str, job_id: str, request: Request) -> dict:
        """All that the detail panel of one role shows."""
        await goal_or_404(request, goal_id)
        try:
            uuid.UUID(job_id)
        except ValueError:
            raise HTTPException(404, "Role not found") from None
        s = svc(request)
        row = await s.artifacts.get_job(goal_id, job_id)
        if row is None:
            raise HTTPException(404, "Role not found")
        return json.loads(json.dumps(job_detail(row, await s.artifacts.list_notes(goal_id)), default=str))

    @app.get("/api/jobs")
    async def all_jobs(request: Request) -> list[dict]:
        """The roles of all goals, for the Jobs page. The browser filters them."""
        return await svc(request).artifacts.job_cards()

    @app.get("/api/goals/{goal_id}/report.md")
    async def report_markdown(goal_id: str, request: Request, tz: str | None = None) -> PlainTextResponse:
        """The goal report as a Markdown file. tz: the time zone of the user, for the times. Default: UTC."""
        await goal_or_404(request, goal_id)
        report = await report_of(request, goal_id)
        name = f"goal-{goal_id[:8]}-report.md"
        return PlainTextResponse(render_markdown(report, known_zone(tz) or "UTC"),
                                 media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/goals/{goal_id}/stream")
    async def stream(goal_id: str, request: Request, after: int = 0) -> StreamingResponse:
        await goal_or_404(request, goal_id)
        s = svc(request)
        return StreamingResponse(sse_stream(s.store, s.hub, goal_id, after), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/goals/{goal_id}/messages", status_code=202)
    async def message(goal_id: str, body: Text, request: Request) -> dict:
        goal = await goal_or_404(request, goal_id)
        if goal["agent"]:
            raise HTTPException(409, f"This is a {goal['agent']} test goal. Continue it with: job-finder agent")
        if body.timezone and body.timezone != goal.get("timezone"):
            await svc(request).store.set_timezone(goal_id, body.timezone)
        text = await with_pending_note(svc(request).store, goal_id, body.text)
        await drive(request, goal_id, text, "follow_up")
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/stop", status_code=202)
    async def stop(goal_id: str, request: Request) -> dict:
        goal = await goal_or_404(request, goal_id)
        if goal["status"] not in STOPPABLE:
            raise HTTPException(409, "This goal is not running.")
        need_key()
        await manager(request).stop(goal_id)
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/retry", status_code=202)
    async def retry(goal_id: str, request: Request) -> dict:
        goal = await goal_or_404(request, goal_id)
        session = manager(request).get(goal_id)
        if goal["status"] not in RETRYABLE or (session and session.main_busy):
            raise HTTPException(409, "Only a failed or stopped goal can be retried.")
        note = retry_message(goal, await svc(request).store.first_user_message(goal_id))
        await drive(request, goal_id, note, "retry", user=False)
        return {"ok": True}

    @app.post("/api/goals/{goal_id}/continue", status_code=202)
    async def continue_after_time_limit(goal_id: str, request: Request) -> dict:
        """Give a goal that stopped at its time limit the new, higher limit of its mandate, and continue it."""
        await goal_or_404(request, goal_id)
        need_key()
        s = svc(request)
        try:
            note = await continue_goal(s.store, MandateStore(s.store.pool), goal_id)
        except ContinueError as exc:
            raise HTTPException(409, str(exc)) from None
        manager(request).refresh(goal_id)  # an open session knows the old limit: the message opens a new one
        await drive(request, goal_id, note, "time_continue", user=False)
        return {"ok": True}

    @app.get("/api/goals/{goal_id}/delete-preview")
    async def goal_delete_preview(goal_id: str, request: Request) -> dict:
        """What a delete of the goal removes: for the warning before it."""
        await goal_or_404(request, goal_id)
        return await deleter(request).goal_preview(goal_id)

    @app.delete("/api/goals/{goal_id}", status_code=204)
    async def delete_goal(goal_id: str, request: Request) -> Response:
        """Delete the goal and all its data. The chat of a mandate goes only with its mandate."""
        await goal_or_404(request, goal_id)
        if not await deleter(request).delete_goal(goal_id):
            raise HTTPException(404, "Goal not found")
        return Response(status_code=204)

    # Mandates: a list for the sidebar, and a delete. Only the agent of the mandate's chat creates or changes a
    # mandate (job_finder/tools/mandates.py).

    def mandate_store(request: Request) -> MandateStore:
        return MandateStore(svc(request).store.pool)

    @app.get("/api/mandates")
    async def list_mandates(request: Request) -> list[dict]:
        return [{**m, "profile": public_profile(m["profile"])} for m in await mandate_store(request).list()]

    def mandate_id_or_404(mandate_id: str) -> None:
        try:
            uuid.UUID(mandate_id)
        except ValueError:
            raise HTTPException(404, "Mandate not found") from None

    @app.get("/api/mandates/{mandate_id}/delete-preview")
    async def mandate_delete_preview(mandate_id: str, request: Request) -> dict:
        """What a delete of the mandate removes: its chat and its goals. For the warning before it."""
        mandate_id_or_404(mandate_id)
        preview = await deleter(request).mandate_preview(mandate_id)
        if preview is None:
            raise HTTPException(404, "Mandate not found")
        return preview

    @app.delete("/api/mandates/{mandate_id}", status_code=204)
    async def delete_mandate(mandate_id: str, request: Request) -> Response:
        """Delete the mandate, its chat and all its goals, with all their data."""
        mandate_id_or_404(mandate_id)
        if not await deleter(request).delete_mandate(mandate_id):
            raise HTTPException(404, "Mandate not found")
        return Response(status_code=204)

    @app.get("/api/status")
    async def status(request: Request, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return {"model": settings.model, "effort": settings.effort, "anthropic_configured": settings.auth_mode != "none",
                "claude_login": settings.auth_label,
                "claude_account": login_status.snapshot(),
                "career_lens_configured": bool(settings.career_lens_base_url and settings.career_lens_api_key),
                "version": version,
                "career_lens": (request.app.state.career_lens_status.snapshot
                                if request.app.state.career_lens_status else
                                {"state": "checking" if settings.career_lens_base_url and settings.career_lens_api_key
                                 else "not_configured", "checked_at": None}),
                "open_sessions": manager(request).count}

    if STATIC_DIR.is_dir():
        @app.get("/goals/{path:path}", include_in_schema=False)
        @app.get("/mandates/{path:path}", include_in_schema=False)
        @app.get("/mandates", include_in_schema=False)
        @app.get("/jobs", include_in_schema=False)
        async def goal_page(path: str = "") -> FileResponse:
            """A link to a page of the app: the browser app reads the URL."""
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
