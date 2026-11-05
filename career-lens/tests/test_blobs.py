import pytest

from career_lens.store.blobs import open_store, sha256


@pytest.fixture
def store():
    """The real MinIO from docker compose. Not a mock: what these tests are
    for is that presigning and HEAD behave, and neither exists in a fake."""
    return open_store()


def test_a_blob_round_trips(store):
    key = "test/roundtrip.txt"
    store.put(key, b"hello", "text/plain")
    assert store.get(key) == b"hello"


def test_head_reports_size_for_a_stored_object(store):
    key = "test/sized.bin"
    store.put(key, b"0123456789", "application/octet-stream")
    assert store.head(key) == 10


def test_head_returns_none_for_an_absent_object(store):
    # The exact case that went undetected in the tool this replaces: 19% of
    # stored keys named objects that were not there.
    assert store.head("test/never-written-{}".format(id(store))) is None


def test_a_presigned_url_carries_a_signature(store):
    store.put("test/signed.txt", b"x", "text/plain")
    url = store.url("test/signed.txt")
    assert "X-Amz-Signature" in url


def test_sha256_is_the_hex_digest():
    assert sha256(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
