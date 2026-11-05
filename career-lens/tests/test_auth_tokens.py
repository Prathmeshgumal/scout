"""Opaque authentication tokens never put their secrets in durable state."""

from __future__ import annotations

import pytest

from career_lens.auth.tokens import (
    digest,
    issue_api_key,
    matches,
    parse_api_key,
)


def test_api_key_round_trips_as_a_public_id_and_random_secret():
    token = issue_api_key("ak-test")

    parsed = parse_api_key(token)

    assert parsed is not None
    key_id, secret = parsed
    assert key_id == "ak-test"
    assert token.startswith("cl_v1.ak-test.")
    assert len(secret) >= 43
    assert matches(secret, digest(secret))


def test_two_api_keys_for_the_same_id_have_different_secrets():
    first = issue_api_key("ak-test")
    second = issue_api_key("ak-test")

    assert first != second


@pytest.mark.parametrize("token", [
    "",
    "Bearer x",
    "cl_v1.missing",
    "cl_v1..secret",
    "cl_v1.not-an-api-key.secret",
    "cl_v1.ak-test.secret.extra",
])
def test_malformed_api_keys_are_rejected(token):
    assert parse_api_key(token) is None


def test_digest_matching_rejects_a_different_secret():
    expected = digest("right-secret")

    assert matches("wrong-secret", expected) is False
