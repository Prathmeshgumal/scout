import pytest
from sqlalchemy import make_url, text

from career_lens.store.db import DatabaseUnavailable, connect, transaction


def test_connect_refuses_a_database_at_the_wrong_revision(app_url, migrator):
    migrator.execute(text("UPDATE alembic_version SET version_num = 'nope'"))
    with pytest.raises(DatabaseUnavailable) as raised:
        connect(app_url)
    assert "nope" in str(raised.value)
    assert raised.value.retryable is False


def test_a_failure_message_never_carries_the_password():
    with pytest.raises(DatabaseUnavailable) as raised:
        connect("postgresql+psycopg://who:hunter2@127.0.0.1:1/none")
    assert "hunter2" not in str(raised.value)


def test_invalid_database_credentials_are_not_retryable(app_url):
    wrong_url = make_url(app_url).set(password="definitely-wrong").render_as_string(
        hide_password=False)

    with pytest.raises(DatabaseUnavailable) as raised:
        connect(wrong_url)

    assert raised.value.retryable is False


def test_transaction_rolls_back(migrate_url):
    # The property AUTOCOMMIT cannot provide, and the reason transaction()
    # exists: engine() commits each statement as it runs, so a ROLLBACK there
    # rolls nothing back.
    #
    # Connected as the owner rather than the app section. The only table in
    # existence at this point is alembic_version, and migration 0001 gives the
    # app section SELECT on it and nothing more -- deliberately, so the revision
    # gate can read it without anything being able to rewrite history. What is
    # under test here is transaction semantics, not privileges, so the section
    # that can legitimately write is the right one to test with.
    with pytest.raises(RuntimeError):
        with transaction(migrate_url) as connection:
            connection.execute(text(
                "INSERT INTO alembic_version VALUES ('scratch')"))
            raise RuntimeError("stop")
    with transaction(migrate_url) as connection:
        rows = connection.execute(text(
            "SELECT version_num FROM alembic_version")).scalars().all()
    assert "scratch" not in rows
