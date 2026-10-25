"""LinkedIn passwords are encrypted, account-bound, and write-only."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from career_lens.store.config import ConfigError


@pytest.fixture
def keyring():
    from career_lens.fleet.credentials import CredentialKeyring

    return CredentialKeyring(active_version=2,
                             keys={1: b"a" * 32, 2: b"b" * 32})


def test_ciphertext_round_trips_and_is_bound_to_account(keyring):
    from career_lens.fleet.credentials import decrypt_password, encrypt_password

    envelope = encrypt_password("la-one", "sëcret", keyring=keyring)
    assert envelope.ciphertext != b"s\xc3\xabcret"
    assert len(envelope.nonce) == 12
    assert envelope.key_version == 2
    assert decrypt_password("la-one", envelope, keyring=keyring) == "sëcret"
    with pytest.raises(InvalidTag):
        decrypt_password("la-two", envelope, keyring=keyring)


def test_linkedin_wrapper_keeps_the_existing_aad_bytes(keyring):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from career_lens.fleet.credentials import encrypt_password

    nonce = b"n" * 12
    envelope = encrypt_password(
        "la-one", "secret", keyring=keyring, nonce=nonce)

    assert AESGCM(keyring.keys[2]).decrypt(
        nonce, envelope.ciphertext,
        b"career_lens.linkedin-account:la-one:v2") == b"secret"


def test_credential_namespaces_cannot_decrypt_each_other(keyring):
    from career_lens.fleet.credentials import (
        decrypt_credential, encrypt_credential,
    )

    envelope = encrypt_credential(
        "other-account", "ca-one", "secret", keyring=keyring)

    with pytest.raises(InvalidTag):
        decrypt_credential(
            "linkedin-account", "ca-one", envelope, keyring=keyring)


@pytest.mark.parametrize("namespace", ["", "crünchbase-account"])
def test_credential_namespace_must_be_nonempty_ascii(keyring, namespace):
    from career_lens.fleet.credentials import CredentialError, encrypt_credential

    with pytest.raises(CredentialError, match="namespace"):
        encrypt_credential(namespace, "ca-one", "secret", keyring=keyring)


def test_an_old_envelope_uses_its_own_key_version(keyring):
    from career_lens.fleet.credentials import decrypt_password, encrypt_password

    old = type(keyring)(active_version=1, keys=keyring.keys)
    envelope = encrypt_password("la-one", "secret", keyring=old)
    assert envelope.key_version == 1
    assert decrypt_password("la-one", envelope, keyring=keyring) == "secret"


def test_keyring_configuration_is_strict_and_never_echoes_values(monkeypatch):
    from career_lens.fleet.credentials import credential_keyring

    secret = "definitely-not-base64"
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", secret)
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", "1")
    with pytest.raises(ConfigError) as caught:
        credential_keyring()
    assert secret not in str(caught.value)
    assert "CAREER_LENS_CREDENTIAL_KEYS" in str(caught.value)


def test_keyring_reads_versioned_base64_keys(monkeypatch):
    from career_lens.fleet.credentials import credential_keyring

    encoded = base64.urlsafe_b64encode(b"k" * 32).decode()
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_KEYS", json.dumps({"7": encoded}))
    monkeypatch.setenv("CAREER_LENS_CREDENTIAL_ACTIVE_KEY", "7")
    result = credential_keyring()
    assert result.active_version == 7
    assert result.keys == {7: b"k" * 32}


def test_account_password_is_encrypted_and_never_projected(
        app_url, keyring):
    from career_lens.fleet.accounts import account_projection, create_account
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        account_id = create_account(
            connection, label="Research A", username="A@Example.com",
            password="secret",
            profile_url="https://www.linkedin.com/in/research-a/",
            max_attempts_24h=20, min_interval_seconds=300, keyring=keyring)

        stored = connection.execute(text("""
            SELECT a.username, c.ciphertext, c.nonce, c.key_version
            FROM linkedin_account a
            JOIN linkedin_account_credential c ON c.account_id = a.id
            WHERE a.id = :id
        """), {"id": account_id}).one()
        projected = account_projection(connection, account_id)
    assert stored.username == "a@example.com"
    assert b"secret" not in bytes(stored.ciphertext)
    assert stored.key_version == 2
    assert projected == {
        "id": account_id,
        "label": "Research A",
        "username": "a@example.com",
        "profile_url": "https://www.linkedin.com/in/research-a/",
        "state": "enabled",
        "max_attempts_24h": 20,
        "min_interval_seconds": 300,
        "cooldown_until": None,
        "recovery_phase": None,
        "recovery_not_before": None,
        "recovery_failures": 0,
        "recovery_login_retry_seconds": 1800,
        "recovery_post_login_seconds": 900,
        "attention_detail": None,
        "operator_attention": None,
        "credential_configured": True,
    }


def test_replacing_a_password_changes_the_envelope(app_url, keyring):
    from career_lens.fleet.accounts import (
        create_account, password_for_login, replace_password,
    )
    from career_lens.store.db import transaction

    with transaction(app_url) as connection:
        account_id = create_account(
            connection, label="Research A", username="a@example.com",
            password="old", max_attempts_24h=20, min_interval_seconds=0,
            keyring=keyring)
        before = connection.execute(text(
            "SELECT ciphertext FROM linkedin_account_credential "
            "WHERE account_id=:id"), {"id": account_id}).scalar_one()
        replace_password(connection, account_id, "new", keyring=keyring)
        after = connection.execute(text(
            "SELECT ciphertext FROM linkedin_account_credential "
            "WHERE account_id=:id"), {"id": account_id}).scalar_one()

        assert before != after
        assert password_for_login(
            connection, account_id, keyring=keyring) == "new"


def test_normalized_usernames_are_unique(app_url, keyring):
    from career_lens.fleet.accounts import create_account
    from career_lens.store.db import transaction

    values = dict(label="Research", password="secret", max_attempts_24h=20,
                  min_interval_seconds=0, keyring=keyring)
    with pytest.raises(IntegrityError):
        with transaction(app_url) as connection:
            create_account(connection, username="A@Example.com", **values)
            create_account(connection, username=" a@example.COM ", **values)


def test_account_write_rejects_an_autocommit_connection(app, keyring):
    from career_lens.fleet.accounts import AccountError, create_account

    with pytest.raises(AccountError, match="real database transaction"):
        create_account(
            app, label="Research", username="a@example.com",
            password="secret", max_attempts_24h=20,
            min_interval_seconds=0, keyring=keyring)
