"""The single baseline migration builds the schema the application needs."""

from __future__ import annotations

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from career_lens.store.db import MIGRATIONS_DIR, migrate


def revisions() -> list[str]:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return [script.revision for script in ScriptDirectory.from_config(config).walk_revisions()]


def schema_snapshot(url: str) -> list[tuple]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(
                "SELECT table_schema, table_name, column_name, data_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema IN ('public','linkedin','google','generic','procrastinate') "
                "ORDER BY 1, 2, 3"))]
    finally:
        engine.dispose()


def test_single_revision():
    assert revisions() == ["0001"]


def test_migrate_twice_changes_nothing(migrate_url):
    assert migrate(migrate_url) == "0001"
    before = schema_snapshot(migrate_url)
    assert before
    assert migrate(migrate_url) == "0001"
    assert schema_snapshot(migrate_url) == before


def test_request_kind_constraint_refuses_others(app):
    with pytest.raises(IntegrityError):
        app.execute(text(
            "INSERT INTO scrape_request "
            "(id, url_requested, url_normalised, kind, status, requested_at) "
            "VALUES ('r1', 'https://example.org', 'https://example.org', "
            "'not_a_kind', 'queued', now())"))


def test_app_role_has_no_update_right_on_append_only(app):
    with pytest.raises(DBAPIError) as refused:
        app.execute(text("UPDATE capture SET id = id"))
    assert refused.value.orig.sqlstate == "42501"


def test_owner_update_of_append_only_hits_the_trigger(migrator):
    with pytest.raises(DBAPIError) as refused:
        migrator.execute(text("UPDATE capture SET id = id"))
    assert refused.value.orig.sqlstate == "CL001"
