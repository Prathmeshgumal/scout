"""Strict local declaration of one supervisor and the workers it runs.

A supervisor is the process on this machine. A worker is one Chrome profile
with one LinkedIn account in it, running one job at a time. Both were called
"worker" before the vocabulary was settled, which is why the file this reads
was called `worker.json` and its array was called `workers`.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

CONFIG_VARIABLE = "CAREER_LENS_SUPERVISOR_CONFIG"
CONFIG_NAME = "supervisor.json"

# What the same things were called before. Kept only to recognise a file that
# has not been migrated and say precisely what to edit -- nothing here is
# accepted as valid input.
FORMER_VARIABLE = "CAREER_LENS_WORKER_CONFIG"
FORMER_NAME = "worker.json"
FORMER_KEY = "slots"


class SupervisorConfigError(ValueError):
    """The local supervisor declaration is unsafe or malformed."""


@dataclass(frozen=True)
class WorkerConfig:
    id: str
    profile: str
    enabled: bool = True


@dataclass(frozen=True)
class SupervisorConfig:
    id: str
    label: str
    workers: tuple[WorkerConfig, ...]


def config_directory() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "career_lens"


def supervisor_config_path() -> Path:
    """Where the declaration lives, refusing the name it used to have.

    A file still called `worker.json` is not read as a default. Loading it
    silently would leave an operator believing they had edited the file this
    process uses, and the rename is the one moment where saying so costs
    nothing.
    """
    explicit = os.environ.get(CONFIG_VARIABLE)
    if explicit:
        return Path(explicit)
    if os.environ.get(FORMER_VARIABLE):
        raise SupervisorConfigError(
            f"{FORMER_VARIABLE} is no longer read; export the same value as "
            f"{CONFIG_VARIABLE}")
    directory = config_directory()
    current, former = directory / CONFIG_NAME, directory / FORMER_NAME
    if not current.exists() and former.exists():
        raise SupervisorConfigError(
            f"{former} is no longer read: rename it to {current}, and rename "
            f"its {FORMER_KEY!r} array to 'workers'")
    return current


def _keys(value: dict, allowed: set[str], where: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise SupervisorConfigError(
            f"unknown {where} keys: {', '.join(sorted(unknown))}")


def _identifier(value, where: str) -> str:
    if not isinstance(value, str) or ID.fullmatch(value) is None:
        raise SupervisorConfigError(
            f"{where} must match {ID.pattern}")
    return value


def load_supervisor_config(path: Path | None = None) -> SupervisorConfig:
    source = path or supervisor_config_path()
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SupervisorConfigError(
            f"cannot read supervisor declaration {source}: {exc}") from None
    if not isinstance(raw, dict):
        raise SupervisorConfigError(
            "supervisor declaration must be a JSON object")
    # Before the general unknown-key check, which would call this "unknown"
    # when it is in fact known and renamed.
    if FORMER_KEY in raw and "workers" not in raw:
        raise SupervisorConfigError(
            f"{source} declares {FORMER_KEY!r}: rename that array to "
            "'workers'")
    _keys(raw, {"id", "label", "workers"}, "supervisor")
    supervisor_id = _identifier(raw.get("id"), "supervisor id")
    label = raw.get("label")
    if not isinstance(label, str) or not (label := " ".join(label.split())):
        raise SupervisorConfigError("supervisor label must be non-empty text")
    values = raw.get("workers")
    if not isinstance(values, list) or not values:
        raise SupervisorConfigError(
            "supervisor workers must be a non-empty array")

    workers: list[WorkerConfig] = []
    ids: set[str] = set()
    profiles: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise SupervisorConfigError(f"worker {index} must be an object")
        _keys(value, {"id", "profile", "enabled"}, f"worker {index}")
        worker_id = _identifier(value.get("id"), f"worker {index} id")
        profile = value.get("profile")
        if (not isinstance(profile, str) or ID.fullmatch(profile) is None
                or profile in {".", ".."}):
            raise SupervisorConfigError(
                f"worker {index} profile is not a safe name")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SupervisorConfigError(
                f"worker {index} enabled must be boolean")
        if worker_id in ids:
            raise SupervisorConfigError(f"duplicate worker id {worker_id}")
        if profile in profiles:
            raise SupervisorConfigError(f"duplicate worker profile {profile}")
        ids.add(worker_id)
        profiles.add(profile)
        workers.append(WorkerConfig(worker_id, profile, enabled))
    return SupervisorConfig(supervisor_id, label, tuple(workers))
