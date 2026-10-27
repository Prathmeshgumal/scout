"""Job postings and keyword job searches: build, store and load their documents.

Each reading is one JSON document in the blob store, plus one append-only head
row that a lookup by URL and a freshness check can use. The document is the
evidence; the head row is the index.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import text

from ..capture.failures import CaptureFailed
from ..capture.writer import store_verified
from ..store.blobs import BlobStore, sha256
from ..store.db import transaction
from ..store.ids import EXTRACTION, new_id
from .parse.company_jobs import extract_jobs, job_page_number
from .parse.dom import parse_document

SCHEMA_VERSION = "1.0"
JOB = "linkedin_job"
JOB_SEARCH = "linkedin_job_search"


class JobDocumentMissing(LookupError):
    """No stored document answers this request or URL."""


class JobDocumentCorrupt(RuntimeError):
    """The stored object disagrees with its durable metadata."""


WORKPLACE_TYPES = ("Remote", "Hybrid", "On-site")
EMPLOYMENT_TYPES = ("Full-time", "Part-time", "Contract", "Temporary",
                    "Internship", "Volunteer", "Other")
CLOSED = "No longer accepting applications"
JOB_ID = re.compile(r"/jobs/view/(\d+)/")
POSTED = re.compile(r"\b(?:ago|today|yesterday)$|^(?:Posted|Reposted)\b",
                    re.IGNORECASE)
APPLICANTS = re.compile(r"applicant|clicked apply", re.IGNORECASE)


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def _description(box) -> str:
    """The description's text, keeping its line breaks and paragraphs."""
    # The page's own whitespace, newlines included, is not layout: only
    # breaks and blocks are. So they are marked first and the rest collapsed.
    mark = "\u2029"
    for br in box.iter("br"):
        br.tail = mark + (br.tail or "")
    for block in box.iter("p", "li", "div", "h3", "h4", "ul", "ol"):
        if block is not box:
            block.tail = mark + (block.tail or "")
    lines = [_clean(line) for line in box.text_content().split(mark)]
    text = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def build_job(job_url: str, item) -> dict:
    """The job document from one signed-in capture of its page."""
    match = JOB_ID.search(job_url)
    if match is None:
        raise CaptureFailed("extract_failed", f"{job_url} names no job")
    job_id = match.group(1)
    if not item.html:
        raise CaptureFailed("extract_failed", "the job page has no HTML")
    document = parse_document(item.html.decode("utf-8", "replace"))
    boxes = document.xpath(
        f'//*[@componentkey="JobDetails_AboutTheJob_{job_id}"]'
        '//*[@data-testid="expandable-text-box"]')
    if not boxes:
        raise CaptureFailed(
            "extract_failed", "the job page shows no job description")
    title_parts = [_clean(part) for part in re.sub(
        r"^\(\d+\)\s*", "", _clean(document.findtext(".//title"))).split("|")]
    title = title_parts[0] or None
    company_name = title_parts[1] if len(title_parts) > 2 else None

    about = boxes[0].xpath(
        f'ancestor::*[@componentkey="JobDetails_AboutTheJob_{job_id}"]')[0]
    before = []
    for element in document.iter():
        if element is about:
            break
        before.append(element)
    leaves = [_clean(element.text_content()) for element in before
              if element.tag in ("span", "p", "a")
              and not any(child.tag in ("span", "p", "a") for child in element)]
    company_url = next(
        (_company_url(element.get("href")) for element in before
         if element.tag == "a" and _company_url(element.get("href"))), None)
    line = next((element for element in before if element.tag == "p"
                 and "·" in element.text_content()
                 and len(element.xpath("./span")) >= 2), None)
    parts = ([_clean(span.text_content()) for span in line.xpath("./span")]
             if line is not None else [])
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": JOB,
        "job_url": job_url,
        "job_id": job_id,
        "observed_at": item.captured_at.isoformat(),
        "title": title,
        "company_name": company_name,
        "company_url": company_url,
        "location": parts[0] if parts else None,
        "posted_at": next((part for part in parts[1:] if POSTED.search(part)),
                          None),
        "applicants": next((part for part in parts[1:]
                            if APPLICANTS.search(part)), None),
        "workplace_type": next((leaf for leaf in leaves
                                if leaf in WORKPLACE_TYPES), None),
        "employment_type": next((leaf for leaf in leaves
                                 if leaf in EMPLOYMENT_TYPES), None),
        "posting_status": "closed" if CLOSED in leaves else "open",
        "description_text": _description(boxes[0]),
    }


def _company_url(href: str | None) -> str | None:
    match = re.search(r"linkedin\.com/company/([^/?#]+)", href or "")
    return f"https://www.linkedin.com/company/{match.group(1)}/" if match else None


def extract_job(request_id: str, target: str, documents, by_section, store,
                url: str | None = None, attempt_id: int | None = None,
                **options) -> None:
    item = next((document for document in documents
                 if document.section == "job"), None)
    if item is None or "job" not in by_section:
        raise CaptureFailed("extract_failed", "the job request produced no page")
    document = build_job(target, item)
    _persist(url, request_id, attempt_id, document, {"job": by_section["job"]},
             store)


def search_terms(search_url: str) -> dict[str, str]:
    """The search terms a canonical job search URL carries."""
    return {name: values[0]
            for name, values in parse_qs(urlsplit(search_url).query).items()}


