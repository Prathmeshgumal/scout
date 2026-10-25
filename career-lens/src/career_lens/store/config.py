"""Where the database is, and who we connect as.

Two URLs, because D5 makes immutability a privilege question: migrations run
as an owner that can create and alter, everything else connects as a role that
can insert and read and nothing more. Keeping them apart in configuration is
what stops the application quietly acquiring the owner's rights.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

APP_ROLE = "career_lens_app"
MIGRATE_ROLE = "career_lens_migrate"

DEFAULT_APP_URL = (
    "postgresql+psycopg://career_lens_app:career-lens-dev-secret"
    "@127.0.0.1:5433/career_lens")
DEFAULT_MIGRATE_URL = (
    "postgresql+psycopg://career_lens_migrate:career-lens-dev-secret"
    "@127.0.0.1:5433/career_lens")


class ConfigError(RuntimeError):
    """The configuration cannot produce a usable connection."""


def _checked(url: str, variable: str) -> str:
    """Reject a URL that would silently select the wrong driver.

    Both `postgresql://` and the short `postgres://` pick psycopg2, which is
    not a dependency of this project. SQLAlchemy's own error for that names a
    module rather than the mistake, and the mistake is easy to make because
    every other tool accepts the short form -- `postgres://` is what hosted
    providers hand out, so it is the one most likely to arrive by copy-paste.

    Nothing after `://` is ever repeated back in an error: that is where
    credentials live, and a rejected URL is not a URL this function may echo.
    """
    if url.startswith(("postgresql://", "postgres://")):
        raise ConfigError(
            f"{variable} names a scheme that selects psycopg2 -- career_lens "
            f"uses psycopg 3. Write 'postgresql+psycopg://' instead.")
    if not url.startswith("postgresql+psycopg://"):
        found = f"{url.partition('://')[0]}://" if "://" in url else "no scheme at all"
        raise ConfigError(
            f"{variable} must start with 'postgresql+psycopg://', found {found}")
    return url


def database_url() -> str:
    """The application's URL: restricted, and what tests use."""
    return _checked(
        os.environ.get("CAREER_LENS_DATABASE_URL") or DEFAULT_APP_URL,
        "CAREER_LENS_DATABASE_URL")


def migrate_database_url() -> str:
    """The owner's URL. Only `career_lens migrate` and the test harness use it."""
    return _checked(
        os.environ.get("CAREER_LENS_MIGRATE_DATABASE_URL") or DEFAULT_MIGRATE_URL,
        "CAREER_LENS_MIGRATE_DATABASE_URL")


# --- object storage -----------------------------------------------------

DEFAULT_S3_ENDPOINT = "http://127.0.0.1:9002"
DEFAULT_S3_BUCKET = "career-lens"
ARTIFACT_URL_TTL_VARIABLE = "CAREER_LENS_ARTIFACT_URL_TTL_SECONDS"
DEFAULT_ARTIFACT_URL_TTL_SECONDS = 604_800


def artifact_url_ttl_seconds() -> int:
    """Lifetime for every API-issued object URL, bounded by SigV4's maximum."""
    raw = os.environ.get(ARTIFACT_URL_TTL_VARIABLE)
    if raw is None:
        return DEFAULT_ARTIFACT_URL_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(
            f"{ARTIFACT_URL_TTL_VARIABLE} must be an integer from 1 through "
            "604800") from None
    if not 1 <= value <= 604_800:
        raise ConfigError(
            f"{ARTIFACT_URL_TTL_VARIABLE} must be from 1 through 604800")
    return value


@dataclass(frozen=True)
class BlobConfig:
    endpoint: str
    public_endpoint: str
    access_key: str
    secret_key: str
    bucket: str


def blob_config() -> BlobConfig:
    """Where captured payloads go.

    `public_endpoint` exists because the API reaches MinIO by its service name
    inside the compose network, and the browser reading the response cannot
    resolve that name. A presigned URL is signed for the address the reader
    will use, rather than rewritten afterwards -- under SigV4 the host is part
    of what the signature covers, so rewriting invalidates it.

    The endpoint has a default rather than being required, but an *empty*
    endpoint is refused: boto3 reads that as "AWS", and a run that uploads
    captured pages to somewhere unintended is worse than one that will not
    start.
    """
    endpoint = os.environ.get("CAREER_LENS_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
    if not endpoint.strip():
        raise ConfigError(
            "CAREER_LENS_S3_ENDPOINT is empty, which boto3 reads as AWS. "
            "Set it to the MinIO address, or unset it for the default.")
    return BlobConfig(
        endpoint=endpoint,
        public_endpoint=os.environ.get("CAREER_LENS_S3_PUBLIC_ENDPOINT", endpoint),
        access_key=os.environ.get("CAREER_LENS_S3_ACCESS_KEY", "career_lens"),
        secret_key=os.environ.get("CAREER_LENS_S3_SECRET_KEY", "career-lens-dev-secret"),
        bucket=os.environ.get("CAREER_LENS_S3_BUCKET", DEFAULT_S3_BUCKET),
    )
