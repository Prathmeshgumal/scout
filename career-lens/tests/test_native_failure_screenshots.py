from contextlib import contextmanager

import pytest
from patchright.sync_api import TimeoutError

from career_lens.capture import diagnostics
from career_lens.capture.failures import CaptureFailed


class Page:
    url = "https://example.com/"

    def is_closed(self):
        return False

    def wait_for_function(self, expression, **kwargs):
        return None

    def screenshot(self, **kwargs):
        assert kwargs["full_page"] is False
        assert 0 < kwargs["timeout"] <= 2000
        return b"pixels"


def test_failure_screenshot_is_before_cleanup_and_preserves_exception(monkeypatch):
    events = []

    @contextmanager
    def session():
        try:
            yield Page()
        finally:
            events.append("closed")

    def store(*args, **kwargs):
        events.append("stored")
        return "stored"

    monkeypatch.setattr(diagnostics, "store_failure_screenshot", store)
    error = TimeoutError("navigation timed out")
    with pytest.raises(TimeoutError) as caught:
        with (
            session() as page,
            diagnostics.on_failure(page, attempt_id=1, database_url=None),
        ):
            raise error
    assert caught.value is error
    assert events == ["stored", "closed"]


@pytest.mark.parametrize(
    "error",
    [
        CaptureFailed("cancelled", "stopped"),
        CaptureFailed("login_required", "sign in"),
        CaptureFailed("storage_error", "offline"),
        ValueError("parser error"),
    ],
)
def test_ineligible_failure_never_takes_screenshot(error, monkeypatch):
    page = Page()
    monkeypatch.setattr(page, "screenshot", lambda **kw: pytest.fail("screenshot"))
    with pytest.raises(type(error)) as caught:
        with diagnostics.on_failure(page, attempt_id=1, database_url=None):
            raise error
    assert caught.value is error


def test_success_never_collects_diagnostics(monkeypatch):
    monkeypatch.setattr(
        diagnostics,
        "capture_failure_screenshot",
        lambda **kw: pytest.fail("screenshot"),
    )
    with diagnostics.on_failure(Page(), attempt_id=1, database_url=None):
        pass


def test_missing_browser_element_is_eligible_but_storage_timeout_is_not():
    import builtins
    from patchright.sync_api import Error

    assert diagnostics.eligible_failure(Error("Element not found"))
    assert not diagnostics.eligible_failure(builtins.TimeoutError("upload timed out"))
    wrapped = CaptureFailed("browser_error", "Element not found")
    wrapped.__cause__ = Error("Element not found")
    assert diagnostics.eligible_failure(wrapped)


@pytest.mark.parametrize("fault", ["safety", "screenshot", "upload"])
def test_failed_diagnostics_never_mask_original_error(fault, monkeypatch):
    page = Page()

    def fail(*args, **kwargs):
        raise OSError("diagnostics failed")

    if fault == "safety":
        monkeypatch.setattr(page, "wait_for_function", fail)
    elif fault == "screenshot":
        monkeypatch.setattr(page, "screenshot", fail)
    else:
        monkeypatch.setattr(diagnostics, "store_failure_screenshot", fail)
    error = TimeoutError("navigation timed out")
    with pytest.raises(TimeoutError) as caught:
        with diagnostics.on_failure(page, attempt_id=1, database_url=None):
            raise error
    assert caught.value is error
