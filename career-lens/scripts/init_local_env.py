"""Create a private .env with a generated credential key. Never overwrites one."""

from __future__ import annotations

import base64
import os
import secrets
from pathlib import Path


def initial_contents(example: str) -> str:
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return (example.rstrip("\n")
            + "\n\nCAREER_LENS_CREDENTIAL_KEYS='{\"1\":\"" + key + "\"}'"
            + "\nCAREER_LENS_CREDENTIAL_ACTIVE_KEY=1\n")


def main(root: Path | None = None) -> None:
    root = root or Path(__file__).resolve().parent.parent
    env = root / ".env"
    if env.exists():
        print(f"{env} already exists; left it alone.")
        return
    contents = initial_contents((root / ".env.example").read_text())
    descriptor = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(contents)
    print(f"Wrote {env}.")


if __name__ == "__main__":
    main()
