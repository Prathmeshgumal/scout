import os
from pathlib import Path

from job_finder.config import ROOT, Settings


def test_defaults_when_env_is_empty():
    settings = Settings.from_env({})
    assert settings.model == "claude-sonnet-5-5"
    assert settings.effort == "medium"
    assert settings.database_url == "postgresql://jobfinder:jobfinder@127.0.0.1:55433/job_finder"
    assert settings.max_turns == 80
    assert settings.max_spend_usd == 4.0
    assert settings.max_fresh_scrapes == 30
    assert settings.mandate_total_usd == 20.0
    assert settings.mandate_time_limit_minutes == 240
    assert settings.mandate_grace_minutes == 60
    assert settings.claude_config_dir == ROOT / "var" / "claude"
    assert settings.agent_cwd == ROOT / "var" / "agent"
    assert settings.career_lens_base_url == ""
    assert settings.read_model == "claude-haiku-5-5"
    assert settings.career_lens_freshness == "7d"


def test_env_overrides_and_relative_paths_resolve_from_repo_root():
    settings = Settings.from_env({
        "JOB_FINDER_MODEL": "claude-opus-5-5",
        "CAREER_LENS_BASE_URL": "https://wh.example.com/",
        "CAREER_LENS_API_KEY": "cl_v1.ak-x.y",
        "CLAUDE_CONFIG_DIR": "data/claude",
        "JOB_FINDER_CWD": "/tmp/agent",
        "JOB_FINDER_MAX_SPEND_USD": "0.5",
        "JOB_FINDER_READ_MODEL": "claude-sonnet-5-5",
    })
    assert settings.model == "claude-opus-5-5"
    assert settings.career_lens_base_url == "https://wh.example.com"  # trailing slash dropped
    assert settings.career_lens_api_key == "cl_v1.ak-x.y"
    assert settings.claude_config_dir == ROOT / "data" / "claude"
    assert settings.agent_cwd == Path("/tmp/agent")
    assert settings.max_spend_usd == 0.5
    assert settings.read_model == "claude-sonnet-5-5"


def test_port_defaults_to_8000_and_can_be_overridden():
    assert Settings.from_env({}).port == 8000
    assert Settings.from_env({"JOB_FINDER_PORT": "8010"}).port == 8010


def test_host_defaults_to_loopback_and_can_be_opened_for_containers():
    assert Settings.from_env({}).host == "127.0.0.1"
    assert Settings.from_env({"JOB_FINDER_HOST": "0.0.0.0"}).host == "0.0.0.0"


def test_phase_2_limits_have_defaults_and_env_overrides():
    s = Settings.from_env({})
    assert (s.max_fresh_scrapes, s.max_wait_hours, s.watch_interval_s, s.scrape_wait_minutes) == (30, 72.0, 10.0, 1.0)
    s = Settings.from_env({"JOB_FINDER_MAX_FRESH_SCRAPES": "2", "JOB_FINDER_MAX_WAIT_HOURS": "0.5",
                           "JOB_FINDER_WATCH_INTERVAL_S": "5", "JOB_FINDER_SCRAPE_WAIT_MINUTES": "3"})
    assert (s.max_fresh_scrapes, s.max_wait_hours, s.watch_interval_s, s.scrape_wait_minutes) == (2, 0.5, 5.0, 3.0)


def test_subagent_settings():
    defaults = Settings.from_env({})
    assert (defaults.max_subagents, defaults.research_max_age_days) == (5, 30)
    custom = Settings.from_env({"JOB_FINDER_MAX_SUBAGENTS": "0", "JOB_FINDER_RESEARCH_MAX_AGE_DAYS": "7"})
    assert (custom.max_subagents, custom.research_max_age_days) == (1, 7)


def test_career_lens_freshness_can_be_set_and_is_checked():
    import pytest
    assert Settings.from_env({"CAREER_LENS_FRESHNESS": "24h"}).career_lens_freshness == "24h"
    for bad in ("7", "1w", "0d", "7 d"):
        with pytest.raises(ValueError, match="CAREER_LENS_FRESHNESS"):
            Settings.from_env({"CAREER_LENS_FRESHNESS": bad})


def test_web_tools_wait_longer_than_scrapes_and_can_be_changed():
    s = Settings.from_env({})
    assert (s.scrape_wait_minutes, s.web_wait_minutes) == (1.0, 5.0)
    assert Settings.from_env({"JOB_FINDER_WEB_WAIT_MINUTES": "3"}).web_wait_minutes == 3.0


def test_auth_mode_prefers_the_api_key_then_the_subscription_token():
    assert Settings.from_env({"ANTHROPIC_API_KEY": "sk-ant-k"}).auth_mode == "api_key"
    assert Settings.from_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-t"}).auth_mode == "oauth_token"
    both = Settings.from_env({"ANTHROPIC_API_KEY": "sk-ant-k", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-t"})
    assert both.auth_mode == "api_key"
    assert Settings.from_env({"ANTHROPIC_API_KEY": " ", "CLAUDE_CODE_OAUTH_TOKEN": ""}).auth_mode == "none"
    assert both.auth_label == "API key"
    assert Settings.from_env({}).auth_label == "Claude not connected"


def test_claude_env_gives_one_credential_and_removes_both_from_the_process(monkeypatch):
    from job_finder.config import claude_env
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-shell")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-shell")
    token_only = Settings.from_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-t"})
    assert claude_env(token_only) == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-t"}
    assert "ANTHROPIC_API_KEY" not in os.environ and "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    both = Settings.from_env({"ANTHROPIC_API_KEY": "sk-ant-k", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-t"})
    assert claude_env(both) == {"ANTHROPIC_API_KEY": "sk-ant-k"}
    assert claude_env(Settings.from_env({})) == {}


def test_the_env_file_is_the_repo_file_or_the_file_that_the_container_names():
    from job_finder.config import ROOT, env_file
    assert env_file({}) == ROOT / ".env"
    assert env_file({"JOB_FINDER_ENV_FILE": "/config/.env"}) == Path("/config/.env")


def test_the_default_total_spend_of_a_mandate_can_be_changed():
    base = {"DATABASE_URL": "postgresql://x"}
    assert Settings.from_env(base).mandate_total_usd == 20.0
    assert Settings.from_env({**base, "JOB_FINDER_MANDATE_TOTAL_USD": "25"}).mandate_total_usd == 25.0


def test_failed_goals_in_a_row_before_a_mandate_pauses_can_be_changed():
    base = {"DATABASE_URL": "postgresql://x"}
    assert Settings.from_env(base).mandate_max_failures == 3
    assert Settings.from_env({**base, "JOB_FINDER_MANDATE_MAX_FAILURES": "0"}).mandate_max_failures == 0


def test_the_default_time_limit_of_a_mandate_goal_can_be_changed():
    base = {"DATABASE_URL": "postgresql://x"}
    s = Settings.from_env(base)
    assert (s.mandate_time_limit_minutes, s.mandate_grace_minutes) == (240, 60)
    s = Settings.from_env({**base, "JOB_FINDER_MANDATE_TIME_LIMIT_MINUTES": "90",
                           "JOB_FINDER_MANDATE_GRACE_MINUTES": "0"})
    assert (s.mandate_time_limit_minutes, s.mandate_grace_minutes) == (90, 0)
