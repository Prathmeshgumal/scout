"""Account labels and credential changes. This module never returns credentials to the browser."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

from .config import AUTH_VARS, LOGIN_CHOICE, Settings


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def metadata_path(path: Path) -> Path:
    return path.with_name(path.name + ".login.json")


def read_metadata(path: Path) -> dict:
    try:
        data = json.loads(metadata_path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_account(path: Path, mode: str, value: str, label: str) -> None:
    """Replace the label when a credential changes. A blank label removes the earlier label."""
    data = read_metadata(path)
    data[mode] = {"fingerprint": fingerprint(value), "account_label": label.strip() or None,
                  "saved_at": datetime.now(UTC).isoformat()}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
            temporary = out.name
            json.dump(data, out)
            if os.geteuid() == 0:
                folder = path.parent.stat()
                os.fchown(out.fileno(), folder.st_uid, folder.st_gid)
        os.replace(temporary, metadata_path(path))
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class LoginStatus:
    """Keep details for the loaded credential. Compare the saved credential without loading it."""

    def __init__(self, settings: Settings, path: Path):
        self.path = path
        value = settings.credential
        self.loaded = fingerprint(value)
        self.loaded_at = datetime.now(UTC).isoformat() if value else None
        row = read_metadata(path).get(settings.auth_mode)
        matches = isinstance(row, dict) and row.get("fingerprint") == self.loaded
        self.label = row.get("account_label") if matches else None
        self.saved_at = row.get("saved_at") if matches else None
        self.file_had_auth = any(name in dotenv_values(path, interpolate=False) for name in AUTH_VARS)

    def snapshot(self) -> dict:
        check_error = False
        changed = False
        saved_modes = {"api_key": False, "oauth_token": False}
        try:
            # Read explicitly so a permission error does not look like an empty configuration.
            if self.path.exists():
                with self.path.open() as source:
                    values = dotenv_values(stream=source, interpolate=False)
                configured = self.file_had_auth or any(name in values for name in AUTH_VARS)
                key, token = values.get("ANTHROPIC_API_KEY"), values.get("CLAUDE_CODE_OAUTH_TOKEN")
                chosen = token if values.get(LOGIN_CHOICE) == "oauth_token" else None
                saved = (chosen or key or token or "").strip()
                changed = configured and fingerprint(saved) != self.loaded
                saved_modes = {"api_key": bool(values.get("ANTHROPIC_API_KEY")),
                               "oauth_token": bool(values.get("CLAUDE_CODE_OAUTH_TOKEN"))}
            else:
                changed = self.file_had_auth
        except OSError:
            check_error = True
        return {"account_label": self.label, "saved_at": self.saved_at, "loaded_at": self.loaded_at,
                "restart_required": changed, "check_error": check_error, "saved": saved_modes}
