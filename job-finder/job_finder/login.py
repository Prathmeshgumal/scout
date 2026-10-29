"""`job-finder login`: save a Claude login to .env, a subscription token from `claude setup-token` or an API key."""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.text import Text

from .config import AUTH_LABELS
from .login_status import save_account

TOKEN = re.compile(r"sk-ant-oat[0-9A-Za-z_-]+")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
URL_PART = re.compile(r"[A-Za-z0-9%._~&=?/:+#-]+")  # a line that continues a long URL
PROMPT = "Paste code here if prompted >"
PASTE = "Paste a Claude token (from `claude setup-token`) or an Anthropic API key. Typing is hidden:"
PASTE_KEY = "Paste your Anthropic API key (it starts with sk-ant-). Typing is hidden:"
RESTART = "Restart `job-finder serve` or the chat to use it."
RESTART_CONTAINER = "Run `docker compose restart jobfinder` to use it."
KEY_IN_ENV = """\n! ANTHROPIC_API_KEY is also set in .env, and the API key wins over the token.
  To use the token, do one of these, then restart `job-finder serve` or the chat:
  - Back up the key, then remove it from .env.
  - Comment out its line: # ANTHROPIC_API_KEY=..."""
KEY_IN_SHELL = """\n! ANTHROPIC_API_KEY is set in your shell, and the shell value wins over .env.
  To use the token, run: unset ANTHROPIC_API_KEY
  Then restart `job-finder serve` or the chat."""
STYLES = {"title": "bold", "url": "cyan", "ok": "green", "warn": "yellow"}

Out = Callable[..., None]  # out(text, kind="info"); kind: title, step, url, ok, warn or info


class TerminalOutput:
    """Writes the login messages to the terminal, with one style for each kind. The code line changes in place."""

    def __init__(self, console: Console):
        self.console = console
        self.open = False  # the code line has no line end yet

    def out(self, text: str, kind: str = "info") -> None:
        self._end_code_line()
        self.console.print(Text(text, style=STYLES.get(kind, "")), soft_wrap=True)  # soft_wrap: a URL stays whole

    def code(self, length: int) -> None:
        stars = "*" * min(length, 40) + ("…" if length > 40 else "")
        self.console.file.write(f"\r   Code: {stars} ({length} characters)")
        self.console.file.flush()
        self.open = True

    def _end_code_line(self) -> None:
        if self.open:
            self.console.file.write("\n")
            self.open = False


def find_token(output: str) -> str | None:
    """The last subscription token in the output of `claude setup-token`."""
    found = TOKEN.findall(output)
    return found[-1] if found else None


