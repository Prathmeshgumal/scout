"""The baseline migration on an empty database, and the check that the schema has no drift."""

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql

from job_finder.config import ROOT


@pytest.fixture
def scratch_url(database_url):
    base, name = database_url.rsplit("/", 1)
    scratch = f"{name}_migrations"
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(scratch)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
    yield f"{base}/{scratch}"
    with psycopg.connect(f"{base}/postgres", autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch)))


def config(url: str) -> Config:
    out = Config(str(ROOT / "alembic.ini"))
    out.set_main_option("sqlalchemy.url", url.replace("postgresql://", "postgresql+psycopg://", 1))
    return out


def test_the_baseline_builds_the_schema_and_alembic_check_finds_no_change(scratch_url):
    cfg = config(scratch_url)
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises when the database and the metadata differ
    with psycopg.connect(scratch_url) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()}
    assert tables == {"alembic_version", "application_notes", "board_order", "companies", "company_job_reads", "company_research",
                      "files", "goal_events", "goals", "jobs", "mandates", "pipeline_jobs", "pipeline_notes", "rejected_jobs", "report_notes", "scrapes", "eval_runs",
                      "subagent_tasks"}


def test_the_baseline_goes_down_to_an_empty_database(scratch_url):
    cfg = config(scratch_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    with psycopg.connect(scratch_url) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    assert [row[0] for row in rows] == ["alembic_version"]
