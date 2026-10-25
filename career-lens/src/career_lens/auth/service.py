"""Database-backed API keys."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..store.ids import API_KEY, new_id
from .tokens import (
    digest,
    issue_api_key,
    matches,
    parse_api_key,
)


MAX_API_KEY_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    id: str
    name: str
    prefix: str
    created_at: datetime
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    id: str
    name: str
    prefix: str
    created_at: datetime
    revoked_at: datetime | None
    token: str

    @property
    def record(self) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=self.id,
            name=self.name,
            prefix=self.prefix,
            created_at=self.created_at,
            revoked_at=self.revoked_at,
        )


def _api_key(row) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
    )


def create_api_key(
    connection: Connection, name: str, now: datetime,
) -> IssuedApiKey:
    normalized_name = name.strip()
    if not normalized_name or len(normalized_name) > MAX_API_KEY_NAME_LENGTH:
        raise ValueError(
            f"API key name must contain 1-{MAX_API_KEY_NAME_LENGTH} characters",
        )
    key_id = new_id(API_KEY)
    token = issue_api_key(key_id)
    _, secret = parse_api_key(token) or (None, None)
    assert secret is not None
    prefix = f"cl_v1.{key_id}"
    row = connection.execute(text("""
        INSERT INTO api_key (
            id, name, prefix, secret_digest, created_at, revoked_at
        ) VALUES (:id, :name, :prefix, :secret_digest, :created_at, NULL)
        RETURNING id, name, prefix, created_at, revoked_at
    """), {
        "id": key_id,
        "name": normalized_name,
        "prefix": prefix,
        "secret_digest": digest(secret),
        "created_at": now,
    }).one()
    record = _api_key(row)
    return IssuedApiKey(
        id=record.id,
        name=record.name,
        prefix=record.prefix,
        created_at=record.created_at,
        revoked_at=record.revoked_at,
        token=token,
    )


def list_api_keys(connection: Connection) -> list[ApiKeyRecord]:
    rows = connection.execute(text("""
        SELECT id, name, prefix, created_at, revoked_at
        FROM api_key
        ORDER BY created_at DESC, id DESC
    """)).all()
    return [_api_key(row) for row in rows]


def authenticate_api_key(
    connection: Connection, token: str,
) -> ApiKeyRecord | None:
    parsed = parse_api_key(token)
    if parsed is None:
        return None
    key_id, secret = parsed
    row = connection.execute(text("""
        SELECT id, name, prefix, secret_digest, created_at, revoked_at
        FROM api_key
        WHERE id=:id
    """), {"id": key_id}).one_or_none()
    if (row is None or row.revoked_at is not None
            or not matches(secret, row.secret_digest)):
        return None
    return _api_key(row)


def revoke_api_key(
    connection: Connection, key_id: str, now: datetime,
) -> ApiKeyRecord | None:
    row = connection.execute(text("""
        UPDATE api_key
        SET revoked_at=COALESCE(revoked_at, :now)
        WHERE id=:id
        RETURNING id, name, prefix, created_at, revoked_at
    """), {"id": key_id, "now": now}).one_or_none()
    return _api_key(row) if row is not None else None
