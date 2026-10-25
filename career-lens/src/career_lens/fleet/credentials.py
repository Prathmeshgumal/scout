"""Application-level encryption for provider-namespaced credentials."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..store.config import ConfigError

KEYS_VARIABLE = "CAREER_LENS_CREDENTIAL_KEYS"
ACTIVE_VARIABLE = "CAREER_LENS_CREDENTIAL_ACTIVE_KEY"


class CredentialError(RuntimeError):
    """A credential cannot be encrypted or decrypted safely."""


@dataclass(frozen=True)
class CredentialKeyring:
    active_version: int
    keys: Mapping[int, bytes]


@dataclass(frozen=True)
class CredentialEnvelope:
    ciphertext: bytes
    nonce: bytes
    key_version: int


def credential_keyring() -> CredentialKeyring:
    """Read a versioned AES keyring without echoing any secret on failure."""
    raw = os.environ.get(KEYS_VARIABLE)
    active = os.environ.get(ACTIVE_VARIABLE)
    if not raw:
        raise ConfigError(f"{KEYS_VARIABLE} is required")
    if not active:
        raise ConfigError(f"{ACTIVE_VARIABLE} is required")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise ConfigError(f"{KEYS_VARIABLE} must be a JSON object") from None
    if not isinstance(parsed, dict) or not parsed:
        raise ConfigError(f"{KEYS_VARIABLE} must be a non-empty JSON object")
    try:
        active_version = int(active)
    except (TypeError, ValueError):
        raise ConfigError(f"{ACTIVE_VARIABLE} must be a positive integer") from None
    if active_version <= 0:
        raise ConfigError(f"{ACTIVE_VARIABLE} must be a positive integer")

    keys: dict[int, bytes] = {}
    for version_text, encoded in parsed.items():
        try:
            version = int(version_text)
            if version <= 0 or not isinstance(encoded, str):
                raise ValueError
            key = base64.b64decode(encoded, altchars=b"-_", validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ConfigError(
                f"{KEYS_VARIABLE} must map positive versions to base64 keys") from None
        if len(key) != 32:
            raise ConfigError(f"{KEYS_VARIABLE} keys must decode to 32 bytes")
        keys[version] = key
    if active_version not in keys:
        raise ConfigError(
            f"{ACTIVE_VARIABLE} must name a version present in {KEYS_VARIABLE}")
    return CredentialKeyring(active_version=active_version, keys=keys)


def _aad(namespace: str, owner_id: str, version: int) -> bytes:
    if (not isinstance(namespace, str) or not namespace
            or not namespace.isascii()):
        raise CredentialError("credential namespace must be non-empty ASCII")
    return f"career_lens.{namespace}:{owner_id}:v{version}".encode()


def encrypt_credential(namespace: str, owner_id: str, secret: str, *,
                       keyring: CredentialKeyring | None = None,
                       nonce: bytes | None = None) -> CredentialEnvelope:
    if not isinstance(secret, str) or not secret:
        raise CredentialError("credential must be non-empty text")
    ring = keyring or credential_keyring()
    version = ring.active_version
    key = ring.keys.get(version)
    if key is None:
        raise CredentialError("active credential key is unavailable")
    nonce = nonce or os.urandom(12)
    if len(nonce) != 12:
        raise CredentialError("credential nonce must be 12 bytes")
    ciphertext = AESGCM(key).encrypt(
        nonce, secret.encode("utf-8"), _aad(namespace, owner_id, version))
    return CredentialEnvelope(ciphertext=ciphertext, nonce=nonce,
                              key_version=version)


def decrypt_credential(namespace: str, owner_id: str,
                       envelope: CredentialEnvelope, *,
                       keyring: CredentialKeyring | None = None) -> str:
    ring = keyring or credential_keyring()
    key = ring.keys.get(envelope.key_version)
    if key is None:
        raise CredentialError("credential key version is unavailable")
    plaintext = AESGCM(key).decrypt(
        envelope.nonce, envelope.ciphertext,
        _aad(namespace, owner_id, envelope.key_version))
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError:
        raise CredentialError("decrypted credential is not UTF-8") from None


def encrypt_password(account_id: str, password: str, *,
                     keyring: CredentialKeyring | None = None,
                     nonce: bytes | None = None) -> CredentialEnvelope:
    """Preserve LinkedIn's original envelope namespace and call signature."""
    if not isinstance(password, str) or not password:
        raise CredentialError("password must be non-empty text")
    return encrypt_credential(
        "linkedin-account", account_id, password,
        keyring=keyring, nonce=nonce)


def decrypt_password(account_id: str, envelope: CredentialEnvelope, *,
                     keyring: CredentialKeyring | None = None) -> str:
    """Decrypt envelopes written by every released LinkedIn implementation."""
    return decrypt_credential(
        "linkedin-account", account_id, envelope, keyring=keyring)
