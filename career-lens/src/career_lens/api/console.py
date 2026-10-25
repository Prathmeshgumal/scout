"""The web console: one page, and the account and worker commands behind it."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from ..capture.urls import UrlError
from ..fleet.accounts import AccountError, account_projection, create_account, replace_password
from ..fleet.assignments import (
    AssignmentConflict, InvalidAssignmentTransition, assign_account,
    confirm_replace, request_clear, request_login_retry, request_verify,
)
from ..fleet.credentials import CredentialError
from ..fleet.dayplan import DayTemplate, required_minutes
from ..fleet.schedules import ScheduleError, read_schedule, write_schedule
from ..google import policy as google_policy
from ..queue.enqueue import enqueue_dispatch
from .db import database, write
from .security import require_key

PAGE = Path(__file__).with_name("console.html")
LIVE_WITHIN = timedelta(seconds=30)
RECENT_REQUESTS = 50
WEEKDAY_NAMES = {
    1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday",
    6: "Saturday", 7: "Sunday",
}
COMMANDS = {
    "verify": request_verify,
    "retry-login": request_login_retry,
    "confirm-replace": confirm_replace,
    "clear": request_clear,
}

Db = Annotated[Connection, Depends(database)]

page_router = APIRouter()
router = APIRouter(prefix="/console/api", dependencies=[Depends(require_key)])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(Strict):
    label: str = Field(min_length=1, max_length=120)
    username: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    profile_url: str | None = None
    max_attempts_24h: int = Field(ge=1, le=10_000)
    min_interval_seconds: int = Field(ge=0, le=86_400)
    recovery_login_retry_seconds: int = Field(default=1800, ge=0, le=86_400)
    recovery_post_login_seconds: int = Field(default=900, ge=0, le=86_400)


class AssignmentCreate(Strict):
    account_id: str


class GooglePolicyPatch(Strict):
    enabled: bool | None = Field(default=None, strict=True)
    max_searches_hour: int | None = Field(default=None, ge=1, strict=True)
    max_searches_24h: int | None = Field(default=None, ge=1, strict=True)
    min_interval_seconds: int | None = Field(default=None, ge=0, le=60, strict=True)

    @model_validator(mode="after")
    def has_change(self):
        if not self.model_fields_set:
            raise ValueError("at least one Google policy field is required")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("Google policy fields may not be null")
        return self


class ShiftDay(Strict):
    weekday: int = Field(ge=1, le=7)
    working: bool
    start_local: time | None = None
    end_local: time | None = None
    lunch_minutes: int = Field(default=0, ge=0, le=240)
    break_count: int = Field(default=0, ge=0, le=12)
    break_minutes: int = Field(default=0, ge=0, le=120)

    @model_validator(mode="after")
    def bounds_match_working(self):
        if self.working != (self.start_local is not None
                            and self.end_local is not None):
            raise ValueError(
                f"{WEEKDAY_NAMES[self.weekday]}: a working day needs a start "
                "and an end, and a day off needs neither")
        if self.working and self.end_local <= self.start_local:
            raise ValueError(
                f"{WEEKDAY_NAMES[self.weekday]}: the shift ends before it "
                "starts. Overnight shifts are not supported.")
        return self


class PasswordPut(Strict):
    password: str = Field(min_length=1, max_length=1024)


class ShiftPut(Strict):
    enabled: bool
    timezone: str
    start_jitter_minutes: int = Field(default=15, ge=0, le=120)
    end_jitter_minutes: int = Field(default=20, ge=0, le=120)
    days: list[ShiftDay]

    @field_validator("timezone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value}") from exc
        return value

    @model_validator(mode="after")
    def whole_week(self):
        if sorted(day.weekday for day in self.days) != [1, 2, 3, 4, 5, 6, 7]:
            raise ValueError("days must list each weekday from 1 to 7 once")
        return self


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _jsonable_schedule(connection: Connection, account_id: str) -> dict | None:
    schedule = read_schedule(connection, account_id)
    if schedule is None:
        return None
    return {
        "enabled": schedule["enabled"], "timezone": schedule["timezone"],
        "days": [{
            "weekday": day["weekday"], "working": day["working"],
            "start_local": str(day["start_local"]) if day["start_local"] else None,
            "end_local": str(day["end_local"]) if day["end_local"] else None,
        } for day in schedule["days"]],
    }


def _assignments(connection: Connection) -> list[dict]:
    rows = connection.execute(text("""
        SELECT id, account_id, worker_id, state, state_detail
        FROM account_worker_assignment WHERE ended_at IS NULL
    """)).all()
    return [{
        "id": row.id, "account_id": row.account_id, "worker_id": row.worker_id,
        "state": row.state, "detail": row.state_detail,
    } for row in rows]


def _workers(connection: Connection, assignments: list[dict], now: datetime) -> list[dict]:
    by_worker = {item["worker_id"]: item for item in assignments}
    rows = connection.execute(text("""
        SELECT worker.id, worker.local_name, worker.desired_state, worker.health,
               worker.health_detail, worker.last_heartbeat_at,
               supervisor.label AS supervisor_label
        FROM worker JOIN supervisor ON supervisor.id=worker.supervisor_id
        ORDER BY supervisor.label, worker.local_name
    """)).all()
    workers = []
    for row in rows:
        try:
            google = google_policy.projection(
                google_policy.read(connection, row.id, now=now))
        except google_policy.UnknownGooglePolicy:
            google = None
        workers.append({
            "id": row.id, "name": row.local_name,
            "supervisor": row.supervisor_label, "state": row.desired_state,
            "health": row.health, "health_detail": row.health_detail,
            "live": (row.last_heartbeat_at is not None
                     and row.last_heartbeat_at >= now - LIVE_WITHIN),
            "last_heartbeat_at": _iso(row.last_heartbeat_at),
            "google": google,
            "assignment": by_worker.get(row.id),
        })
    return workers


def _accounts(connection: Connection, assignments: list[dict], now: datetime) -> list[dict]:
    by_account = {item["account_id"]: item for item in assignments}
    ids = connection.execute(text(
        "SELECT id FROM linkedin_account WHERE state <> 'retired' ORDER BY label, id")).scalars().all()
    accounts = []
    for account_id in ids:
        value = account_projection(connection, account_id)
        value["assignment"] = by_account.get(account_id)
        value["schedule"] = _jsonable_schedule(connection, account_id)
        value["attempts_24h"] = connection.execute(text("""
            SELECT count(*) FROM linkedin_account_event
            WHERE account_id=:id AND kind='attempt_started' AND occurred_at >= :since
        """), {"id": account_id, "since": now - timedelta(hours=24)}).scalar_one()
        accounts.append(value)
    return accounts


def _requests(connection: Connection) -> list[dict]:
    rows = connection.execute(text("""
        SELECT id, kind, status, url_requested, query_requested, requested_at,
               dispatch_wait_reason
        FROM scrape_request ORDER BY requested_at DESC, id DESC LIMIT :limit
    """), {"limit": RECENT_REQUESTS}).all()
    return [{
        "id": row.id, "kind": row.kind, "status": row.status,
        "target": row.url_requested or row.query_requested,
        "url": row.url_requested,
        "requested_at": _iso(row.requested_at),
        "wait_reason": row.dispatch_wait_reason,
    } for row in rows]


@page_router.get("/console", include_in_schema=False)
def console_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@router.get("/overview")
def overview(connection: Db) -> dict:
    now = datetime.now(UTC)
    assignments = _assignments(connection)
    return {
        "workers": _workers(connection, assignments, now),
        "accounts": _accounts(connection, assignments, now),
        "requests": _requests(connection),
    }


@router.post("/accounts", status_code=201)
def add_account(body: AccountCreate, connection: Db) -> dict:
    try:
        with write(connection) as writable:
            account_id = create_account(writable, **body.model_dump())
            value = account_projection(writable, account_id)
            enqueue_dispatch(writable)
        return value
    except (AccountError, CredentialError, UrlError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        raise HTTPException(409, "that LinkedIn username already exists") from exc


@router.put("/accounts/{account_id}/password")
def put_password(account_id: str, body: PasswordPut, connection: Db) -> dict:
    with write(connection) as writable:
        state = writable.execute(text(
            "SELECT state FROM linkedin_account WHERE id=:id FOR UPDATE"
        ), {"id": account_id}).scalar_one_or_none()
        if state is None:
            raise HTTPException(404, f"no account {account_id}")
        if state == "retired":
            raise HTTPException(409, "this account is retired")
        try:
            replace_password(writable, account_id, body.password)
        except CredentialError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"account_id": account_id}


@router.put("/accounts/{account_id}/shift")
def put_shift(account_id: str, body: ShiftPut, connection: Db) -> dict:
    if account_projection(connection, account_id) is None:
        raise HTTPException(404, f"no LinkedIn account {account_id}")
    # Jitter is what can shrink a shift below what its breaks need on some
    # future day, and only here are the day and its jitter visible together.
    shrink = body.start_jitter_minutes + body.end_jitter_minutes
    for day in body.days:
        if not day.working:
            continue
        span = ((day.end_local.hour * 60 + day.end_local.minute)
                - (day.start_local.hour * 60 + day.start_local.minute))
        if span - shrink < required_minutes(DayTemplate(**day.model_dump())):
            raise HTTPException(422, (
                f"{WEEKDAY_NAMES[day.weekday]}: a shift this short cannot hold "
                f"its lunch and breaks, allowing for up to {shrink} minutes of "
                "jitter"))
    try:
        with write(connection) as writable:
            write_schedule(
                writable, account_id, enabled=body.enabled,
                timezone=body.timezone,
                start_jitter_minutes=body.start_jitter_minutes,
                end_jitter_minutes=body.end_jitter_minutes,
                days=[day.model_dump() for day in body.days])
            enqueue_dispatch(writable)
            return {"schedule": _jsonable_schedule(writable, account_id)}
    except ScheduleError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/workers/{worker_id}/assignments", status_code=202)
def add_assignment(worker_id: str, body: AssignmentCreate, connection: Db) -> dict:
    try:
        with write(connection) as writable:
            assignment_id, operation_id = assign_account(
                writable, account_id=body.account_id, worker_id=worker_id)
        return {"assignment_id": assignment_id, "operation_id": operation_id}
    except AssignmentConflict as exc:
        code = 404 if str(exc).startswith("no ") else 409
        raise HTTPException(code, str(exc)) from exc


@router.post("/assignments/{assignment_id}/{command}", status_code=202)
def assignment_command(assignment_id: str, command: str, connection: Db) -> dict:
    action = COMMANDS.get(command)
    if action is None:
        raise HTTPException(404, f"no command {command}")
    try:
        with write(connection) as writable:
            exists = writable.execute(text(
                "SELECT 1 FROM account_worker_assignment WHERE id=:id"
            ), {"id": assignment_id}).scalar_one_or_none()
            if exists is None:
                raise HTTPException(404, f"no assignment {assignment_id}")
            return {"assignment_id": assignment_id,
                    "operation_id": action(writable, assignment_id)}
    except (AssignmentConflict, InvalidAssignmentTransition) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/workers/{worker_id}/google-policy")
def patch_google_policy(worker_id: str, body: GooglePolicyPatch, connection: Db) -> dict:
    values = body.model_dump(exclude_unset=True)
    try:
        with write(connection) as writable:
            google_policy.update(writable, worker_id, values)
            enqueue_dispatch(writable)
            return {"google": google_policy.projection(
                google_policy.read(writable, worker_id))}
    except google_policy.UnknownGooglePolicy as exc:
        raise HTTPException(404, str(exc)) from exc
