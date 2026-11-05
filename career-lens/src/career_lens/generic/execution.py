"""Extract direct URL observations from durable page evidence."""

from ..capture.failures import CaptureFailed
from .contract import build
from .store import store_extraction


def extract_generic(request_id: str, target: str, documents, by_section, store,
                    url: str | None = None, attempt_id: int | None = None,
                    *, persist=store_extraction, **options) -> None:
    page = next((item for item in documents if item.section == "page"), None)
    capture_id = by_section.get("page")
    if page is None or capture_id is None:
        raise CaptureFailed(
            "extract_failed", "the generic request produced no page capture")
    try:
        document = build(page, capture_id=capture_id, normalised_url=target)
    except Exception as exc:
        raise CaptureFailed("extract_failed", str(exc)[:2000]) from exc
    try:
        persist(url, request_id=request_id, attempt_id=attempt_id,
                capture_id=capture_id, document=document, store=store)
    except Exception as exc:
        raise CaptureFailed("storage_error", str(exc)[:2000]) from exc
