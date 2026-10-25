"""Provider-neutral policy for recovering signed-out account sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import random
from typing import Literal

RecoveryPhase = Literal["before_relogin", "after_relogin"]
RecoveryAction = Literal["cooldown", "login", "attention", "resume"]


@dataclass(frozen=True)
class RecoveryPolicy:
    before_relogin: timedelta
    after_relogin: timedelta
    failure_limit: int
    variance: float = 0.0


def configured_policy(
    login_retry_seconds: int,
    post_login_seconds: int,
) -> RecoveryPolicy:
    """Translate persisted account targets into the shared recovery policy."""
    return RecoveryPolicy(
        before_relogin=timedelta(seconds=login_retry_seconds),
        after_relogin=timedelta(seconds=post_login_seconds),
        failure_limit=2,
        variance=0.2,
    )


def _deadline(now: datetime, target: timedelta, variance: float) -> datetime:
    seconds = target.total_seconds()
    spread = seconds * variance
    return now + timedelta(seconds=random.uniform(
        max(0.0, seconds - spread), seconds + spread,
    ))


@dataclass(frozen=True)
class RecoveryState:
    phase: RecoveryPhase | None = None
    not_before: datetime | None = None
    failures: int = 0


@dataclass(frozen=True)
class RecoveryTransition:
    action: RecoveryAction
    state: RecoveryState


def signed_out(
    state: RecoveryState,
    *,
    now: datetime,
    policy: RecoveryPolicy,
) -> RecoveryTransition:
    """Schedule a first login or allow one whose pre-login rest has elapsed."""
    if state.phase == "before_relogin" and state.not_before is not None:
        if state.not_before <= now:
            action: RecoveryAction = (
                "attention"
                if state.failures >= policy.failure_limit
                else "login"
            )
            return RecoveryTransition(action, state)
        return RecoveryTransition("cooldown", state)
    return RecoveryTransition(
        "cooldown",
        RecoveryState(
            phase="before_relogin",
            not_before=_deadline(now, policy.before_relogin, policy.variance),
            failures=state.failures,
        ),
    )


def login_finished(
    state: RecoveryState,
    *,
    landed: bool,
    now: datetime,
    policy: RecoveryPolicy,
) -> RecoveryTransition:
    """Apply a post-login rest or back off another bounded login attempt."""
    if landed:
        return RecoveryTransition(
            "cooldown",
            RecoveryState(
                phase="after_relogin",
                not_before=_deadline(now, policy.after_relogin, policy.variance),
                failures=0,
            ),
        )
    failures = state.failures + 1
    if failures >= policy.failure_limit:
        return RecoveryTransition(
            "attention",
            RecoveryState(failures=failures),
        )
    return RecoveryTransition(
        "cooldown",
        RecoveryState(
            phase="before_relogin",
            not_before=_deadline(now, policy.before_relogin, policy.variance),
            failures=failures,
        ),
    )


def authenticated(state: RecoveryState, *, now: datetime) -> RecoveryTransition:
    """Keep a recovered account resting until its post-login deadline."""
    if (
        state.phase == "after_relogin"
        and state.not_before is not None
        and state.not_before > now
    ):
        return RecoveryTransition("cooldown", state)
    return RecoveryTransition("resume", RecoveryState())
