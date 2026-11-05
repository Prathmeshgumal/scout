"""What migrations use to enforce D5.

This module lives with the migrations rather than in `store/` on purpose:
migrations call it, and a migration that changes meaning when application code
changes is not a migration. Nothing here may change what its SQL does. New
behaviour goes in a new function.
"""

from __future__ import annotations

from contextlib import contextmanager

from alembic import op

from career_lens.store.config import APP_ROLE

# A SQLSTATE of our own so a test can assert on the specific refusal rather
# than on message text. Classes beginning I-Z are reserved for
# implementation-defined conditions, so 'CL' will not collide with Postgres.
REFUSED = "CL001"

REJECT_FUNCTION = "career_lens_reject_mutation"


def create_reject_function() -> None:
    """Install the function every append-only trigger calls. Runs once, in 0001."""
    op.execute(f"""
        CREATE FUNCTION {REJECT_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION
                'career_lens: % on % is refused: this table is append-only',
                TG_OP, TG_TABLE_NAME
                USING ERRCODE = '{REFUSED}';
        END $$;
    """)


def protect(name: str, *, append_only: bool) -> None:
    """Grant the app role what this table's mutability allows, and no more.

    Called for every table, immediately after it is created. A table nobody
    calls this for is unreachable by the application at all, which the schema
    agreement test in tests/test_schema_agreement.py catches.

    The trigger is FOR EACH STATEMENT, so an UPDATE matching no rows is refused
    too. Intent is what is being rejected, not damage: code that means to
    update an append-only table is wrong whether or not it happens to find a
    row today.

    Honest about its limits: a superuser can set session_replication_role to
    'replica' and skip triggers, and the owner can drop them. This stops the
    application, which is the threat -- D5 says the same about RLS.
    """
    verbs = "SELECT, INSERT" if append_only else "SELECT, INSERT, UPDATE, DELETE"
    op.execute(f"GRANT {verbs} ON {name} TO {APP_ROLE}")
    if append_only:
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {name} FROM {APP_ROLE}")
        # The trigger's own name may not be schema-qualified -- Postgres accepts
        # a qualified name only after ON. Trigger names are scoped per table, so
        # dropping the schema here cannot collide with the same name on a table
        # in another schema.
        trigger = f"{name.rsplit('.', 1)[-1]}_append_only"
        op.execute(f"""
            CREATE TRIGGER {trigger}
            BEFORE UPDATE OR DELETE OR TRUNCATE ON {name}
            FOR EACH STATEMENT EXECUTE FUNCTION {REJECT_FUNCTION}()
        """)


@contextmanager
def backfilling(name: str):
    """Let a migration write a new column onto an append-only table.

    The trigger `protect` installs refuses UPDATE from everyone, the owner
    included -- which is right at runtime and wrong for the one operation that
    legitimately rewrites history in place: adding a column and giving the rows
    that already exist a value for it.

    Scoped to the statements inside the block and restored in a finally, so a
    backfill that raises cannot leave the table writable. This is a migration's
    privilege only: nothing in `career_lens/` may call it, and the app role
    could not use it anyway, since disabling a trigger requires owning the
    table.
    """
    trigger = f"{name.rsplit('.', 1)[-1]}_append_only"
    op.execute(f"ALTER TABLE {name} DISABLE TRIGGER {trigger}")
    try:
        yield
    finally:
        op.execute(f"ALTER TABLE {name} ENABLE TRIGGER {trigger}")
