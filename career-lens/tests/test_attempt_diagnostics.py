from io import BytesIO
import time

from PIL import Image
import pytest
from sqlalchemy import text

from fleet_support import _anonymous_attempt
from career_lens.capture import diagnostics


def png():
    output = BytesIO()
    Image.new("RGB", (20, 10), "white").save(output, format="PNG")
    return output.getvalue()


class Store:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type):
        self.objects[key] = data

    def head(self, key):
        return len(self.objects[key])


def test_diagnostic_is_separate_idempotent_evidence(app, app_url, monkeypatch):
    _, attempt = _anonymous_attempt(app_url, kind="generic")
    store = Store()
    monkeypatch.setattr(diagnostics, "diagnostic_store", lambda **kw: store)
    for _ in range(2):
        result = diagnostics.store_failure_screenshot(
            attempt, png(), database_url=app_url, deadline=time.monotonic() + 5
        )
        assert result == "stored"
    assert (
        app.execute(text("SELECT count(*) FROM attempt_diagnostic")).scalar_one() == 1
    )
    assert app.execute(text("SELECT count(*) FROM capture")).scalar_one() == 0
    row = app.execute(text("SELECT blob_key, byte_size FROM attempt_diagnostic")).one()
    assert len(store.objects[row.blob_key]) == row.byte_size


@pytest.mark.parametrize(
    "image", [b"not an image", b"\x89PNG\r\n\x1a\n", b"x" * (5 * 1024 * 1024 + 1)]
)
def test_invalid_diagnostic_never_reaches_store(image, monkeypatch):
    monkeypatch.setattr(
        diagnostics, "diagnostic_store", lambda **kw: pytest.fail("opened store")
    )
    with pytest.raises(ValueError):
        diagnostics.store_failure_screenshot(1, image, deadline=time.monotonic() + 5)


def test_diagnostic_upload_failure_does_not_create_evidence(app, app_url, monkeypatch):
    _, attempt = _anonymous_attempt(app_url, kind="generic")

    class BrokenStore(Store):
        def put(self, *args):
            raise OSError("offline")

    monkeypatch.setattr(diagnostics, "diagnostic_store", lambda **kw: BrokenStore())
    with pytest.raises(OSError):
        diagnostics.store_failure_screenshot(
            attempt, png(), database_url=app_url, deadline=time.monotonic() + 5
        )
    assert (
        app.execute(text("SELECT count(*) FROM attempt_diagnostic")).scalar_one() == 0
    )


def test_expired_diagnostic_never_opens_store(monkeypatch):
    monkeypatch.setattr(
        diagnostics, "diagnostic_store", lambda **kw: pytest.fail("opened store")
    )
    with pytest.raises(TimeoutError):
        diagnostics.store_failure_screenshot(1, png(), deadline=time.monotonic() - 1)


def test_final_authorization_failure_keeps_metadata_absent(app, app_url, monkeypatch):
    _, attempt = _anonymous_attempt(app_url, kind="generic")
    monkeypatch.setattr(diagnostics, "diagnostic_store", lambda **kw: Store())

    def rejected(connection):
        raise ValueError("lease expired during upload")

    with pytest.raises(ValueError):
        diagnostics.store_failure_screenshot(
            attempt,
            png(),
            database_url=app_url,
            deadline=time.monotonic() + 5,
            authorize=rejected,
        )
    assert (
        app.execute(text("SELECT count(*) FROM attempt_diagnostic")).scalar_one() == 0
    )


