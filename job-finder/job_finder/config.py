"""Settings from the environment (.env is loaded by the CLI)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTH_VARS = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")
LOGIN_CHOICE = "JOB_FINDER_CLAUDE_LOGIN"  # the login that the user chose in the web app
NO_LOGIN = ("Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in .env, or run `job-finder login`, "
            "or use the Settings button of the web app.")
AUTH_LABELS = {"api_key": "API key", "oauth_token": "Claude subscription token", "none": "Claude not connected"}


def env_file(environ: Mapping[str, str] = os.environ) -> Path:
    """The .env file of the CLI. A test names its own file."""
    return Path(environ.get("JOB_FINDER_ENV_FILE") or ROOT / ".env")


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _freshness(value: str) -> str:
    """A career-lens freshness: a number and m, h or d, for example 90m, 24h or 7d."""
    if not re.fullmatch(r"[1-9][0-9]*[mhd]", value):
        raise ValueError(f"CAREER_LENS_FRESHNESS must be a number and m, h or d, for example 7d, not {value!r}")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str
    career_lens_base_url: str
    career_lens_api_key: str
    anthropic_api_key: str
    model: str
    effort: str
    claude_config_dir: Path
    agent_cwd: Path
    max_turns: int
    max_spend_usd: float
    mandate_total_usd: float = 20.0
    mandate_max_failures: int = 3
    mandate_time_limit_minutes: int = 240
    mandate_grace_minutes: int = 60
    host: str = "127.0.0.1"
    port: int = 8000
    retry_delay_s: float = 30.0
    watch_interval_s: float = 10.0
    max_fresh_scrapes: int = 30
    max_wait_hours: float = 72.0
    max_subagents: int = 5
    research_max_age_days: int = 30
    scrape_wait_minutes: float = 1.0
    web_wait_minutes: float = 5.0
    idle_minutes: float = 10.0
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket: str = "job-finder"
    read_model: str = "claude-haiku-5-5"
    read_effort: str = "medium"
    career_lens_freshness: str = "7d"
    claude_oauth_token: str = ""  # a Claude subscription token from `claude setup-token`
    claude_login: str = ""  # api_key or oauth_token: the login that the user chose. Empty: the API key wins.

    @property
    def auth_mode(self) -> str:
        """The Claude credential to use. The choice of the user wins. With no choice, the API key wins."""
        if self.claude_login == "oauth_token" and self.claude_oauth_token:
            return "oauth_token"
        if self.anthropic_api_key:
            return "api_key"
        return "oauth_token" if self.claude_oauth_token else "none"

    @property
    def credential(self) -> str:
        return {"api_key": self.anthropic_api_key, "oauth_token": self.claude_oauth_token}.get(self.auth_mode, "")

    @property
    def auth_label(self) -> str:
        return AUTH_LABELS[self.auth_mode]

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Settings:
        get = lambda key, default="": (env.get(key) or default).strip()  # noqa: E731
        return cls(
            database_url=get("DATABASE_URL", "postgresql://jobfinder:jobfinder@127.0.0.1:55433/job_finder"),
            career_lens_base_url=get("CAREER_LENS_BASE_URL").rstrip("/"),
            career_lens_api_key=get("CAREER_LENS_API_KEY"),
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            model=get("JOB_FINDER_MODEL", "claude-sonnet-5-5"),
            effort=get("JOB_FINDER_EFFORT", "medium"),
            claude_config_dir=_path(get("CLAUDE_CONFIG_DIR", "var/claude")),
            agent_cwd=_path(get("JOB_FINDER_CWD", "var/agent")),
            max_turns=int(get("JOB_FINDER_MAX_TURNS", "80")),
            max_spend_usd=float(get("JOB_FINDER_MAX_SPEND_USD", "4.00")),
            mandate_total_usd=float(get("JOB_FINDER_MANDATE_TOTAL_USD", "20.00")),
            mandate_max_failures=max(0, int(get("JOB_FINDER_MANDATE_MAX_FAILURES", "3"))),
            mandate_time_limit_minutes=max(5, int(get("JOB_FINDER_MANDATE_TIME_LIMIT_MINUTES", "240"))),
            mandate_grace_minutes=max(0, int(get("JOB_FINDER_MANDATE_GRACE_MINUTES", "60"))),
            host=get("JOB_FINDER_HOST", "127.0.0.1"),
            port=int(get("JOB_FINDER_PORT", "8000")),
            watch_interval_s=float(get("JOB_FINDER_WATCH_INTERVAL_S", "10")),
            max_fresh_scrapes=int(get("JOB_FINDER_MAX_FRESH_SCRAPES", "30")),
            max_wait_hours=float(get("JOB_FINDER_MAX_WAIT_HOURS", "72")),
            max_subagents=max(1, int(get("JOB_FINDER_MAX_SUBAGENTS", "5"))),
            research_max_age_days=int(get("JOB_FINDER_RESEARCH_MAX_AGE_DAYS", "30")),
            scrape_wait_minutes=float(get("JOB_FINDER_SCRAPE_WAIT_MINUTES", "1")),
            web_wait_minutes=float(get("JOB_FINDER_WEB_WAIT_MINUTES", "5")),
            idle_minutes=float(get("JOB_FINDER_IDLE_MINUTES", "10")),
            s3_endpoint=get("JOB_FINDER_S3_ENDPOINT"),
            s3_access_key=get("JOB_FINDER_S3_ACCESS_KEY"),
            s3_secret_key=get("JOB_FINDER_S3_SECRET_KEY"),
            s3_bucket=get("JOB_FINDER_S3_BUCKET", "job-finder"),
            read_model=get("JOB_FINDER_READ_MODEL", "claude-haiku-5-5"),
            read_effort=get("JOB_FINDER_READ_EFFORT", "medium"),
            career_lens_freshness=_freshness(get("CAREER_LENS_FRESHNESS", "7d")),
            claude_oauth_token=get("CLAUDE_CODE_OAUTH_TOKEN"),
            claude_login=get(LOGIN_CHOICE),
        )


def claude_env(settings: Settings) -> dict[str, str]:
    """The credential for Claude Code. The SDK adds os.environ to the env it gives, so remove both there first:
    a key that stays in os.environ can win over the token."""
    for name in AUTH_VARS:
        os.environ.pop(name, None)
    if settings.auth_mode == "api_key":
        return {"ANTHROPIC_API_KEY": settings.anthropic_api_key}
    if settings.auth_mode == "oauth_token":
        return {"CLAUDE_CODE_OAUTH_TOKEN": settings.claude_oauth_token}
    return {}
