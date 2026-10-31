"""`job-finder login`: save a Claude login to .env, a subscription token from `claude setup-token` or an API key."""

from __future__ import annotations

import codecs
import fcntl
import os
import pty
import re
import shutil
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key
from rich.console import Console
from rich.text import Text

from .config import AUTH_LABELS, AUTH_VARS, LOGIN_CHOICE
from .login_status import save_account

TOKEN = re.compile(r"sk-ant-oat[0-9A-Za-z_-]+")
CREDENTIAL = re.compile(r"sk-ant-(?!oat$)[0-9A-Za-z_-]+")  # one word: a value cannot add a line to .env
ENV_NAMES = {"api_key": "ANTHROPIC_API_KEY", "oauth_token": "CLAUDE_CODE_OAUTH_TOKEN"}
COLUMN_MOVE = re.compile(r"\x1b\[[0-9]*G")  # on a terminal, Claude Code moves the cursor and writes no spaces
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


def save_credential(env_file: Path, value: str, *, account_label: str = "", use: bool = False) -> str:
    """Save a token or a key to .env. The other one stays. With use, this login is the one in use.
    With no use, the choice goes: the terminal login says that the API key wins, and that must stay true."""
    if not CREDENTIAL.fullmatch(value):
        raise ValueError("That does not look like a Claude token or an Anthropic API key.")
    mode = "oauth_token" if value.startswith("sk-ant-oat") else "api_key"
    name = ENV_NAMES[mode]
    if len(account_label) > 120 or any(c in account_label for c in "\r\n") or "sk-ant-" in account_label:
        raise ValueError("Use an account label of 120 characters maximum. Do not enter a credential as the label.")
    env_file.touch(mode=0o600, exist_ok=True)
    set_key(env_file, name, value, quote_mode="never")
    if use:
        set_key(env_file, LOGIN_CHOICE, mode, quote_mode="never")
    elif LOGIN_CHOICE in dotenv_values(env_file):
        unset_key(env_file, LOGIN_CHOICE, quote_mode="never")
    _protect(env_file)
    save_account(env_file, mode, value, account_label)
    return mode


def choose_credential(env_file: Path, mode: str) -> None:
    """Use a login that .env already has. The other login stays."""
    if not dotenv_values(env_file).get(ENV_NAMES[mode]):
        raise ValueError("That login is not saved.")
    set_key(env_file, LOGIN_CHOICE, mode, quote_mode="never")
    _protect(env_file)


def _protect(env_file: Path) -> None:
    env_file.chmod(0o600)
    if os.geteuid() == 0:  # in the container: dotenv wrote a new file, and the host user must still edit it
        folder = env_file.parent.stat()
        os.chown(env_file, folder.st_uid, folder.st_gid)


class SetupTokenFlow:
    """`claude setup-token` for the web app. The screen of the process stays here: only the URL goes to the browser.

    Claude Code draws this screen only on a terminal, so the process gets a pseudo-terminal. The terminal is wide,
    so that the URL and the token each stay on one line."""

    COLUMNS = 2000
    MAX_AGE_S = 600

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.master: int | None = None
        self.started = 0.0
        self.chunks: list[bytes] = []
        self.lock = threading.Lock()

    def screen(self) -> str:
        with self.lock:
            text = b"".join(self.chunks).decode("utf-8", "replace")
        return ANSI.sub("", COLUMN_MOVE.sub(" ", text)).replace("\r", "")

    def active(self) -> bool:
        return (self.process is not None and self.process.poll() is None
                and time.monotonic() - self.started < self.MAX_AGE_S)

    def start(self, timeout: float = 30.0) -> str | None:
        """Start a new sign-in and return its URL. None when Claude Code shows no URL in time."""
        self.close()
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.COLUMNS, 0, 0))
        # The sign-in must not use the login of the app.
        env = {name: value for name, value in os.environ.items() if name not in AUTH_VARS}
        self.process = subprocess.Popen(["claude", "setup-token"], stdin=slave, stdout=slave, stderr=slave, env=env,
                                        close_fds=True, start_new_session=True)
        os.close(slave)
        self.master, self.started, self.chunks = master, time.monotonic(), []
        threading.Thread(target=self._read, args=(master,), daemon=True).start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self.process.poll() is None:
            screen = self.screen()
            if PROMPT in screen:
                return sign_in_url(screen)
            time.sleep(0.05)
        self.close()
        return None

    def finish(self, code: str, timeout: float = 30.0) -> str | None:
        """Give the code to Claude Code and return the new token. None when Claude Code gives no token."""
        os.write(self.master, code.encode())
        time.sleep(0.3)  # Enter in the same write can become a part of the pasted text
        os.write(self.master, b"\r")
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if token := find_token(self.screen()):
                    return token
                if self.process.poll() is not None:
                    time.sleep(0.2)  # the last output can still be on its way
                    return find_token(self.screen())
                time.sleep(0.05)
            return None
        finally:
            self.close()

    def close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        self.process = None
        if self.master is not None:
            try:
                os.close(self.master)
            except OSError:
                pass
            self.master = None
        with self.lock:
            self.chunks = []  # the screen can hold the token

    def _read(self, master: int) -> None:
        try:
            while chunk := os.read(master, 4096):
                with self.lock:
                    self.chunks.append(chunk)
        except OSError:  # the process ended, or close() closed the terminal
            pass


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
