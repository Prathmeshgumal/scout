"""Read a `.env` file the way docker compose reads it, not the way a shell does.

The same file is read by two parsers today. `docker compose` reads it
literally: `KEY={"1":"abc"}` is the six-character-plus JSON object it looks
like. The supervisor runs on the host and was started with

    set -a; source .env; set +a

which hands the file to the shell, and the shell removes the inner quotes
before the value ever reaches Python -- so the API held a keyring the
supervisor could not parse, from one unchanged line. Nothing about the file
says which of the two readings is meant, so the fix is to stop having two:
`career-lens supervisor --env-file .env` applies the compose reading, and the
supervisor sees exactly what the containers see.

Values already present in the environment win, as they do under compose: an
explicit `FOO=bar career-lens supervisor` still overrides the file.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import MutableMapping

from .config import ConfigError

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: What a backslash means inside a double-quoted value. A single-quoted value
#: has no escapes at all, which is why quoting a JSON keyring with `'` is the
#: form the runbook asks for.
ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _closed(text: str, quote: str) -> tuple[str, str] | None:
    """Split `text` at the closing `quote`, or return None if it never closes."""
    body: list[str] = []
    index = 0
    while index < len(text):
        character = text[index]
        if quote == '"' and character == "\\" and index + 1 < len(text):
            body.append(text[index:index + 2])
            index += 2
            continue
        if character == quote:
            return "".join(body), text[index + 1:]
        body.append(character)
        index += 1
    return None


def _unescape(body: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\" and index + 1 < len(body):
            following = body[index + 1]
            out.append(ESCAPES.get(following, "\\" + following))
            index += 2
            continue
        out.append(character)
        index += 1
    return "".join(out)


def _uncommented(value: str) -> str:
    """Drop a trailing comment from an unquoted value.

    Only a `#` that follows whitespace starts one, so a value that merely
    contains a hash keeps it.
    """
    for index, character in enumerate(value):
        if character == "#" and (index == 0 or value[index - 1].isspace()):
            return value[:index]
    return value


def parse_env_file(content: str, *, source: str = ".env") -> dict[str, str]:
    """Parse compose's env-file syntax, refusing anything ambiguous.

    Raising on a line that cannot be read is deliberate. The failure this
    replaces was silent: a value that parsed differently in two places and
    said nothing about it until a login opened a browser and could not
    decrypt a password.
    """
    values: dict[str, str] = {}
    lines = content.splitlines()
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index].strip()
        index += 1
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if not separator or not NAME.fullmatch(name):
            raise ConfigError(
                f"{source} line {number} is not NAME=value")
        value = value.strip()
        if value[:1] in {"'", '"'}:
            quote = value[0]
            pending = value[1:]
            closed = _closed(pending, quote)
            while closed is None:
                if index >= len(lines):
                    raise ConfigError(
                        f"{source}: {name} opens a {quote} that never closes")
                pending += "\n" + lines[index]
                index += 1
                closed = _closed(pending, quote)
            resolved, rest = closed
            resolved = _unescape(resolved) if quote == '"' else resolved
            rest = rest.strip()
            if rest and not rest.startswith("#"):
                raise ConfigError(
                    f"{source} line {number}: {name} has text after its "
                    f"closing {quote}")
        else:
            resolved = _uncommented(value).strip()
        values[name] = resolved
    return values


def load_env_file(path: str | Path,
                  environ: MutableMapping[str, str] | None = None,
                  ) -> tuple[list[str], list[str]]:
    """Apply an env file to the process, leaving anything already set alone.

    Returns the names it set and the names it found already present, so the
    caller can say what it did rather than change the environment silently.
    """
    target = os.environ if environ is None else environ
    location = Path(path)
    try:
        content = location.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {location}: {exc.strerror}") from None
    values = parse_env_file(content, source=str(location))
    applied: list[str] = []
    kept: list[str] = []
    for name, value in values.items():
        if name in target:
            kept.append(name)
            continue
        target[name] = value
        applied.append(name)
    return applied, kept
