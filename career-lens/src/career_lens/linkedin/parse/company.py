"""Assemble structured JSON from LinkedIn company captures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Callable

from lxml.etree import ParserError

from .company_about import extract_about
from .company_common import (
    available_company_sections,
    company_events_available,
    extract_affiliated_pages,
    extract_company_header,
)
from .company_events import extract_events
from .company_home import extract_home
from .company_jobs import extract_jobs, job_page_number
from .company_life import extract_life
from .company_people import extract_people
from .company_posts import extract_posts
from .company_products import extract_product
from .company_services import extract_services
from .dom import parse_document


COMPANY_SECTIONS = (
    "home", "events", "about", "services", "posts", "products", "jobs",
    "life", "people",
)


def parse_company_html(
    home_html: bytes | str,
    tabs: Mapping[str, bytes | str],
    *,
    company_url: str,
    tab_urls: Mapping[str, str] | None = None,
    tab_capture: Mapping[str, Mapping[str, object]] | None = None,
    provenance: Mapping[str, object] | None = None,
    requested_sections: frozenset[str] | None = None,
) -> dict[str, object]:
    """Parse independently captured company tabs into one stable document."""
    document = parse_document(home_html)
    company = extract_company_header(document, company_url)
    company["affiliated_pages"] = extract_affiliated_pages(document, str(company["url"]))
    sources: dict[str, object] = {
        "company": {"artifact": None, "url": company["url"]}
    }
    available = available_company_sections(document, str(company["url"]))
    events_available = company_events_available(document, str(company["url"]))
    statuses = {
        section: (
            "not_requested"
            if requested_sections is not None and section not in requested_sections
            else "not_available"
            if (
                available is not None
                and (
                    (section == "events" and not events_available)
                    or (section not in {"home", "events"} and section not in available)
                )
            )
            else "not_captured"
        )
        for section in COMPANY_SECTIONS
    }
    warnings: list[dict[str, str]] = []

    def requested(section: str) -> bool:
        return requested_sections is None or section in requested_sections

    def parse_section(
        section: str, source_url: str, extractor: Callable[[], object]
    ) -> dict[str, object]:
        source = {"artifact": None, "url": source_url}
        sources[section] = source
        try:
            company[section] = extractor()
        except (ParserError, TypeError, ValueError) as exc:
            statuses[section] = "parse_failed"
            warnings.append({
                "code": "company_section_parse_failed",
                "section": section,
                "message": str(exc),
            })
        else:
            statuses[section] = "parsed"
        return source

    if requested("home"):
        parse_section(
            "home", str(company["url"]),
            lambda: extract_home(document, str(company["url"])),
        )
    if requested("events") and "events" in tabs:
        events_url = (tab_urls or {}).get("events", f"{company['url']}events/")
        parse_section(
            "events", events_url,
            lambda: extract_events(parse_document(tabs["events"]), events_url),
        )
    if requested("about") and "about" in tabs:
        about_url = (tab_urls or {}).get(
            "about", f"{company['url']}about/"
        )
        parse_section(
            "about", about_url,
            lambda: extract_about(parse_document(tabs["about"]), about_url),
        )
    if requested("posts") and "posts" in tabs:
        posts_url = (tab_urls or {}).get(
            "posts", f"{company['url']}posts/"
        )
        parse_section(
            "posts", posts_url,
            lambda: extract_posts(
                parse_document(tabs["posts"]), posts_url,
                (tab_capture or {}).get("posts"),
            ),
        )
    if requested("services") and "services" in tabs:
        services_url = (tab_urls or {}).get(
            "services", f"{company['url']}services/"
        )
        parse_section(
            "services", services_url,
            lambda: extract_services(
                parse_document(tabs["services"]), services_url
            ),
        )
    product_tab = (
        "product"
        if "product" in tabs
        else "products"
        if "products" in tabs
        else None
    )
    if requested("products") and product_tab is not None:
        product_url = (tab_urls or {}).get(
            product_tab, f"{company['url']}product/"
        )
        parse_section(
            "products", product_url,
            lambda: extract_product(parse_document(tabs[product_tab]), product_url),
        )
    job_page_names = sorted(
        (
            (page, name)
            for name in tabs
            if (page := job_page_number(name)) is not None
        ),
        key=lambda value: value[0],
    )
    if requested("jobs") and ("jobs" in tabs or job_page_names):
        jobs_url = (tab_urls or {}).get("jobs", f"{company['url']}jobs/")

        def parse_jobs() -> object:
            result_pages = [
                (
                    page,
                    parse_document(tabs[name]),
                    (tab_urls or {}).get(name, jobs_url),
                    (tab_capture or {}).get(name),
                )
                for page, name in job_page_names
            ]
            jobs_document = parse_document(tabs["jobs"]) if "jobs" in tabs else None
            return extract_jobs(jobs_document, result_pages, jobs_url)

        job_source = parse_section("jobs", jobs_url, parse_jobs)
        job_source["result_pages"] = [
            {
                "page": page,
                "artifact": None,
                "url": (tab_urls or {}).get(name, jobs_url),
            }
            for page, name in job_page_names
        ]
    if requested("life") and "life" in tabs:
        life_url = (tab_urls or {}).get("life", f"{company['url']}life/")
        parse_section(
            "life", life_url,
            lambda: extract_life(parse_document(tabs["life"]), life_url),
        )
    if requested("people") and "people" in tabs:
        people_url = (tab_urls or {}).get(
            "people", f"{company['url']}people/"
        )
        parse_section(
            "people", people_url,
            lambda: extract_people(
                parse_document(tabs["people"]),
                people_url,
                (tab_capture or {}).get("people"),
            ),
        )
    return {
        "schema_version": "1.1",
        "company": company,
        "sources": sources,
        "section_status": statuses,
        "warnings": warnings,
        "provenance": dict(provenance or {
            "arrival_method": "direct", "requested_url": company_url,
        }),
    }
