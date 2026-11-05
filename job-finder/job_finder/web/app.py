"""The web app: an HTTP API and a live event stream for the browser chat. See docs/architecture.md."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from dotenv import dotenv_values
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from ..board_order import BOARDS, BoardOrder
from ..config import NO_LOGIN, ROOT, Settings, env_file
from ..deletion import Deleter, MandateChat
from ..events import Hub, follow
from ..files import MAX_BYTES, FileRefused, FileStorageError
from ..login import ENV_NAMES, SetupTokenFlow, check_credential, choose_credential, save_credential
from ..login_status import LoginStatus
from ..mandate_store import MandateStore
from ..metrics import metrics as read_metrics
from ..models import EFFORT_LABELS, ModelList, effort_label, read_sdk_models
from ..options import retry_message
from ..page_reader import PageReader
from ..pipeline import PipelineStore
from ..profile import public_profile
from ..report import build_report, job_detail
from ..report_md import render_markdown
from ..request_check import check_request
from ..runtime_status import CareerLensStatus, read_version
from ..schedule import known_zone
from ..scheduler import Scheduler
from ..services import GoalLocked, Services, with_pending_note
from ..store import Store, goal_title
from ..time_limit import ContinueError, continue_check, continue_goal
from ..translate import DETAIL_TOOLS
from ..watcher import Watcher
from . import auth
from .sessions import GoalSessions

log = logging.getLogger(__name__)
STATIC_DIR = ROOT / "web" / "dist"
RESTART_DELAY_S = 0.5  # the reply must reach the browser first
STOPPABLE = {"running", "waiting"}
RETRYABLE = {"failed", "stopped"}


class LoginBody(BaseModel):
    credential: str
    account_label: str = ""


class LoginChoice(BaseModel):
    mode: str


class LoginCode(BaseModel):
    code: str
    account_label: str = ""


class BoardColumnOrder(BaseModel):
    column: str = Field(min_length=1, max_length=40)
    keys: list[str] = Field(max_length=2000)

    @field_validator("keys")
    @classmethod
    def no_repeats(cls, keys: list[str]) -> list[str]:
        if len(set(keys)) != len(keys) or any(not k or len(k) > 500 for k in keys):
            raise ValueError("Give each card once, with a key of 1 to 500 characters.")
        return keys


class PipelineMove(BaseModel):
    role_key: str
    stage: Literal["fit", "referral", "applied", "responded", "removed"]


class PipelineNote(BaseModel):
    role_key: str
    text: str = Field(max_length=5000)

    @field_validator("text")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Type a note")
        return value


class Text(BaseModel):
    text: str
    timezone: str | None = None  # the time zone of the user, from the browser. An unknown zone becomes None.
    file_ids: list[uuid.UUID] = []  # uploaded files that this message attaches

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
               model_loader=None, request_check=None) -> FastAPI:
    """The app. Tests give services, sessions and the model list; else the app opens them when it starts.
    request_check: checks the first message of a new goal. Tests give it; else the app uses the small model."""
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
        app.state.request_check = lambda text: check_request(PageReader.for_settings(settings), text)
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
            app.state.setup_token.close()
            await manager.close_all()
            await s.close()

    app = FastAPI(title="job-finder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(auth.Protect)
    app.include_router(auth.router)
    app.state.services, app.state.sessions = services, sessions
    app.state.career_lens_status = None
    app.state.request_check = request_check
    app.state.setup_token = SetupTokenFlow()
    # Only a process that has a supervisor can restart itself. Docker Compose sets this name.
    app.state.restart = (lambda: os.kill(os.getpid(), signal.SIGTERM)) if os.environ.get("JOB_FINDER_SELF_RESTART") else None

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
                       career_lens=s.career_lens, files=s.files)

    async def with_files(request: Request, goal_id: str, body: Text) -> str:
        """The message, with the names of the files that it attaches. The files now belong to the goal."""
        ids = [str(file_id) for file_id in body.file_ids]
        linked = await svc(request).files.link(ids, goal_id)
        names = ", ".join(row["name"] for row in linked)
        return f"{body.text}\n\nAttached files: {names}" if names else body.text

    async def file_or_404(request: Request, file_id: str) -> dict:
        try:
            row = await svc(request).files.get(str(uuid.UUID(file_id)))
        except ValueError:
            row = None
        if row is None:
            raise HTTPException(404, "File not found.")
        return row

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

    @app.get("/api/metrics")
    async def metrics(request: Request, days: int = 30, tz: str = "UTC") -> dict:
        """Cost, time, results, failures and eval scores of the goals of the last `days` days."""
        return await read_metrics(svc(request).store.pool, days, known_zone(tz) or "UTC")

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
        check = request.app.state.request_check
        if check is not None and (refusal := await check(body.text)):
            raise HTTPException(422, refusal)  # no goal, no agent, no spend: the user can change the message
        goal = await svc(request).store.create_goal(goal_title(body.text), max_fresh_scrapes=settings.max_fresh_scrapes,
                                                    max_spend_usd=settings.max_spend_usd,
                                                    model=body.model or settings.model,
                                                    effort=body.effort or settings.effort,
                                                    timezone=body.timezone)
        await drive(request, goal["id"], await with_files(request, goal["id"], body), "user", recover=False)
        return {"goal_id": goal["id"]}

    @app.post("/api/files", status_code=201)
    async def upload_file(request: Request, file: UploadFile = File(...),
                          role: Literal["attachment", "resume"] = Form("attachment")) -> dict:
        """Keep a file. Replies give the facts of a file and never its text."""
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"The largest file is {MAX_BYTES // (1024 * 1024)} MB.")
        try:
            return await svc(request).files.save(file.filename, data, role)
        except FileRefused as exc:
            raise HTTPException(400, str(exc)) from exc
        except FileStorageError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.get("/api/files")
    async def list_files(request: Request, goal_id: str | None = None) -> dict:
        files = svc(request).files
        rows = await files.of_goal((await goal_or_404(request, goal_id))["id"]) if goal_id else []
        return {"resume": await files.resume(), "files": rows}

    @app.get("/api/files/{file_id}/download")
    async def download_file(file_id: str, request: Request) -> Response:
        row = await file_or_404(request, file_id)
        try:
            data = await svc(request).files.data(row["id"])
        except FileStorageError as exc:
            raise HTTPException(503, str(exc)) from exc
        return Response(data, media_type=row["content_type"], headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(row['name'], safe='')}",
            "X-Content-Type-Options": "nosniff"})

    @app.delete("/api/files/{file_id}", status_code=204)
    async def delete_file(file_id: str, request: Request) -> None:
        row = await file_or_404(request, file_id)
        await svc(request).files.delete(row["id"])

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
        detail = job_detail(row, await s.artifacts.list_notes(goal_id))
        stage = await PipelineStore(s.store.pool).stage_of(row["role_key"])
        return json.loads(json.dumps({**detail, "pipeline": {"role_key": row["role_key"], "stage": stage}}, default=str))

    def pipeline(request: Request) -> PipelineStore:
        return PipelineStore(svc(request).store.pool)

    def board_or_404(board: str) -> str:
        if board not in BOARDS:
            raise HTTPException(404, "There is no board with that name.")
        return board

    @app.get("/api/board-order/{board}")
    async def board_order(board: str, request: Request) -> dict:
        """The places of the cards that the user moved up or down, for each column."""
        return {"positions": await BoardOrder(svc(request).store.pool).positions(board_or_404(board))}

    @app.put("/api/board-order/{board}", status_code=204)
    async def save_board_order(board: str, body: BoardColumnOrder, request: Request) -> None:
        await BoardOrder(svc(request).store.pool).save(board_or_404(board), body.column, body.keys)

    @app.get("/api/pipeline")
    async def pipeline_items(request: Request) -> dict:
        return await pipeline(request).board()

    @app.put("/api/pipeline/stage")
    async def pipeline_stage(body: PipelineMove, request: Request) -> dict:
        item = await pipeline(request).set_stage(body.role_key, body.stage)
        if item is None:
            raise HTTPException(404, "Role not found")
        return item

    @app.post("/api/pipeline/notes", status_code=201)
    async def pipeline_note(body: PipelineNote, request: Request) -> dict:
        note = await pipeline(request).add_note(body.role_key, body.text)
        if note is None:
            raise HTTPException(404, "Role not found")
        return note

    @app.delete("/api/pipeline/notes/{note_id}", status_code=204)
    async def pipeline_note_delete(note_id: str, request: Request) -> None:
        try:
            uuid.UUID(note_id)
        except ValueError:
            raise HTTPException(404, "Note not found") from None
        if not await pipeline(request).delete_note(note_id):
            raise HTTPException(404, "Note not found")

    @app.get("/api/jobs")
    async def all_jobs(request: Request) -> list[dict]:
        """The roles of all goals, for the Jobs page. The browser filters them."""
        return await svc(request).artifacts.job_cards()

    @app.get("/api/goals/{goal_id}/steps/{tool_use_id}")
    async def step_detail(goal_id: str, tool_use_id: str, request: Request) -> dict:
        """The input and output of one step, for the Activity tab. kept: False for a step that keeps nothing."""
        await goal_or_404(request, goal_id)
        step = await svc(request).store.step_detail(goal_id, tool_use_id)
        if step is None:
            raise HTTPException(404, "This goal has no such step.")
        return {"input": step["input"], "output": step["output"], "truncated": step["truncated"],
                "kept": step["tool"] in DETAIL_TOOLS}

    @app.get("/api/goals/{goal_id}/events.jsonl")
    async def event_log(goal_id: str, request: Request) -> PlainTextResponse:
        """The event log of the goal, one JSON object for each line. It has no step input or output."""
        await goal_or_404(request, goal_id)
        events = await svc(request).store.list_events(goal_id)
        body = "".join(json.dumps(event, default=str, ensure_ascii=False) + "\n" for event in events)
        return PlainTextResponse(body, media_type="application/x-ndjson",
                                 headers={"Content-Disposition": f'attachment; filename="goal-{goal_id[:8]}-events.jsonl"'})

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
        text = await with_pending_note(svc(request).store, goal_id, await with_files(request, goal_id, body))
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

    # The Claude login.

    def login_saved(request: Request) -> bool:
        """Start the app again to load the new login. False when the user must do this."""
        restart = request.app.state.restart
        if restart is None:
            return False
        asyncio.get_running_loop().call_later(RESTART_DELAY_S, restart)
        return True

    @app.post("/api/login")
    async def save_login(request: Request, body: LoginBody):
        value = body.credential.strip()
        try:
            await asyncio.to_thread(check_credential, value)
            mode = save_credential(env_file(), value, account_label=body.account_label, use=True)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None  # from None: the error of the parser can hold the value
        return {"mode": mode, "restarting": login_saved(request)}

    @app.post("/api/login/use")
    async def use_login(request: Request, body: LoginChoice):
        if body.mode not in ENV_NAMES:
            raise HTTPException(404, "There is no login of that type.")
        value = dotenv_values(env_file()).get(ENV_NAMES[body.mode])
        if not value:
            raise HTTPException(409, "That login is not saved.")
        try:
            await asyncio.to_thread(check_credential, value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        choose_credential(env_file(), body.mode)
        return {"mode": body.mode, "restarting": login_saved(request)}

    @app.post("/api/login/subscription/start")
    async def start_subscription_login(request: Request):
        if not shutil.which("claude"):
            raise HTTPException(409, "Claude Code is not installed here. Run `claude setup-token` and paste the token.")
        url = await asyncio.to_thread(request.app.state.setup_token.start)
        if not url:
            raise HTTPException(502, "Claude Code did not show a sign-in page. Run `claude setup-token` and paste the token.")
        return {"url": url}

    @app.post("/api/login/subscription/code")
    async def finish_subscription_login(request: Request, body: LoginCode):
        flow = request.app.state.setup_token
        if not flow.active():
            raise HTTPException(409, "The sign-in ended. Sign in again.")
        token = await asyncio.to_thread(flow.finish, body.code.strip())
        if not token:
            raise HTTPException(400, "Claude did not accept the code. Sign in again.")
        try:
            await asyncio.to_thread(check_credential, token)
            mode = save_credential(env_file(), token, account_label=body.account_label, use=True)
        except ValueError as exc:
            raise HTTPException(400, f"{exc} Sign in again.") from None
        return {"mode": mode, "restarting": login_saved(request)}

    if STATIC_DIR.is_dir():
        @app.get("/goals/{path:path}", include_in_schema=False)
        @app.get("/mandates/{path:path}", include_in_schema=False)
        @app.get("/mandates", include_in_schema=False)
        @app.get("/jobs/{path:path}", include_in_schema=False)
        @app.get("/jobs", include_in_schema=False)
        @app.get("/pipeline/{path:path}", include_in_schema=False)
        @app.get("/pipeline", include_in_schema=False)
        @app.get("/metrics", include_in_schema=False)
        async def goal_page(path: str = "") -> FileResponse:
            """A link to a page of the app: the browser app reads the URL."""
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app