def sign_in_url(text: str) -> str | None:
    """The sign-in URL on the screen of `claude setup-token`. The screen can put its end on the next lines."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = re.search(r"https://\S+", line)
        if match and "oauth" in match.group(0):
            url = match.group(0)
            for following in lines[index + 1:]:
                part = following.strip()
                if not part or not URL_PART.fullmatch(part):
                    break
                url += part
            return url
    return None


def pasted_length(screen: str) -> int:
    """The length of the code after the last paste prompt. The screen shows one * for each character."""
    at = screen.rfind(PROMPT)
    if at < 0:
        return 0
    lines = screen[at + len(PROMPT):].split("\n")
    length = lines[0].count("*") if set(lines[0].strip()) <= {"*"} else 0
    for line in lines[1:]:
        if not line.strip() or set(line.strip()) != {"*"}:
            break
        length += line.count("*")
    return length


class SetupTokenView:
    """Shows only what the user needs from the screen of `claude setup-token`: the sign-in URL, the paste
    hint and the length of the pasted code. Never the token: the screen shows it, but job-finder saves it."""

    def __init__(self, out: Out, code: Callable[[int], None]):
        self.out, self.code = out, code
        self.text = ""
        self.shown = False
        self.length = 0

    def feed(self, chunk: str) -> None:
        self.text += chunk
        screen = ANSI.sub("", self.text).replace("\r", "")
        if not self.shown and PROMPT in screen:  # the URL comes before the paste prompt
            self.shown = True
            if url := sign_in_url(screen):
                self.out("   No browser? Open this URL:", "step")
                self.out(f"   {url}", "url")
            self.out("\n2. If the browser shows a code, paste it here and press Enter.", "step")
        if self.shown and (length := pasted_length(screen)) > self.length:  # a frame can arrive in parts
            self.length = length
            self.code(length)


def run_setup_token(out: Out, code: Callable[[int], None]) -> str:
    """Run `claude setup-token`, show the parts of its screen that the user needs, and return its output."""
    process = subprocess.Popen(["claude", "setup-token"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    view, decoder, chunks = SetupTokenView(out, code), codecs.getincrementaldecoder("utf-8")(errors="replace"), []
    while chunk := os.read(process.stdout.fileno(), 4096):
        text = decoder.decode(chunk)
        chunks.append(text)
        view.feed(text)
    process.wait()
    return "".join(chunks)


def save_credential(env_file: Path, value: str, *, account_label: str = "") -> str:
    """Save a token or a key to .env. The other one stays: the user decides what to do with it."""
    if value.startswith("sk-ant-oat"):
        mode, name = "oauth_token", "CLAUDE_CODE_OAUTH_TOKEN"
    elif value.startswith("sk-ant-"):
        mode, name = "api_key", "ANTHROPIC_API_KEY"
    else:
        raise ValueError("That does not look like a Claude token or an Anthropic API key.")
    if len(account_label) > 120 or any(c in account_label for c in "\r\n") or "sk-ant-" in account_label:
        raise ValueError("Use an account label of 120 characters maximum. Do not enter a credential as the label.")
    env_file.touch(mode=0o600, exist_ok=True)
    set_key(env_file, name, value, quote_mode="never")
    env_file.chmod(0o600)
    if os.geteuid() == 0:  # in the container: set_key wrote a new file, and the host user must still edit it
        folder = env_file.parent.stat()
        os.chown(env_file, folder.st_uid, folder.st_gid)
    save_account(env_file, mode, value, account_label)
    return mode


def restart_hint(environ: Mapping[str, str] = os.environ) -> str:
    """How to use a new login: the container names its env file."""
    return RESTART_CONTAINER if environ.get("JOB_FINDER_ENV_FILE") else RESTART


def _from_shell(name: str, environ: Mapping[str, str], before: Mapping[str, str | None]) -> bool:
    """True when the value comes from the shell: the CLI loads .env, but a shell value wins."""
    return bool(environ.get(name)) and environ.get(name) != (before.get(name) or "")


def login(env_file: Path, *, api_key: bool, ask: Callable[[str], str], out: Out,
          ask_label: Callable[[], str] = lambda: "",
          code: Callable[[int], None] = lambda length: None, which: Callable[[str], str | None] = shutil.which,
          setup_token: Callable[[Out, Callable[[int], None]], str] = run_setup_token,
          environ: Mapping[str, str] = os.environ) -> int:
    """The exit code. It never shows the token or the key."""
    before = dotenv_values(env_file) if env_file.exists() else {}
    value = None
    if not api_key:
        if which("claude"):
            out("Claude login\n", "title")
            out("1. Approve the login in the browser window that opens.", "step")
            value = find_token(setup_token(out, code))
            out("")
            if not value:
                out("! `claude setup-token` gave no token.", "warn")
        else:
            out("Claude Code is not installed, so a token cannot be created here.")
    if not value:
        value = ask(PASTE_KEY if api_key else PASTE).strip()
    if not value:
        out("! Nothing entered; skipped.", "warn")
        return 1
    try:
        mode = save_credential(env_file, value, account_label=ask_label())
    except ValueError as exc:
        out(f"! {exc}", "warn")
        return 1
    out(f"✓ Saved the {AUTH_LABELS[mode]} to .env.", "ok")
    if mode == "api_key":
        out(f"  job-finder uses the API key. It wins over a subscription token.\n  {restart_hint(environ)}")
        if _from_shell("ANTHROPIC_API_KEY", environ, before):
            out("! ANTHROPIC_API_KEY is also set in your shell, and the shell value wins over .env.", "warn")
        return 0
    if _from_shell("ANTHROPIC_API_KEY", environ, before):
        out(KEY_IN_SHELL, "warn")
    elif dotenv_values(env_file).get("ANTHROPIC_API_KEY"):
        out(KEY_IN_ENV, "warn")
    else:
        out(f"  ANTHROPIC_API_KEY is not set, so job-finder uses the subscription token.\n  {restart_hint(environ)}")
    if _from_shell("CLAUDE_CODE_OAUTH_TOKEN", environ, before):
        out("! CLAUDE_CODE_OAUTH_TOKEN is also set in your shell, and the shell value wins over .env.", "warn")
    return 0