def build_search(search_url: str, documents) -> dict:
    """The job search document from its captured result pages."""
    pages = sorted(
        ((number, item) for item in documents
         if (number := job_page_number(item.section)) is not None
         and item.html),
        key=lambda pair: pair[0])
    if not pages:
        raise CaptureFailed(
            "extract_failed", "the job search produced no result pages")
    result_pages = [
        (number, parse_document(item.html.decode("utf-8", "replace")),
         item.url_final or item.url_requested,
         {"scroll_stopped": item.scroll_stopped, **item.metadata})
        for number, item in pages
    ]
    jobs = extract_jobs(None, result_pages, search_url)
    terms = search_terms(search_url)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": JOB_SEARCH,
        "search_url": search_url,
        "keywords": terms.get("keywords", ""),
        "location": terms.get("location"),
        "observed_at": max(item.captured_at for _, item in pages).isoformat(),
        "captured_pages": jobs["captured_pages"],
        "reached_end": jobs["reached_end"],
        "items": jobs["items"],
    }


def store_document(database_url: str | None, *, request_id: str,
                   attempt_id: int | None, document: dict,
                   inputs: dict[str, str], store: BlobStore) -> str:
    """Store one document, its extraction row and its head row, once."""
    kind = document["kind"]
    extraction_id = new_id(EXTRACTION)
    payload = json.dumps(document, ensure_ascii=False, indent=2,
                         sort_keys=True).encode("utf-8")
    key = f"extractions/{extraction_id}/{kind}.json"
    store_verified(store, key, payload, "application/json")
    with transaction(database_url) as connection:
        connection.execute(text(
            "SELECT pg_advisory_xact_lock(hashtextextended(:request, 0))"),
            {"request": request_id})
        existing = connection.execute(text("""
            SELECT id FROM extraction
            WHERE request_id=:request AND extractor=:extractor
              AND document_key IS NOT NULL
            ORDER BY extracted_at DESC, id DESC LIMIT 1
        """), {"request": request_id, "extractor": kind}).scalar_one_or_none()
        if existing is not None:
            return existing
        connection.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version,
                 extracted_at, outcome, document_key, document_sha256,
                 document_bytes)
            VALUES (:id, :request, :attempt, :extractor, :version, :now,
                    'ok', :key, :digest, :size)
        """), {
            "id": extraction_id, "request": request_id, "attempt": attempt_id,
            "extractor": kind, "version": SCHEMA_VERSION,
            "now": datetime.now(UTC), "key": key, "digest": sha256(payload),
            "size": len(payload),
        })
        for section, capture_id in sorted(inputs.items()):
            connection.execute(text("""
                INSERT INTO extraction_input (extraction_id, capture_id, section)
                VALUES (:extraction, :capture, :section)
            """), {"extraction": extraction_id, "capture": capture_id,
                   "section": section})
        _project(connection, extraction_id, request_id, document, inputs)
    return extraction_id


def _project(connection, extraction_id: str, request_id: str, document: dict,
             inputs: dict[str, str]) -> None:
    values = {"extraction": extraction_id, "request": request_id,
              "observed": document["observed_at"]}
    if document["kind"] == JOB:
        connection.execute(text("""
            INSERT INTO linkedin.job_observation
                (extraction_id, request_id, capture_id, job_url, job_id,
                 title, company_name, posting_status, observed_at)
            VALUES (:extraction, :request, :capture, :url, :job_id, :title,
                    :company, :status, :observed)
        """), {**values, "capture": inputs["job"],
               "url": document["job_url"], "job_id": document["job_id"],
               "title": document.get("title"),
               "company": document.get("company_name"),
               "status": document.get("posting_status")})
        return
    first = min(inputs, key=lambda section: job_page_number(section) or 0)
    connection.execute(text("""
        INSERT INTO linkedin.job_search_observation
            (extraction_id, request_id, capture_id, search_url, keywords,
             location, captured_pages, reached_end, result_count, observed_at)
        VALUES (:extraction, :request, :capture, :url, :keywords, :location,
                :pages, :end, :count, :observed)
    """), {**values, "capture": inputs[first], "url": document["search_url"],
           "keywords": document["keywords"],
           "location": document.get("location"),
           "pages": document["captured_pages"],
           "end": document["reached_end"], "count": len(document["items"])})


def load_document(connection, *, extraction_id: str, store: BlobStore) -> dict:
    """The verified document of one extraction."""
    row = connection.execute(text("""
        SELECT document_key, document_sha256, document_bytes
        FROM extraction WHERE id=:id AND document_key IS NOT NULL
    """), {"id": extraction_id}).one_or_none()
    if row is None:
        raise JobDocumentMissing(f"no job document for extraction {extraction_id}")
    try:
        payload = store.get(row.document_key)
    except Exception as exc:
        raise JobDocumentCorrupt("the job document is unavailable") from exc
    if (not isinstance(payload, bytes) or len(payload) != row.document_bytes
            or sha256(payload) != row.document_sha256):
        raise JobDocumentCorrupt("the job document failed integrity verification")
    return json.loads(payload)


def extract_job_search(request_id: str, target: str, documents, by_section,
                       store, url: str | None = None,
                       attempt_id: int | None = None, **options) -> None:
    document = build_search(target, documents)
    inputs = {section: capture_id for section, capture_id in by_section.items()
              if job_page_number(section) is not None}
    _persist(url, request_id, attempt_id, document, inputs, store)


def _persist(url, request_id, attempt_id, document, inputs, store) -> None:
    try:
        store_document(url, request_id=request_id, attempt_id=attempt_id,
                       document=document, inputs=inputs, store=store)
    except Exception as exc:
        raise CaptureFailed("storage_error", str(exc)[:2000]) from exc
