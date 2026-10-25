"""Graphical display lifecycle for native browser workers."""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from typing import BinaryIO

from ..store.config import ConfigError

DEFAULT_VIRTUAL_DISPLAY_SIZE = (1920, 1080)
DEFAULT_STARTUP_TIMEOUT_S = 5.0
_STOP_TIMEOUT_S = 2.0
_STDERR_LIMIT = 2_000
_DISPLAY_ENVIRONMENT = ("DISPLAY", "WAYLAND_DISPLAY", "XDG_SESSION_TYPE")


def require_graphical_display(
        environ: Mapping[str, str] | None = None) -> None:
    """Require a desktop display when the virtual one was not requested."""
    values = os.environ if environ is None else environ
    if values.get("DISPLAY") or values.get("WAYLAND_DISPLAY"):
        return
    raise ConfigError(
        "No graphical display is available. Start from a desktop session "
        "or use --use-virtual-display.")


def _command(executable: str, display_fd: int,
             size: tuple[int, int]) -> list[str]:
    width, height = size
    return [
        executable,
        "-displayfd", str(display_fd),
        "-screen", "0", f"{width}x{height}x24",
        "-nolisten", "tcp",
        "-noreset",
        "-ac",
    ]


class VirtualDisplay:
    """One collision-free Xvfb server inherited by native worker processes."""

    def __init__(self, size: tuple[int, int], *,
                 startup_timeout_s: float = DEFAULT_STARTUP_TIMEOUT_S):
        self.size = size
        self.startup_timeout_s = startup_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr: BinaryIO | None = None
        self._previous_environment: dict[str, str | None] = {}
        self._environment_captured = False
        self._selected_display: str | None = None

    def _stderr_tail(self) -> str:
        if self._stderr is None:
            return ""
        self._stderr.flush()
        self._stderr.seek(0)
        text = self._stderr.read().decode("utf-8", errors="replace")
        return " ".join(text[-_STDERR_LIMIT:].split())

    def _stop_process(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=_STOP_TIMEOUT_S)

    def _restore_environment(self) -> None:
        if not self._environment_captured:
            return
        for name, value in self._previous_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._previous_environment = {}
        self._environment_captured = False

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None

    def _failed_start(self, message: str) -> ConfigError:
        self._stop_process()
        self._restore_environment()
        self._process = None
        self._selected_display = None
        self._close_stderr()
        return ConfigError(message)

    def start(self) -> str:
        """Start Xvfb, wait for its ready signal, and select its display."""
        executable = shutil.which("Xvfb")
        if executable is None:
            raise ConfigError(
                "Virtual display requested, but Xvfb is not installed. "
                "Install the 'xvfb' package or start without "
                "--use-virtual-display.")

        self._previous_environment = {
            name: os.environ.get(name) for name in _DISPLAY_ENVIRONMENT}
        self._environment_captured = True
        read_fd, write_fd = os.pipe()
        self._stderr = tempfile.TemporaryFile(mode="w+b")
        try:
            try:
                self._process = subprocess.Popen(
                    _command(executable, write_fd, self.size),
                    stdout=subprocess.DEVNULL,
                    stderr=self._stderr,
                    pass_fds=(write_fd,),
                )
            except OSError as exc:
                raise self._failed_start(
                    "Virtual display could not start: "
                    f"{type(exc).__name__}: {exc}") from None
            finally:
                os.close(write_fd)

            ready, _, _ = select.select(
                [read_fd], [], [], self.startup_timeout_s)
            if not ready:
                raise self._failed_start(
                    "Virtual display could not start within "
                    f"{self.startup_timeout_s:g} seconds.")

            answer = os.read(read_fd, 64).strip()
            if not answer:
                process = self._process
                if process is not None and process.poll() is None:
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                if process is not None and process.poll() is not None:
                    detail = self._stderr_tail()
                    suffix = f": {detail}" if detail else "."
                    raise self._failed_start(
                        "Virtual display could not start: Xvfb exited with "
                        f"code {process.returncode}{suffix}")
            try:
                number = answer.decode("ascii")
            except UnicodeDecodeError:
                number = ""
            if not number.isdecimal():
                raise self._failed_start(
                    "Virtual display could not start: Xvfb returned an "
                    "invalid display number.")

            self._selected_display = f":{number}"
            os.environ["DISPLAY"] = self._selected_display
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.environ["XDG_SESSION_TYPE"] = "x11"
            return self._selected_display
        finally:
            os.close(read_fd)

    def check(self) -> None:
        """Fail when the X server Chrome is using has disappeared."""
        process = self._process
        if process is None or process.poll() is None:
            return
        detail = self._stderr_tail()
        suffix = f": {detail}" if detail else "."
        raise ConfigError(
            "Virtual display stopped unexpectedly with exit code "
            f"{process.returncode}{suffix}")

    def stop(self) -> None:
        """Stop Xvfb and restore DISPLAY; safe after failure or repeated calls."""
        self._stop_process()
        self._process = None
        self._selected_display = None
        self._close_stderr()
        self._restore_environment()
