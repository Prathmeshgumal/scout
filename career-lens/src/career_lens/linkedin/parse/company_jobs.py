"""Job listings captured from a LinkedIn company Jobs tab."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from lxml.html import HtmlElement

from .company_common import class_xpath, first, resolve_url, text


JOB_PAGE = re.compile(r"^jobs-page-(?P<number>\d+)$")
JOB_VIEW = re.compile(r"/jobs/view/(?P<identifier>\d+)/?")
JOB_CARD_KEY = "job-card-component-ref-"
SEPARATOR_ONLY = re.compile(r"^[\s\u00b7|\-]+$")
LOCATION_WITH_WORKPLACE = re.compile(
    r"^(?P<location>.+?)\s*\((?P<workplace>On-site|Hybrid|Remote)\)$",
    re.IGNORECASE,
)
POSTING_AGE = re.compile(
    r"^(?:today|yesterday|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)$",
    re.IGNORECASE,
)
POSTING_WITH_STATUS = re.compile(
    r"^(?P<status>Posted|Reposted)\s+(?P<age>.+)$",
    re.IGNORECASE,
)
CURRENT_POSTING_WITH_STATUS = re.compile(
    r"^(?P<status>Posted|Reposted)\s+"
    r"(?P<age>today|yesterday|\d+\s+(?:minute|hour|day|week|month|year)s?\s+ago)",
    re.IGNORECASE,
)
WORKPLACE_TYPES = {
    "hybrid": "Hybrid",
    "on-site": "On-site",
    "remote": "Remote",
}
ALUMNI_WORKPLACE_INSIGHT = re.compile(
    r"\b(?:school|company) alumni(?: who| that)? work here$", re.IGNORECASE
)


def job_page_number(name: str) -> int | None:
    """Return the captured result-page number encoded in a section name."""
    match = JOB_PAGE.fullmatch(name)
    return int(match.group("number")) if match else None


def _visible_text(element: HtmlElement | None) -> str | None:
    if element is None:
        return None
    parts = element.xpath(
        ".//text()[not(ancestor::*[@aria-hidden='true']) "
        "and not(ancestor::script) and not(ancestor::style)]"
    )
    value = " ".join(" ".join(str(part).split()) for part in parts).strip()
    return " ".join(value.split()) or None


def _job_id(url: str | None) -> str | None:
    if not url:
        return None
    match = JOB_VIEW.search(urlsplit(url).path)
    if match:
        return match.group("identifier")
    values = parse_qs(urlsplit(url).query).get("currentJobId", [])
    return values[0] if values and values[0].isdigit() else None


def _job_url(identifier: str | None, url: str | None, base_url: str) -> str | None:
    if identifier:
        return f"https://www.linkedin.com/jobs/view/{identifier}/"
    return resolve_url(url, base_url)


def _job_fields(details: Sequence[str | None]) -> dict[str, object]:
    values = [
        value.strip()
        for value in details
        if (value and value.strip() and not SEPARATOR_ONLY.fullmatch(value)
            and not ALUMNI_WORKPLACE_INSIGHT.search(value.strip()))
    ]
    company_name = values.pop(0) if values else None

    posted_at = None
    posting_status = None
    for index, value in enumerate(values):
        status_match = (
            CURRENT_POSTING_WITH_STATUS.match(value)
            or POSTING_WITH_STATUS.fullmatch(value)
        )
        if status_match:
            posting_status = status_match.group("status").lower()
            posted_at = status_match.group("age")
        elif POSTING_AGE.fullmatch(value):
            posted_at = value
        else:
            continue
        values.pop(index)
        break

    location = None
    workplace_type = None
    if values:
        location_value = values.pop(0)
        location_match = LOCATION_WITH_WORKPLACE.fullmatch(location_value)
        normalized = WORKPLACE_TYPES.get(location_value.lower())
        if location_match:
            location = location_match.group("location").strip() or None
            workplace_type = WORKPLACE_TYPES[
                location_match.group("workplace").lower()
            ]
        elif normalized:
            workplace_type = normalized
        else:
            location = location_value

    return {
        "company_name": company_name,
        "location": location,
        "workplace_type": workplace_type,
        "posted_at": posted_at,
        "posting_status": posting_status,
        "insights": values,
    }


def _item(
    *,
    identifier: str | None,
    title: str | None,
    url: str | None,
    details: Sequence[str | None],
    page: int | None,
    position: int,
) -> dict[str, object] | None:
    if not title:
        return None
    return {
        "id": identifier,
        "title": title,
        "url": url,
        **_job_fields(details),
        "page": page,
        "position": position,
    }


def _semantic_results(
    document: HtmlElement, source_url: str, page: int
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    articles = document.xpath(
        ".//main[./h1[normalize-space()='Job listings']]/article"
    )
    for position, article in enumerate(articles, start=1):
        anchor = first(article, ".//h2//a[@href]")
        if anchor is None:
            continue
        url = resolve_url(anchor.get("href"), source_url, keep_query=False)
        identifier = _job_id(url)
        item = _item(
            identifier=identifier,
            title=text(anchor),
            url=_job_url(identifier, url, source_url),
            details=[text(node) for node in article.xpath("./p")],
            page=page,
            position=position,
        )
        if item is not None:
            items.append(item)
    return items


def _legacy_results(
    document: HtmlElement, source_url: str, page: int
) -> list[dict[str, object]]:
    """Read old full-page artifacts using the same result-card boundary as capture."""
    items: list[dict[str, object]] = []
    lists = document.xpath(
        ".//*[@data-testid='lazy-column' "
        "and @componentkey='SearchResultsMainContent']"
    )
    cards = (
        lists[0].xpath(
            ".//*[@componentkey and "
            f"starts-with(@componentkey, '{JOB_CARD_KEY}')]"
        )
        if lists
        else []
    )
    for position, card in enumerate(cards, start=1):
        key = card.get("componentkey") or ""
        identifier = key.removeprefix(JOB_CARD_KEY)
        if not identifier.isdigit():
            continue

        title = None
        for node in card.xpath(".//*[@aria-label]"):
            label = " ".join((node.get("aria-label") or "").split())
            if label.startswith("Dismiss ") and label.endswith(" job"):
                title = label.removeprefix("Dismiss ").removesuffix(" job")
                break
        lines = [
            value
            for node in card.xpath(".//p")
            if (value := _visible_text(node))
        ]
        if title is None and lines:
            title = re.sub(r" \(Verified job\).*$", "", lines[0])
        item = _item(
            identifier=identifier,
            title=title,
            url=_job_url(identifier, None, source_url),
            details=lines[1:],
            page=page,
            position=position,
        )
        if item is not None:
            items.append(item)
    return items


def _result_items(
    document: HtmlElement, source_url: str, page: int
) -> list[dict[str, object]]:
    semantic = _semantic_results(document, source_url, page)
    return semantic if semantic else _legacy_results(document, source_url, page)


def _preview_items(document: HtmlElement, source_url: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    anchors: list[HtmlElement] = []
    for card in document.xpath(
        class_xpath("org-jobs-recently-posted-job-card__container")
    ):
        anchor = first(card, class_xpath("job-card-square__link"))
        if anchor is not None:
            anchors.append(anchor)
    if not anchors and document.xpath(
        ".//main//h2[normalize-space()='Recently posted jobs']"
    ):
        anchors = document.xpath(
            ".//main//a[@href][contains(@href, 'currentJobId=')]"
        )
    for position, anchor in enumerate(anchors, start=1):
        href = anchor.get("href")
        identifier = _job_id(resolve_url(href, source_url))
        title = _visible_text(first(anchor, class_xpath("job-card-square__title")))
        current_lines = [
            value for node in anchor.xpath(".//p")
            if (value := _visible_text(node))
        ]
        if title is None and current_lines:
            title = re.sub(r"\s+\(Verified job\).*$", "", current_lines[0])
        details = (
            current_lines[1:]
            if current_lines
            else [
                _visible_text(first(anchor, class_xpath("job-card-container__company-name"))),
                _visible_text(first(anchor, class_xpath("job-card-container__metadata-wrapper"))),
                _visible_text(first(anchor, ".//time")),
            ]
        )
        item = _item(
            identifier=identifier,
            title=title,
            url=_job_url(identifier, href, source_url),
            details=details,
            page=None,
            position=position,
        )
        if item is not None:
            items.append(item)
    return items


def _is_confirmed_empty(document: HtmlElement | None) -> bool:
    if document is None:
        return False
    return bool(document.xpath(class_xpath("org-jobs-empty-jobs-module")))


def _identity(item: Mapping[str, object]) -> tuple[object, ...]:
    identifier = item.get("id")
    if identifier:
        return ("id", identifier)
    url = item.get("url")
    if url:
        return ("url", url)
    return (
        "content",
        item.get("title"),
        item.get("company_name"),
        item.get("location"),
        item.get("workplace_type"),
        item.get("posted_at"),
    )


def extract_jobs(
    jobs_document: HtmlElement | None,
    result_pages: Sequence[
        tuple[int, HtmlElement, str, Mapping[str, object] | None]
    ],
    source_url: str,
) -> dict[str, object]:
    """Read company job cards without including the selected-job account pane."""
    items: list[dict[str, object]] = []
    ordered_pages = sorted(result_pages, key=lambda value: value[0])
    for page, document, page_url, _capture in ordered_pages:
        items.extend(_result_items(document, page_url, page))
    if not result_pages and jobs_document is not None:
        items = _preview_items(jobs_document, source_url)
        if not items and jobs_document.xpath(
            ".//main//a[@href][contains(@href, 'currentJobId=')]"
        ):
            raise ValueError("LinkedIn company Jobs content could not be read")

    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for item in items:
        identity = _identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated.append(item)

    reached_end = None
    if ordered_pages:
        last_capture = ordered_pages[-1][3]
        has_more = (
            last_capture.get("has_more")
            if isinstance(last_capture, Mapping) else None
        )
        if isinstance(has_more, bool):
            reached_end = not has_more
    elif _is_confirmed_empty(jobs_document):
        reached_end = True

    return {
        "source_url": source_url,
        "captured_pages": len(result_pages),
        "reached_end": reached_end,
        "items": deduplicated,
    }
