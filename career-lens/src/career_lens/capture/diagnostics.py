"""Optional failure evidence, never an extraction input or a new failure cause."""

from __future__ import annotations

from datetime import UTC, datetime
from contextlib import contextmanager
from io import BytesIO
import time

from PIL import Image
from sqlalchemy import make_url, text

from ..store.blobs import S3BlobStore, sha256
from ..store.config import blob_config
from ..store.config import database_url as configured_database_url
from ..store.db import transaction

DIAGNOSTIC_BUDGET_SECONDS = 5
MAX_DIAGNOSTIC_BYTES = 5 * 1024 * 1024

# Fail closed, including frames whose credential fields cannot be inspected.
SAFE_PAGE = """() => {
  const url = new URL(location.href);
  return ['https:', 'http:'].includes(url.protocol)
    && !/login|signin|sign-in|auth|checkpoint|challenge|password|recover|oauth/i.test(url.pathname)
    && !url.username && !url.password && !url.hash
    && ![...url.searchParams.keys()].some(key => /token|password|secret|credential|code/i.test(key))
    && !!document.body
    && !document.querySelector('input[type="password"], input[autocomplete="one-time-code"], iframe, frame');
}"""


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("diagnostic deadline expired")
    return seconds


def diagnostic_store(*, deadline: float):
    # Optional diagnostics must not inherit the normal capture upload retries.
    return S3BlobStore(blob_config(), timeout_seconds=min(0.5, remaining(deadline) / 8))


def validate_image(image: bytes) -> None:
    if not image or len(image) > MAX_DIAGNOSTIC_BYTES:
        raise ValueError("invalid diagnostic image size")
    try:
        with Image.open(BytesIO(image)) as decoded:
            if decoded.format != "PNG" or decoded.width * decoded.height > 16_000_000:
                raise ValueError("invalid diagnostic image")
            decoded.verify()
    except Exception as error:
        raise ValueError("invalid diagnostic image") from error


def eligible_failure(error: Exception) -> bool:
    from .failures import CaptureFailed

    if isinstance(error, CaptureFailed):
        return error.failure_kind == "timeout" or (
            error.failure_kind == "browser_error"
            and isinstance(error.__cause__, Exception)
            and eligible_failure(error.__cause__)
        )
    browser_error = type(error).__module__.startswith(("playwright.", "patchright."))
    return browser_error and (
        type(error).__name__ == "TimeoutError"
        or ("element" in str(error).lower() and "not found" in str(error).lower())
    )


@contextmanager
def on_failure(page, *, attempt_id: int, database_url: str | None):
    try:
        yield
    except Exception as error:
        if eligible_failure(error):
            try:
                from . import trace

                with trace.phase(trace.WORK, "failure screenshot") as phase:
                    phase.detail = "unavailable"
                    phase.detail = capture_failure_screenshot(
                        page, attempt_id=attempt_id, database_url=database_url
                    )
            except Exception:
                # Diagnostics cannot become the attempt's failure or stop cleanup.
                pass
        raise


def capture_failure_screenshot(
    page, *, attempt_id: int, database_url: str | None
) -> str:
    deadline = time.monotonic() + DIAGNOSTIC_BUDGET_SECONDS
    if page.is_closed():
        return "unavailable"
    page.wait_for_function(SAFE_PAGE, timeout=500)
    image = page.screenshot(
        type="png", full_page=False, timeout=min(2000, max(1, int(remaining(deadline) * 1000)))
    )
    page.wait_for_function(
        SAFE_PAGE, timeout=min(500, max(1, int(remaining(deadline) * 1000)))
    )
    return store_failure_screenshot(
        attempt_id, image, database_url=database_url, deadline=deadline
    )


def store_failure_screenshot(
    attempt_id: int,
    image: bytes,
    *,
    database_url: str | None = None,
    deadline: float,
    authorize=None,
) -> str:
    validate_image(image)
    remaining(deadline)
    digest = sha256(image)
    # Content-addressing avoids overwriting the first screenshot on a repeated
    # delivery; a differing retry may leave an unreferenced blob, never mutate evidence.
    key = f"attempt-diagnostics/{attempt_id}/{digest}.png"
    store = diagnostic_store(deadline=deadline)
    store.put(key, image, "image/png")
    remaining(deadline)
    if store.head(key) != len(image):
        raise OSError("diagnostic write not verified")
    remaining(deadline)
    # Keep connection/schema checks bounded too, without changing normal writes.
    url = make_url(database_url or configured_database_url())
    options = str(url.query.get("options", ""))
    ms = max(1, int(remaining(deadline) * 1000))
    url = url.update_query_dict(
        {
            "connect_timeout": "2",
            "options": f"{options} -c statement_timeout={ms} -c lock_timeout={ms}",
        }
    )
    with transaction(url.render_as_string(hide_password=False)) as connection:
        connection.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(max(1, int(remaining(deadline) * 1000)))},
        )
        if authorize is not None:
            authorize(connection)
        remaining(deadline)
        connection.execute(
            text("""
            INSERT INTO attempt_diagnostic
                (attempt_id, blob_key, content_type, byte_size, sha256, captured_at)
            VALUES (:attempt, :key, 'image/png', :size, :digest, :now)
            ON CONFLICT (attempt_id) DO NOTHING
        """),
            {
                "attempt": attempt_id,
                "key": key,
                "size": len(image),
                "digest": digest,
                "now": datetime.now(UTC),
            },
        )
    return "stored"
