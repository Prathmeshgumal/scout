"""Persistence for LinkedIn accounts without plaintext-bearing projections."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from sqlalchemy import Connection, text

from ..capture.urls import canonical, classify
from ..store.ids import LINKEDIN_ACCOUNT, new_id
from .credentials import (
    CredentialEnvelope, CredentialKeyring, decrypt_password, encrypt_password,
)
from .attention import guidance


class AccountError(ValueError):
    """An account command is invalid or names no account."""


def _require_transaction(connection: Connection) -> None:
    isolation = connection.get_execution_options().get("isolation_level")
    if isolation == "AUTOCOMMIT" or not connection.in_transaction():
        raise AccountError("account writes require a real database transaction")


def _clean(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise AccountError(f"{name} must be text")
    cleaned = " ".join(value.split())
    if not cleaned:
        raise AccountError(f"{name} must not be empty")
    return cleaned


def profile_url_for(value: str | None) -> str | None:
    """Canonical LinkedIn person URL, or None. Raises on anything else."""
    if value is None:
        return None
    result = canonical(value)
    if classify(result) != "linkedin_person":
        raise AccountError("profile_url must be a LinkedIn individual URL")
    return result


def profile_identity_for(value: str) -> str:
    """Stable, host-independent identity for a LinkedIn individual URL."""
    profile_url = profile_url_for(value)
    if profile_url is None:  # Kept explicit for the nullable URL helper above.
        raise AccountError("profile_url must be a LinkedIn individual URL")
    handle = unquote(urlsplit(profile_url).path).strip("/").split("/", 1)[1]
    return f"/in/{handle.casefold()}"


def create_account(connection: Connection, *, label: str, username: str,
                   password: str, max_attempts_24h: int,
                   min_interval_seconds: int,
                   recovery_login_retry_seconds: int = 1800,
                   recovery_post_login_seconds: int = 900,
                   profile_url: str | None = None,
                   keyring: CredentialKeyring | None = None,
                   now: datetime | None = None) -> str:
    """Insert an account and its secret using the caller's transaction.

    The connection must belong to a real transaction (``store.db.transaction``
    in application code), because the two rows are one logical write.
    """
    _require_transaction(connection)
    if not isinstance(max_attempts_24h, int) or max_attempts_24h <= 0:
        raise AccountError("max_attempts_24h must be a positive integer")
    if not isinstance(min_interval_seconds, int) or min_interval_seconds < 0:
        raise AccountError("min_interval_seconds must be a non-negative integer")
    for name, value in (
        ("recovery_login_retry_seconds", recovery_login_retry_seconds),
        ("recovery_post_login_seconds", recovery_post_login_seconds),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AccountError(f"{name} must be a non-negative integer")
    account_id = new_id(LINKEDIN_ACCOUNT)
    created_at = now or datetime.now(UTC)
    envelope = encrypt_password(account_id, password, keyring=keyring)
    normalised_profile_url = profile_url_for(profile_url)
    profile_identity = (
        profile_identity_for(normalised_profile_url)
        if normalised_profile_url is not None else None
    )
    connection.execute(text("""
        INSERT INTO linkedin_account
            (id, label, username, profile_url, profile_identity, state,
             max_attempts_24h,
             min_interval_seconds, recovery_login_retry_seconds,
             recovery_post_login_seconds, created_at, updated_at)
        VALUES (:id, :label, :username, :profile_url, :profile_identity,
                'enabled', :maximum, :interval, :recovery_login_retry,
                :recovery_post_login, :now, :now)
    """), {
        "id": account_id, "label": _clean(label, "label"),
        "username": _clean(username, "username").casefold(),
        "profile_url": normalised_profile_url,
        "profile_identity": profile_identity,
        "maximum": max_attempts_24h,
        "interval": min_interval_seconds,
        "recovery_login_retry": recovery_login_retry_seconds,
        "recovery_post_login": recovery_post_login_seconds,
        "now": created_at,
    })
    connection.execute(text("""
        INSERT INTO linkedin_account_credential
            (account_id, ciphertext, nonce, key_version, updated_at)
        VALUES (:id, :ciphertext, :nonce, :version, :now)
    """), {
        "id": account_id, "ciphertext": envelope.ciphertext,
        "nonce": envelope.nonce, "version": envelope.key_version,
        "now": created_at,
    })
    return account_id


def replace_password(connection: Connection, account_id: str, password: str, *,
                     keyring: CredentialKeyring | None = None,
                     now: datetime | None = None) -> None:
    _require_transaction(connection)
    changed_at = now or datetime.now(UTC)
    envelope = encrypt_password(account_id, password, keyring=keyring)
    result = connection.execute(text("""
        UPDATE linkedin_account_credential
        SET ciphertext = :ciphertext, nonce = :nonce, key_version = :version,
            updated_at = :now
        WHERE account_id = :id
    """), {
        "id": account_id, "ciphertext": envelope.ciphertext,
        "nonce": envelope.nonce, "version": envelope.key_version,
        "now": changed_at,
    })
    if result.rowcount != 1:
        raise AccountError(f"no LinkedIn account {account_id}")
    connection.execute(text(
        "UPDATE linkedin_account SET updated_at = :now WHERE id = :id"),
        {"id": account_id, "now": changed_at})


def update_limits(connection: Connection, account_id: str, *,
                  max_attempts_24h: int, min_interval_seconds: int,
                  now: datetime | None = None) -> None:
    """Change an account's limits. The next dispatch reads the new values."""
    _require_transaction(connection)
    result = connection.execute(text("""
        UPDATE linkedin_account
        SET max_attempts_24h = :maximum, min_interval_seconds = :interval,
            updated_at = :now
        WHERE id = :id
    """), {"id": account_id, "maximum": max_attempts_24h,
           "interval": min_interval_seconds, "now": now or datetime.now(UTC)})
    if result.rowcount != 1:
        raise AccountError(f"no LinkedIn account {account_id}")


