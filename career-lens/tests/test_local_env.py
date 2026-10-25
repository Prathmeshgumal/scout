"""Local setup writes a private .env once and never replaces one."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from career_lens.store.env_file import parse_env_file


def _script():
    path = Path(__file__).resolve().parents[1] / "scripts/init_local_env.py"
    spec = importlib.util.spec_from_file_location("init_local_env", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".env.example").write_text("EXAMPLE=1\n")
    return tmp_path


def test_setup_writes_a_private_file_with_a_generated_key(tmp_path):
    _script().main(_root(tmp_path))
    env = tmp_path / ".env"
    assert env.stat().st_mode & 0o777 == 0o600
    values = parse_env_file(env.read_text())
    assert values["CAREER_LENS_CREDENTIAL_ACTIVE_KEY"] == "1"
    assert '"1":"' in values["CAREER_LENS_CREDENTIAL_KEYS"]


def test_two_setups_generate_different_keys():
    script = _script()
    assert script.initial_contents("") != script.initial_contents("")


def test_setup_never_replaces_an_existing_file(tmp_path):
    (_root(tmp_path) / ".env").write_text("MINE=1\n")
    _script().main(tmp_path)
    assert (tmp_path / ".env").read_text() == "MINE=1\n"
