"""Captured payloads, in object storage.

One backend. The tool this replaces had a spilling store that fell back to
local disk when MinIO was unreachable, and in a real run 39 of 202 stored keys
(19%) pointed at objects that did not exist, with the spill directory empty and
nothing detecting it. A store that silently changes where it writes is how that
happens, so there is no fallback here: an upload either succeeds or raises.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from .config import BlobConfig, blob_config

S3_CONNECT_TIMEOUT_S = 5
S3_READ_TIMEOUT_S = 60
S3_MAX_ATTEMPTS = 3


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def head(self, key: str) -> int | None: ...
    def url(self, key: str, expires: int = 3600) -> str: ...


class S3BlobStore:
    """MinIO speaks the S3 API, so this is one client with a different endpoint."""

    def __init__(self, config: BlobConfig, *, timeout_seconds: float | None = None):
        import boto3
        from botocore.config import Config as BotoConfig

        self.bucket = config.bucket
        settings = BotoConfig(
            connect_timeout=(S3_CONNECT_TIMEOUT_S if timeout_seconds is None
                             else timeout_seconds),
            read_timeout=(S3_READ_TIMEOUT_S if timeout_seconds is None
                          else timeout_seconds),
            retries={"max_attempts": S3_MAX_ATTEMPTS if timeout_seconds is None else 0,
                     "mode": "standard"},
            # Stated, not inherited. Against a custom endpoint boto3 will
            # happily sign SigV2, and the difference is load-bearing here: it
            # is SigV4 that puts the host inside the signature, which is the
            # entire reason a URL for the browser has to be SIGNED at the
            # public address rather than signed once and rewritten.
            signature_version="s3v4",
        )

        def client(endpoint: str):
            return boto3.client(
                "s3", endpoint_url=endpoint,
                aws_access_key_id=config.access_key,
                aws_secret_access_key=config.secret_key, config=settings)

        self._client = client(config.endpoint)
        # A separate client only when the reader's browser reaches the bucket
        # at a different address than we do. It never connects: signing is
        # local arithmetic. Signed at the right address rather than rewritten
        # afterwards, because under SigV4 the host is part of the signature.
        self._signer = (client(config.public_endpoint)
                        if config.public_endpoint != config.endpoint
                        else self._client)

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key: str) -> int | None:
        """The object's size, or None if it is not there.

        This is what verifies a write before the row naming it is inserted. It
        is a HEAD rather than a re-read on purpose: the failure it must catch
        is the object being absent, and a profile capture is ~30 MB, so
        re-downloading everything to prove it arrived would double the transfer
        to detect a case a HEAD already detects.
        """
        from botocore.exceptions import ClientError
        try:
            return int(self._client.head_object(
                Bucket=self.bucket, Key=key)["ContentLength"])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise

    def url(self, key: str, expires: int = 3600) -> str:
        """Presigned, so the API hands out a URL rather than proxying bytes."""
        return self._signer.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires)


def open_store(config: BlobConfig | None = None) -> S3BlobStore:
    return S3BlobStore(config or blob_config())
