"""Authentication lifecycle is revocable and stores no plaintext secrets."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.auth.service import (
    authenticate_api_key,
    create_api_key,
    list_api_keys,
    revoke_api_key,
)


NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def test_api_key_plaintext_is_returned_once_and_not_stored(app):
    issued = create_api_key(app, "  SEC production  ", NOW)

    stored = app.execute(text("""
        SELECT name, prefix, secret_digest FROM api_key WHERE id=:id
    """), {"id": issued.id}).one()
    listed = list_api_keys(app)

    assert issued.name == "SEC production"
    assert issued.token.startswith(f"cl_v1.{issued.id}.")
    assert stored.name == "SEC production"
    assert stored.prefix == issued.prefix
    assert issued.token not in tuple(stored)
    assert len(stored.secret_digest) == 64
    assert listed == [issued.record]
    assert not hasattr(listed[0], "token")


@pytest.mark.parametrize("name", ["", "   "])
def test_api_key_name_must_not_be_blank(app, name):
    with pytest.raises(ValueError, match="name"):
        create_api_key(app, name, NOW)


def test_api_key_name_has_a_bounded_length(app):
    with pytest.raises(ValueError, match="name"):
        create_api_key(app, "x" * 121, NOW)


def test_api_key_revocation_takes_effect_on_the_next_lookup(app):
    issued = create_api_key(app, "customer", NOW)

    assert authenticate_api_key(app, issued.token) == issued.record
    revoked = revoke_api_key(app, issued.id, NOW + timedelta(seconds=1))

    assert revoked is not None
    assert revoked.revoked_at == NOW + timedelta(seconds=1)
    assert authenticate_api_key(app, issued.token) is None


def test_api_key_authentication_rejects_malformed_and_wrong_secrets(app):
    issued = create_api_key(app, "customer", NOW)
    prefix, key_id, _ = issued.token.split(".")

    assert authenticate_api_key(app, "not-a-key") is None
    assert authenticate_api_key(app, f"{prefix}.{key_id}.{'x' * 43}") is None


def test_revoke_api_key_is_idempotent_and_unknown_keys_are_absent(app):
    issued = create_api_key(app, "customer", NOW)
    first = revoke_api_key(app, issued.id, NOW + timedelta(seconds=1))
    second = revoke_api_key(app, issued.id, NOW + timedelta(seconds=2))

    assert first == second
    assert revoke_api_key(app, "ak-unknown", NOW) is None


