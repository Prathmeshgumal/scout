import os

import psycopg
import pytest
from psycopg import sql

from job_finder.artifact_store import ArtifactStore
from job_finder.config import Settings
from job_finder.db import migrate, open_pool
from job_finder.store import Store

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://jobfinder:jobfinder@127.0.0.1:55433/job_finder_test")


@pytest.fixture(scope="session")
def database_url() -> str:
    base, name = TEST_DATABASE_URL.rsplit("/", 1)
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    migrate(TEST_DATABASE_URL)
    return TEST_DATABASE_URL


@pytest.fixture
async def pool(database_url):
    pool = await open_pool(database_url)
    async with pool.connection() as conn:
        await conn.execute("TRUNCATE mandates, goals, goal_events, scrapes, companies, company_research, jobs, "
                           "application_notes, rejected_jobs, company_job_reads, subagent_tasks, files, pipeline_jobs, pipeline_notes, board_order CASCADE")
    yield pool
    await pool.close()


@pytest.fixture
def store(pool) -> Store:
    return Store(pool)


@pytest.fixture
def artifacts(pool) -> ArtifactStore:
    return ArtifactStore(pool)


from job_finder.events import Hub, Recorder  # noqa: E402


@pytest.fixture
def hub() -> Hub:
    return Hub()


@pytest.fixture
def recorder(store, hub) -> Recorder:
    return Recorder(store, hub)


@pytest.fixture
def app_settings(tmp_path, database_url) -> Settings:
    return Settings(database_url=database_url, career_lens_base_url="", career_lens_api_key="", anthropic_api_key="k",
                    model="claude-sonnet-5-5", effort="medium", claude_config_dir=tmp_path / "claude",
                    agent_cwd=tmp_path / "agent", max_turns=40, max_spend_usd=2.0, retry_delay_s=0)


@pytest.fixture
def make_run(store, pool):
    """A mandate run goal. Give the mandate of an earlier call to make another run of it."""
    from datetime import UTC, datetime, time

    from job_finder.mandate_store import MandateStore

    async def make(slot_hour=3, mandate=None, quota=2):
        ms = MandateStore(pool)
        if mandate is None:
            home = await store.create_goal("home")
            mandate = await ms.create(home["id"], {
                "title": "EU fintech jobs", "instruction": "fintech", "every_unit": "day", "every_count": 1, "jobs_per_run": quota,
                "window_days": [1, 2, 3, 4, 5], "window_start": time(9), "timezone": "UTC",
                "max_spend_per_run": 2, "max_scrapes_per_run": 15, "next_run_at": datetime.now(UTC)})
        run = await ms.create_run(mandate, datetime(2026, 10, 8, slot_hour, tzinfo=UTC), 2.0, 1)
        return mandate, run
    return make


@pytest.fixture(autouse=True)
def no_live_login_check(monkeypatch):
    """A test never asks Anthropic about a credential. A test of the check itself uses respx."""
    monkeypatch.setattr("job_finder.login.credential_works", lambda value: True)