def password_for_login(connection: Connection, account_id: str, *,
                       keyring: CredentialKeyring | None = None) -> str:
    row = connection.execute(text("""
        SELECT ciphertext, nonce, key_version
        FROM linkedin_account_credential WHERE account_id = :id
    """), {"id": account_id}).one_or_none()
    if row is None:
        raise AccountError(f"no credential for LinkedIn account {account_id}")
    return decrypt_password(
        account_id,
        CredentialEnvelope(ciphertext=bytes(row.ciphertext),
                           nonce=bytes(row.nonce), key_version=row.key_version),
        keyring=keyring,
    )


def account_projection(connection: Connection,
                       account_id: str) -> dict | None:
    row = connection.execute(text("""
        SELECT a.id, a.label, a.username, a.profile_url, a.state,
               a.max_attempts_24h, a.min_interval_seconds, a.cooldown_until,
               a.recovery_phase, a.recovery_not_before,
               a.recovery_failures, a.recovery_login_retry_seconds,
               a.recovery_post_login_seconds,
               a.attention_detail, a.operator_attention_code,
               (c.account_id IS NOT NULL) AS configured
        FROM linkedin_account a
        LEFT JOIN linkedin_account_credential c ON c.account_id = a.id
        WHERE a.id = :id
    """), {"id": account_id}).one_or_none()
    if row is None:
        return None
    return {
        "id": row.id, "label": row.label, "username": row.username,
        "profile_url": row.profile_url, "state": row.state,
        "max_attempts_24h": row.max_attempts_24h,
        "min_interval_seconds": row.min_interval_seconds,
        "cooldown_until": (row.cooldown_until.isoformat()
                           if row.cooldown_until else None),
        "recovery_phase": row.recovery_phase,
        "recovery_not_before": (
            row.recovery_not_before.isoformat()
            if row.recovery_not_before else None
        ),
        "recovery_failures": row.recovery_failures,
        "recovery_login_retry_seconds": row.recovery_login_retry_seconds,
        "recovery_post_login_seconds": row.recovery_post_login_seconds,
        "attention_detail": row.attention_detail,
        "operator_attention": (
            guidance(row.operator_attention_code).projection()
            if row.operator_attention_code else None),
        "credential_configured": row.configured,
    }
