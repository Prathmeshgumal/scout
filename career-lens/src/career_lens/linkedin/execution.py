"""Transport-neutral LinkedIn extraction from captured documents."""

from __future__ import annotations

from collections.abc import Callable

from ..capture.failures import CaptureFailed


def _render_drift(drift: list[dict[str, str]]) -> str:
    return "\n".join(
        f"{item.get('section')}: {item.get('contract')} - "
        f"expected {item.get('expected')}, found {item.get('found')}"
        for item in drift
    )


def extract_linkedin_person(
    request_id: str,
    target: str,
    documents,
    by_section,
    store,
    database_url: str | None = None,
    *,
    attempt_id: int | None = None,
    requested_sections: frozenset[str] | None = None,
    parser: Callable | None = None,
    persist: Callable | None = None,
) -> None:
    """Parse and store one person result from native or remote evidence."""
    if parser is None:
        from .parse import parse_profile_html

        parser = parse_profile_html
    if persist is None:
        from .project import store_extraction

        persist = store_extraction
    profile = next((document for document in documents
                    if document.section == "profile"), None)
    if profile is None or not profile.html:
        raise CaptureFailed(
            "extract_failed", "the profile page produced no HTML to parse")
    extraction_roles = {
        section: capture_id for section, capture_id in by_section.items()
        if section != "discovery-serp"
    }
    sections = {
        document.section: document.html.decode("utf-8", "replace")
        for document in documents
        if document.section not in ("discovery-serp", "profile", "page")
        and document.html
    }
    section_urls = {
        document.section: document.url_final or document.url_requested
        for document in documents if document.section in sections
    }
    try:
        document = parser(
            profile.html.decode("utf-8", "replace"),
            sections,
            profile_url=target,
            section_urls=section_urls,
            profile_capture={
                "scroll_stopped": profile.scroll_stopped,
                **profile.metadata,
            },
            requested_sections=requested_sections,
        )
    except Exception as exc:
        raise CaptureFailed("extract_failed", str(exc)[:2000]) from exc
    document["provenance"] = profile.metadata.get("provenance") or {
        "arrival_method": "direct", "requested_url": target,
    }
    persist(
        database_url,
        request_id=request_id,
        capture_ids=extraction_roles,
        document=document,
        store=store,
        attempt_id=attempt_id,
    )
    if document.get("drift"):
        raise CaptureFailed(
            "parser_drift", _render_drift(document["drift"])[:2000])


def extract_linkedin_company(
    request_id: str,
    target: str,
    documents,
    by_section,
    store,
    database_url: str | None = None,
    *,
    attempt_id: int | None = None,
    requested_sections: frozenset[str] | None = None,
    parser: Callable | None = None,
    persist: Callable | None = None,
) -> None:
    """Parse and store one company result from native or remote evidence."""
    if parser is None:
        from .parse import parse_company_html

        parser = parse_company_html
    if persist is None:
        from .company_project import store_company_extraction

        persist = store_company_extraction
    home = next(
        (document for document in documents if document.section == "company"),
        None,
    )
    if home is None or not home.html:
        raise CaptureFailed(
            "extract_failed", "the company page produced no HTML to parse",
        )
    extraction_roles = {
        section: capture_id
        for section, capture_id in by_section.items()
        if section != "discovery-serp"
    }
    tabs = {
        document.section: document.html.decode("utf-8", "replace")
        for document in documents
        if document.section not in ("discovery-serp", "company", "page")
        and document.html
    }
    tab_urls = {
        document.section: document.url_final or document.url_requested
        for document in documents
        if document.section in tabs
    }
    tab_capture = {
        document.section: {
            "scroll_stopped": document.scroll_stopped,
            **document.metadata,
        }
        for document in documents
        if document.section in tabs
    }
    provenance = home.metadata.get("provenance") or {
        "arrival_method": "direct",
        "requested_url": target,
    }
    try:
        document = parser(
            home.html.decode("utf-8", "replace"),
            tabs,
            company_url=home.url_final or target,
            tab_urls=tab_urls,
            tab_capture=tab_capture,
            provenance=provenance,
            requested_sections=requested_sections,
        )
    except Exception as exc:
        raise CaptureFailed("extract_failed", str(exc)[:2000]) from exc
    persist(
        database_url,
        request_id=request_id,
        capture_ids=extraction_roles,
        document=document,
        store=store,
        attempt_id=attempt_id,
    )
