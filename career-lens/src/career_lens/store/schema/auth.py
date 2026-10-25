"""Mutable authentication credentials, never their plaintext secrets."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, Column, Text

from .common import MUTABLE, TS, table, text_pk


api_key = table(
    "api_key",
    text_pk(),
    Column("name", Text, nullable=False),
    Column("prefix", Text, nullable=False),
    Column("secret_digest", Text, nullable=False, unique=True),
    Column("created_at", TS, nullable=False),
    Column("revoked_at", TS),
    CheckConstraint("name <> ''", name="name_nonblank"),
    CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at",
                    name="revoked_after_creation"),
    mutability=MUTABLE,
)


