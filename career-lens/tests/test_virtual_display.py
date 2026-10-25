"""The native supervisor's graphical display boundary."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from career_lens.store.config import ConfigError


@pytest.fixture
def fake_xvfb(tmp_path, monkeypatch):
    """Make Xvfb's descriptor handshake deterministic without a real server."""
    executable = tmp_path / "Xvfb"

    def install(body: str) -> Path:
        executable.write_text(
            "#!/usr/bin/env python3\n" + body, encoding="utf-8")
        executable.chmod(0o700)
        monkeypatch.setattr(
            "career_lens.fleet.virtual_display.shutil.which",
            lambda name: str(executable) if name == "Xvfb" else None)
        return executable

    return install


def _ready_body(*, then: str = "while True:\n    time.sleep(1)\n") -> str:
    return (
        "import os, sys, time\n"
        "fd = int(sys.argv[sys.argv.index('-displayfd') + 1])\n"
        "os.write(fd, b'83\\n')\n"
        f"{then}"
    )


def test_a_real_x11_display_is_accepted():
    from career_lens.fleet.virtual_display import require_graphical_display

    require_graphical_display({"DISPLAY": ":0"})


def test_a_real_wayland_display_is_accepted():
    from career_lens.fleet.virtual_display import require_graphical_display

    require_graphical_display({"WAYLAND_DISPLAY": "wayland-0"})


def test_a_missing_real_display_names_the_virtual_option():
    from career_lens.fleet.virtual_display import require_graphical_display

    with pytest.raises(
            ConfigError,
            match="No graphical display is available.*--use-virtual-display"):
        require_graphical_display({})


def test_missing_xvfb_has_an_actionable_error(monkeypatch):
    from career_lens.fleet.virtual_display import VirtualDisplay

    monkeypatch.setattr(
        "career_lens.fleet.virtual_display.shutil.which", lambda _name: None)
    with pytest.raises(
            ConfigError,
            match="Xvfb is not installed.*Install the 'xvfb' package"):
        VirtualDisplay((1920, 1080)).start()


def test_cleanup_after_missing_xvfb_preserves_the_desktop_display(monkeypatch):
    from career_lens.fleet.virtual_display import VirtualDisplay

    monkeypatch.setattr(
        "career_lens.fleet.virtual_display.shutil.which", lambda _name: None)
    monkeypatch.setenv("DISPLAY", ":7")
    display = VirtualDisplay((1920, 1080))

    with pytest.raises(ConfigError):
        display.start()
    display.stop()

    assert os.environ["DISPLAY"] == ":7"


def test_command_uses_an_automatic_display_and_requested_screen_size():
    from career_lens.fleet.virtual_display import _command

    assert _command("/usr/bin/Xvfb", 12, (1440, 900)) == [
        "/usr/bin/Xvfb", "-displayfd", "12",
        "-screen", "0", "1440x900x24",
        "-nolisten", "tcp", "-noreset", "-ac",
    ]


def test_start_selects_the_ready_display_and_stop_restores_the_previous_one(
        monkeypatch, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb(_ready_body())
    monkeypatch.setenv("DISPLAY", ":7")
    display = VirtualDisplay((1920, 1080))

    assert display.start() == ":83"
    assert os.environ["DISPLAY"] == ":83"
    display.check()

    display.stop()
    display.stop()
    assert os.environ["DISPLAY"] == ":7"


def test_virtual_display_forces_x11_then_restores_the_desktop_backend(
        monkeypatch, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb(_ready_body())
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    display = VirtualDisplay((1920, 1080))

    display.start()

    assert os.environ["DISPLAY"] == ":83"
    assert "WAYLAND_DISPLAY" not in os.environ
    assert os.environ["XDG_SESSION_TYPE"] == "x11"

    display.stop()

    assert os.environ["DISPLAY"] == ":0"
    assert os.environ["WAYLAND_DISPLAY"] == "wayland-0"
    assert os.environ["XDG_SESSION_TYPE"] == "wayland"


def test_stop_removes_display_when_it_was_originally_absent(
        monkeypatch, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb(_ready_body())
    monkeypatch.delenv("DISPLAY", raising=False)
    display = VirtualDisplay((1920, 1080))

    display.start()
    display.stop()

    assert "DISPLAY" not in os.environ


def test_startup_timeout_is_actionable(monkeypatch, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb("import time\ntime.sleep(30)\n")
    monkeypatch.setenv("DISPLAY", ":7")
    display = VirtualDisplay((1920, 1080), startup_timeout_s=0.01)

    with pytest.raises(
            ConfigError,
            match="Virtual display could not start within 0.01 seconds"):
        display.start()

    assert os.environ["DISPLAY"] == ":7"


def test_invalid_display_number_is_rejected(monkeypatch, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb(
        "import os, sys\n"
        "fd = int(sys.argv[sys.argv.index('-displayfd') + 1])\n"
        "os.write(fd, b'not-a-display\\n')\n")
    monkeypatch.delenv("DISPLAY", raising=False)

    with pytest.raises(
            ConfigError,
            match="Xvfb returned an invalid display number"):
        VirtualDisplay((1920, 1080)).start()

    assert "DISPLAY" not in os.environ


def test_early_exit_includes_xvfb_stderr(fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    fake_xvfb(
        "import sys\n"
        "sys.stderr.write('display allocation failed\\n')\n"
        "raise SystemExit(7)\n")

    with pytest.raises(
            ConfigError,
            match="Xvfb exited with code 7.*display allocation failed"):
        VirtualDisplay((1920, 1080)).start()


def test_an_xvfb_that_exits_after_start_is_reported(tmp_path, fake_xvfb):
    from career_lens.fleet.virtual_display import VirtualDisplay

    trigger = tmp_path / "exit-xvfb"
    fake_xvfb(_ready_body(then=(
        f"while not os.path.exists({str(trigger)!r}):\n"
        "    time.sleep(0.01)\n"
        "sys.stderr.write('server stopped\\n')\n"
        "raise SystemExit(9)\n")))
    display = VirtualDisplay((1920, 1080))
    display.start()
    trigger.touch()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            display.check()
        except ConfigError as exc:
            assert "exit code 9" in str(exc)
            assert "server stopped" in str(exc)
            break
        time.sleep(0.01)
    else:
        pytest.fail("the exited Xvfb process was not reported")

    display.stop()
