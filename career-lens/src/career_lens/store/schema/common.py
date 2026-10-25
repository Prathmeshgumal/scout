"""The pieces every table is built from.

The important one is `mutability`. Every table says, in its own definition,
whether it is append-only or mutable -- and a migration turns that word into
grants and triggers, while a test asserts the database agrees with it. A table
that forgets to say fails the test rather than quietly landing writable.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Column, DateTime, Identity as Serial, MetaData, Table, Text,
)

# Predictable constraint names, so that autogenerate produces stable diffs and
# a failing constraint can be found by grepping for the name Postgres printed.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING)

# Every instant in this database is stored with its offset. There are no naive
# timestamps: the time axis is the point of the system (D8).
TS = DateTime(timezone=True)

APPEND_ONLY = "append_only"
MUTABLE = "mutable"
MUTABILITIES = (APPEND_ONLY, MUTABLE)


def table(name: str, *arguments, mutability: str, **keywords) -> Table:
    """A table that has declared whether it can be changed after the fact.

    `mutability` is keyword-only and has no default, so the decision is made at
    the point of definition by someone who knows the answer, rather than
    inherited from whatever the last table happened to be.
    """
    if mutability not in MUTABILITIES:
        raise ValueError(f"{name}: mutability must be one of {MUTABILITIES}")
    return Table(name, metadata, *arguments,
                 info={"mutability": mutability}, **keywords)


def pk() -> Column:
    """A generated integer primary key.

    ALWAYS rather than BY DEFAULT: nothing supplies these, and an insert that
    did would desynchronise the sequence silently.
    """
    return Column("id", BigInteger, Serial(always=True), primary_key=True)


def text_pk() -> Column:
    """A prefixed text primary key, supplied by `store.ids.new_id`.

    Only for rows that are named outside this system -- see ids.py.
    """
    return Column("id", Text, primary_key=True)


def enum_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for a closed vocabulary, as a CHECK expression.

    A CHECK rather than a Postgres ENUM type: adding a value to an enum is a
    DDL operation that cannot run inside a transaction with other statements,
    and every vocabulary here is expected to grow.
    """
    listed = ", ".join("'{}'".format(v.replace("'", "''")) for v in values)
    return f"{column} IN ({listed})"
