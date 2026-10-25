"""Local supervisor declarations cannot alias or escape browser profiles."""

from __future__ import annotations

import json

import pytest


def _write(tmp_path, value):
    path = tmp_path / "supervisor.json"
    path.write_text(json.dumps(value))
    return path


def _declaration(profile="research-a"):
    return {
        "id": "research-laptop", "label": "Research laptop",
        "workers": [{"id": "research-a", "profile": profile}],
    }


def test_loads_multiple_simultaneous_workers(tmp_path):
    from career_lens.fleet.config import load_supervisor_config

    value = _declaration()
    value["workers"].append({
        "id": "research-b", "profile": "research-b", "enabled": False,
    })
    config = load_supervisor_config(_write(tmp_path, value))
    assert config.id == "research-laptop"
    assert [worker.id for worker in config.workers] == ["research-a", "research-b"]
    assert config.workers[0].enabled is True
    assert config.workers[1].enabled is False


@pytest.mark.parametrize("profile", ["../other", "/tmp/profile", "a/b", "", ".", ".."])
def test_profile_name_cannot_escape_the_profile_root(tmp_path, profile):
    from career_lens.fleet.config import SupervisorConfigError, load_supervisor_config

    with pytest.raises(SupervisorConfigError):
        load_supervisor_config(_write(tmp_path, _declaration(profile)))


@pytest.mark.parametrize("field", ["id", "profile"])
def test_worker_ids_and_profiles_must_be_unique(tmp_path, field):
    from career_lens.fleet.config import SupervisorConfigError, load_supervisor_config

    value = _declaration()
    second = {"id": "research-b", "profile": "research-b"}
    second[field] = value["workers"][0][field]
    value["workers"].append(second)
    with pytest.raises(SupervisorConfigError, match="duplicate"):
        load_supervisor_config(_write(tmp_path, value))


def test_unknown_keys_are_rejected(tmp_path):
    from career_lens.fleet.config import SupervisorConfigError, load_supervisor_config

    value = _declaration()
    value["capacity"] = 10
    with pytest.raises(SupervisorConfigError, match="unknown"):
        load_supervisor_config(_write(tmp_path, value))


def test_config_path_prefers_explicit_environment(monkeypatch, tmp_path):
    from career_lens.fleet.config import supervisor_config_path

    path = tmp_path / "supervisor.json"
    monkeypatch.setenv("CAREER_LENS_SUPERVISOR_CONFIG", str(path))
    assert supervisor_config_path() == path


"""The rename is a hard break, and these are the three ways to walk into it.

A file that still says `workers` cannot be loaded silently: the unknown-key
check would refuse it anyway, and "unknown supervisor keys: workers" does not
tell an operator that the key was renamed rather than removed.
"""


def test_the_former_key_is_refused_by_name(tmp_path):
    from career_lens.fleet.config import SupervisorConfigError, load_supervisor_config

    value = _declaration()
    value["slots"] = value.pop("workers")
    with pytest.raises(SupervisorConfigError, match="rename that array"):
        load_supervisor_config(_write(tmp_path, value))


def test_the_former_filename_is_refused_by_name(monkeypatch, tmp_path):
    from career_lens.fleet.config import SupervisorConfigError, supervisor_config_path

    monkeypatch.delenv("CAREER_LENS_SUPERVISOR_CONFIG", raising=False)
    monkeypatch.delenv("CAREER_LENS_WORKER_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "career_lens").mkdir()
    (tmp_path / "career_lens" / "worker.json").write_text("{}")

    with pytest.raises(SupervisorConfigError, match="rename it to"):
        supervisor_config_path()


def test_the_new_filename_wins_when_both_exist(monkeypatch, tmp_path):
    """No error, and no chance of reading the stale one."""
    from career_lens.fleet.config import supervisor_config_path

    monkeypatch.delenv("CAREER_LENS_SUPERVISOR_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    directory = tmp_path / "career_lens"
    directory.mkdir()
    (directory / "worker.json").write_text("{}")
    (directory / "supervisor.json").write_text("{}")

    assert supervisor_config_path() == directory / "supervisor.json"


def test_the_former_environment_variable_is_refused_by_name(monkeypatch):
    from career_lens.fleet.config import SupervisorConfigError, supervisor_config_path

    monkeypatch.delenv("CAREER_LENS_SUPERVISOR_CONFIG", raising=False)
    monkeypatch.setenv("CAREER_LENS_WORKER_CONFIG", "/anywhere/worker.json")

    with pytest.raises(SupervisorConfigError, match="CAREER_LENS_SUPERVISOR_CONFIG"):
        supervisor_config_path()
