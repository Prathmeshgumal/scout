import pytest

from career_lens.store.config import (
    ConfigError,
    artifact_url_ttl_seconds,
    blob_config,
    database_url,
)


def test_psycopg2_scheme_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", "postgresql://a:b@h/d")
    with pytest.raises(ConfigError) as raised:
        database_url()
    assert "psycopg2" in str(raised.value)


def test_short_postgres_scheme_is_refused(monkeypatch):
    # The form hosted providers hand out, so the one most likely to arrive by
    # copy-paste.
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", "postgres://a:b@h/d")
    with pytest.raises(ConfigError):
        database_url()


def test_a_rejected_url_never_appears_in_the_error(monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", "mysql://user:hunter2@h/d")
    with pytest.raises(ConfigError) as raised:
        database_url()
    assert "hunter2" not in str(raised.value)
    assert "user" not in str(raised.value)


def test_empty_s3_endpoint_is_refused_because_boto_reads_it_as_aws(monkeypatch):
    monkeypatch.setenv("CAREER_LENS_S3_ENDPOINT", "   ")
    with pytest.raises(ConfigError) as raised:
        blob_config()
    assert "AWS" in str(raised.value)


def test_public_endpoint_defaults_to_the_private_one(monkeypatch):
    monkeypatch.setenv("CAREER_LENS_S3_ENDPOINT", "http://minio:9000")
    monkeypatch.delenv("CAREER_LENS_S3_PUBLIC_ENDPOINT", raising=False)
    assert blob_config().public_endpoint == "http://minio:9000"


def test_the_attempt_ceiling_defaults_to_three(monkeypatch):
    from career_lens.capture.options import default_max_attempts

    monkeypatch.delenv("CAREER_LENS_MAX_ATTEMPTS", raising=False)

    assert default_max_attempts() == 3


def test_the_deployment_may_choose_a_different_ceiling(monkeypatch):
    from career_lens.capture.options import default_max_attempts

    monkeypatch.setenv("CAREER_LENS_MAX_ATTEMPTS", "5")

    assert default_max_attempts() == 5


def test_an_unreadable_ceiling_falls_back_rather_than_refusing_work(monkeypatch):
    """A typo in the deployment's environment is not the caller's fault, and
    refusing every submission over it would be a worse failure than the
    default."""
    from career_lens.capture.options import default_max_attempts

    for bad in ("", "lots", "0", "-2"):
        monkeypatch.setenv("CAREER_LENS_MAX_ATTEMPTS", bad)
        assert default_max_attempts() == 3, bad


@pytest.mark.parametrize("raw,want", [
    (None, 604800),
    ("1", 1),
    ("604800", 604800),
])
def test_artifact_url_ttl(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("CAREER_LENS_ARTIFACT_URL_TTL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("CAREER_LENS_ARTIFACT_URL_TTL_SECONDS", raw)
    assert artifact_url_ttl_seconds() == want


@pytest.mark.parametrize("raw", ["", "0", "604801", "one week", "1.5"])
def test_invalid_artifact_url_ttl_is_a_configuration_error(monkeypatch, raw):
    monkeypatch.setenv("CAREER_LENS_ARTIFACT_URL_TTL_SECONDS", raw)
    with pytest.raises(ConfigError):
        artifact_url_ttl_seconds()
