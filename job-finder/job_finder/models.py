"""The models and the effort levels that a user can choose for a goal. The .env gives the default.

The list comes from the SDK: Claude Code gives its models, with the effort levels of each model, when a session
starts. A new SDK version brings new models without a change here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from .config import Settings, claude_env

log = logging.getLogger(__name__)

# The names that the UI shows for the SDK's effort levels. A level that is not here shows its id.
EFFORT_LABELS = {"low": "Low", "medium": "Medium", "high": "High", "xhigh": "Extra", "max": "Max"}
CREDITS = "Requires usage credits"  # Claude Code adds this to a model that needs usage credits on this account
RETRY_S = 60.0  # after a failed read, the menu has only the default model for this long


def effort_label(effort: str) -> str:
    return EFFORT_LABELS.get(effort, effort.capitalize())


def from_sdk(raw: list[dict]) -> list[dict]:
    """The menu from the SDK's models. The description is "Name · detail", with a note on usage credits for some
    models. The SDK gives the default model twice: as "default" and by its name."""
    menu, seen = [], set()
    for model in raw:
        model_id = model.get("resolvedModel") or model.get("value")
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        name, *details = [part.strip() for part in str(model.get("description") or "").split("·")]
        entry = {"id": model_id, "label": name or model.get("displayName") or model_id,
                 "detail": " · ".join(d for d in details if d != CREDITS),
                 "efforts": list(model.get("supportedEffortLevels") or []) if model.get("supportsEffort") else []}
        if CREDITS in details:
            entry["badge"] = CREDITS
        menu.append(entry)
    return menu


async def read_sdk_models(settings: Settings) -> list[dict]:
    """Start Claude Code, read its models and stop it. This sends no message to the model. Claude Code reads the
    account here, e.g. which models need usage credits: CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC stops that."""
    env = {"CLAUDE_CONFIG_DIR": str(settings.claude_config_dir), **claude_env(settings)}
    settings.claude_config_dir.mkdir(parents=True, exist_ok=True)
    settings.agent_cwd.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(setting_sources=[], tools=[], env=env, cwd=str(settings.agent_cwd))
    async with ClaudeSDKClient(options) as client:
        info = await client.get_server_info() or {}
    return from_sdk(info.get("models") or [])


class ModelList:
    """The models of the SDK, read on the first use and kept. Without the SDK, only the default model of .env."""

    def __init__(self, loader: Callable[[], Awaitable[list[dict]]], default_model: str, default_effort: str):
        self._loader = loader
        self.default_model, self.default_effort = default_model, default_effort
        self._models: list[dict] | None = None
        self._failed_at = 0.0
        self._lock = asyncio.Lock()

    async def menu(self) -> list[dict]:
        async with self._lock:
            if self._models is None and time.monotonic() - self._failed_at >= RETRY_S:
                try:
                    self._models = await self._loader()
                except Exception:
                    log.exception("could not read the models from the SDK")
                    self._failed_at = time.monotonic()
        models = self._models or []
        if any(m["id"] == self.default_model for m in models):
            return models
        default = {"id": self.default_model, "label": self.default_model, "detail": "The default of this server",
                   "efforts": [self.default_effort]}
        return [default, *models]
