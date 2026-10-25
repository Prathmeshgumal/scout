"""The schema declares what it is; this asserts the database agrees.

Every assertion here is an enumeration rather than a spot check. A spot check
passes when a table is added and forgotten; an enumeration fails.
"""

from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from career_lens.store.schema import APPEND_ONLY_TABLES, metadata
from career_lens.store.schema.common import APPEND_ONLY, MUTABLE, MUTABILITIES
from career_lens.store.schema.core import KINDS


def _qualified(table) -> str:
    return f"{table.schema}.{table.name}" if table.schema else table.name


def test_every_table_declared_its_mutability():
    for table in metadata.tables.values():
        assert table.info.get("mutability") in MUTABILITIES, (
            f"{_qualified(table)} did not declare mutability")


def test_authentication_state_is_mutable_without_storing_plaintext_secrets():
    api_key = metadata.tables["api_key"]

    assert api_key.info["mutability"] == MUTABLE
    assert {column.name for column in api_key.columns} == {
        "id", "name", "prefix", "secret_digest", "created_at", "revoked_at",
    }
    assert "secret" not in api_key.columns


def test_the_append_only_set_is_what_the_spec_says():
    # Spelled out rather than derived, so that changing a table's mutability
    # is a deliberate edit to this list and not a silent consequence.
    declared = {
        _qualified(t) for t in metadata.tables.values()
        if t.info.get("mutability") == APPEND_ONLY
    }
    assert declared == set(APPEND_ONLY_TABLES)


def test_the_database_matches_the_python_schema(migrator):
    context = MigrationContext.configure(
        migrator, opts={"compare_type": True, "include_schemas": True,
                        "include_name": lambda n, t, p:
                            t != "schema" or n in (
                                None, "public", "linkedin", "google",
                                "generic")})
    differences = compare_metadata(context, metadata)
    assert differences == [], f"migrations and metadata disagree: {differences}"


def test_request_kind_constraint_allows_the_declared_vocabulary(migrator):
    """The durable kind checks must match the request kinds callers submit."""
    definitions = migrator.execute(text("""
        SELECT c.conrelid::regclass::text, pg_get_constraintdef(c.oid)
        FROM pg_constraint c
        WHERE c.conname = 'ck_scrape_request_kind'
        ORDER BY c.conrelid::regclass::text
    """)).all()
    assert [table for table, _ in definitions] == ["scrape_request"]
    for _, definition in definitions:
        assert all(kind in definition for kind in KINDS)


def test_every_append_only_table_has_its_trigger_and_no_others_do(migrator):
    found = set(migrator.execute(text("""
        SELECT n.nspname || '.' || c.relname
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE t.tgname LIKE '%_append_only' AND NOT t.tgisinternal
    """)).scalars().all())
    expected = {name if "." in name else f"public.{name}"
                for name in APPEND_ONLY_TABLES}
    # Both directions. A table that lost its trigger and a table that grew one
    # it should not have are different bugs, and one-way containment hides the
    # second.
    assert found == expected


def test_the_app_role_holds_exactly_what_each_table_allows(migrator):
    rows = migrator.execute(text("""
        SELECT table_schema || '.' || table_name, privilege_type
        FROM information_schema.table_privileges
        WHERE grantee = 'career_lens_app'
    """)).all()
    held: dict[str, set[str]] = {}
    for name, privilege in rows:
        held.setdefault(name, set()).add(privilege)
    held.pop("public.alembic_version", None)

    for table in metadata.tables.values():
        name = _qualified(table)
        name = name if "." in name else f"public.{name}"
        append_only = table.info["mutability"] == APPEND_ONLY
        want = ({"SELECT", "INSERT"} if append_only
                else {"SELECT", "INSERT", "UPDATE", "DELETE"})
        assert held.get(name) == want, f"{name} holds {held.get(name)}, wants {want}"


def test_no_table_is_unreachable_by_the_application(migrator):
    reachable = set(migrator.execute(text("""
        SELECT table_schema || '.' || table_name
        FROM information_schema.table_privileges
        WHERE grantee = 'career_lens_app' AND privilege_type = 'SELECT'
    """)).scalars().all())
    for table in metadata.tables.values():
        name = _qualified(table)
        name = name if "." in name else f"public.{name}"
        assert name in reachable, f"{name} has no grants: protect() was not called"


def test_no_column_stores_a_naive_timestamp():
    for table in metadata.tables.values():
        for column in table.columns:
            if column.type.__class__.__name__ == "DateTime":
                assert column.type.timezone, (
                    f"{_qualified(table)}.{column.name} is naive")


def test_no_table_of_ours_landed_in_procrastinate_s_schema(migrator):
    """A migration must not create our tables in the queue's schema.

    0004 sets `search_path` to `procrastinate, public` to apply Procrastinate's
    own schema, and Alembic runs every migration in ONE transaction -- so a
    `SET LOCAL` that is not put back silently redirects every later migration
    that names a table without qualifying it. 0007 hit exactly this: both its
    tables were created in `procrastinate` and the run reported success.
    """
    ours = {t.name for t in metadata.tables.values() if t.schema in (None, "public")}
    strays = set(migrator.execute(text("""
        SELECT tablename FROM pg_tables WHERE schemaname = 'procrastinate'
    """)).scalars().all()) & ours

    assert strays == set(), f"created in the wrong schema: {sorted(strays)}"


def test_every_append_only_trigger_is_named_for_its_own_table(migrator):
    """`migrations/support.py` derives this name from the table, both when it
    installs the trigger and when `backfilling()` disables it.

    The test above asserts on the *table* a trigger sits on, so a rename that
    moved the table and left the trigger behind passes it -- and then breaks
    the next migration that backfills that table, which is a long way from
    here. Revision 0042 renamed six tables; this is what makes that safe.
    """
    mismatched = migrator.execute(text("""
        SELECT c.relname || ' carries ' || t.tgname
        FROM pg_trigger t
        JOIN pg_class c ON c.oid = t.tgrelid
        WHERE t.tgname LIKE '%_append_only' AND NOT t.tgisinternal
          AND t.tgname <> c.relname || '_append_only'
    """)).scalars().all()
    assert mismatched == []
