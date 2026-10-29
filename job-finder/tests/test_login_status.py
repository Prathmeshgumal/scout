import json
from dataclasses import replace

import pytest

from job_finder.config import Settings
from job_finder.login import save_credential


@pytest.fixture
def app_settings():
    return Settings.from_env({})


def test_account_details_follow_the_loaded_credential(app_settings, tmp_path):
    from job_finder.login_status import LoginStatus
    path = tmp_path / ".env"
    first, second = "sk-ant-oat01-first", "sk-ant-oat01-second"
    save_credential(path, first, account_label="First account")
    settings = replace(app_settings, anthropic_api_key="", claude_oauth_token=first)
    status = LoginStatus(settings, path)
    initial = status.snapshot()
    assert initial["account_label"] == "First account"
    assert initial["saved_at"] and initial["loaded_at"] and not initial["restart_required"]
    save_credential(path, second, account_label="Second account")
    pending = status.snapshot()
    assert pending["account_label"] == "First account" and pending["restart_required"]
    assert pending["loaded_at"] == initial["loaded_at"]
    new = LoginStatus(replace(settings, claude_oauth_token=second), path).snapshot()
    assert new["account_label"] == "Second account" and not new["restart_required"]
    assert first not in json.dumps(pending) and second not in json.dumps(pending)


def test_manual_token_changes_do_not_reuse_an_old_account_label(app_settings, tmp_path):
    from job_finder.login_status import LoginStatus
    path = tmp_path / ".env"
    save_credential(path, "sk-ant-oat01-first", account_label="First account")
    path.write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-manual\n")
    settings = replace(app_settings, anthropic_api_key="", claude_oauth_token="sk-ant-oat01-manual")
    details = LoginStatus(settings, path).snapshot()
    assert details["account_label"] is None and details["saved_at"] is None
    assert not details["restart_required"]


def test_api_key_precedence_and_unrelated_file_changes(app_settings, tmp_path):
    from job_finder.login_status import LoginStatus
    path = tmp_path / ".env"
    save_credential(path, "sk-ant-api03-key", account_label="API account")
    settings = replace(app_settings, anthropic_api_key="sk-ant-api03-key")
    status = LoginStatus(settings, path)
    save_credential(path, "sk-ant-oat01-token", account_label="Subscription account")
    with path.open("a") as out:
        out.write("JOB_FINDER_MAX_TURNS=80\n")
    details = status.snapshot()
    assert details["account_label"] == "API account" and not details["restart_required"]


def test_a_new_login_without_a_label_clears_the_old_label(app_settings, tmp_path):
    import stat

    from job_finder.login_status import LoginStatus
    path = tmp_path / ".env"
    save_credential(path, "sk-ant-oat01-first", account_label="First account")
    save_credential(path, "sk-ant-oat01-second")
    settings = replace(app_settings, claude_oauth_token="sk-ant-oat01-second")
    assert LoginStatus(settings, path).snapshot()["account_label"] is None
    metadata = path.with_name(".env.login.json")
    assert stat.S_IMODE(metadata.stat().st_mode) == 0o600
    assert "sk-ant-oat01-" not in metadata.read_text()
