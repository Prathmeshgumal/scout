"""Captured documents, into object storage and then into rows.

The ordering is the whole point. In a real run of the tool this replaces, 39 of
202 stored keys (19%) named objects that did not exist, with nothing detecting
it. So: every object is written and its presence confirmed BEFORE the
transaction that inserts the rows naming it. A blob with no row is garbage,
which costs storage; a row with no blob is corruption, which costs the capture.
Only one of those is acceptable, and the ordering chooses which.

There is no reconciliation sweep. A background scan over object storage would
spend throughput detecting a class of failure this ordering already prevents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text

from ..store.blobs import BlobStore, sha256
from .media import extension_for
from ..store.db import transaction
from ..store.ids import CAPTURE, new_id


class BlobMissing(RuntimeError):
    """An object was uploaded and is not there.

    Distinct from an upload that raised: this is the silent case, where the
    store reported success and kept nothing, and it is the one that produced
    the dangling keys.
    """


@dataclass
class Document:
    """One fetched page, with everything that will be stored about it."""

    section: str
    url_requested: str
    url_final: str | None
    host: str
    http_status: int | None
    captured_at: datetime
    title: str | None
    html: bytes | None
    text: str | None = None
    request_method: str | None = None
    request_headers: dict[str, str] | None = None
    response_headers: dict[str, str] | None = None
    # How completely the page was read. See core.py's note on these columns.
    scroll_stopped: str | None = None
    scroll_growing: bool = False
    screenshot_error: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    # In page order. A page short enough for one image has one entry.
    screenshots: list[bytes] = field(default_factory=list)
    # The page's own images -- an avatar, a cover -- keyed by artifact kind.
    # Values are `capture.media.Media`.
    images: dict[str, object] = field(default_factory=dict)
    markdown: str | None = None
    markdown_parser: str | None = None
    # Gzipped HAR 1.2: every request this page made and what came back.
    # None when request logging is off, or when the page made no requests.
    har: bytes | None = None


@dataclass(frozen=True)
class _Pending:
    """An object already durable, waiting for its row."""

    kind: str
    ordinal: int
    key: str
    content_type: str
    data: bytes


def store_verified(store: BlobStore, key: str, data: bytes,
                   content_type: str) -> None:
    store.put(key, data, content_type)
    size = store.head(key)
    if size is None:
        raise BlobMissing(
            f"{key} was uploaded and is not in the bucket. Nothing referencing "
            f"it has been written.")
    if size != len(data):
        raise BlobMissing(
            f"{key} is {size} bytes in the bucket and {len(data)} were sent.")


def write_capture(url: str | None, *, request_id: str, document: Document,
                  store: BlobStore, browser_profile: str | None = None,
                  attempt_id: int | None = None) -> str:
    """Store one document's payloads, then write its rows in one transaction.

    Returns the capture id.

    One document at a time rather than a batch at the end of a run: a profile
    is nine pages over about two and a half minutes, and a caller that only
    learns about them together cannot show a capture in progress. The ordering
    guarantee is unchanged and now applies per document -- every object is
    durable and verified before the row naming it exists.

    The capture id is generated up front because a blob key contains one, and
    the key has to exist before the object is written. That is safe: an id that
    never reaches a row costs nothing.
    """
    capture_id = new_id(CAPTURE)
    items: list[_Pending] = []
    if document.html is not None:
        items.append(_Pending("html", 0, f"captures/{capture_id}/page.html",
                              "text/html; charset=utf-8", document.html))
    if document.text is not None:
        items.append(_Pending(
            "text", 0, f"captures/{capture_id}/page.txt",
            "text/plain; charset=utf-8", document.text.encode("utf-8")))
    for index, image in enumerate(document.screenshots):
        items.append(_Pending(
            "screenshot", index,
            f"captures/{capture_id}/screen-{index:02d}.png",
            "image/png", image))
    # Beside the screenshots, in the same bucket and under the same key prefix:
    # these are payloads of the capture like any other. Named from the type
    # served, since a signed media URL carries no extension.
    if document.har is not None:
        items.append(_Pending(
            "har", 0, f"captures/{capture_id}/network.har.gz",
            "application/gzip", document.har))
    for kind, found in sorted(document.images.items()):
        items.append(_Pending(
            kind, 0,
            f"captures/{capture_id}/{kind.replace('_', '-')}"
            f".{extension_for(found.content_type)}",
            found.content_type, found.data))

    # Every upload first, outside the transaction. A transaction held open
    # across tens of megabytes of HTTP would hold row locks for the length of
    # an upload, and an upload that fails must leave no rows -- which it does,
    # because none have been written yet.
    for item in items:
        store_verified(store, item.key, item.data, item.content_type)

    with transaction(url) as connection:
        connection.execute(text("""
            INSERT INTO capture
                (id, request_id, attempt_id, section,
                 url_requested, url_final, host,
                 http_status, request_method, request_headers, response_headers,
                 captured_at, browser_profile, title, markdown, markdown_parser,
                 scroll_stopped, scroll_growing, screenshot_error, metadata)
            VALUES
                (:id, :request_id, :attempt_id, :section,
                 :url_requested, :url_final, :host,
                 :http_status, :request_method, CAST(:request_headers AS jsonb),
                 CAST(:response_headers AS jsonb), :captured_at, :browser_profile, :title,
                 :markdown, :markdown_parser,
                 :scroll_stopped, :scroll_growing, :screenshot_error,
                 CAST(:metadata AS jsonb))
        """), {
            "id": capture_id, "request_id": request_id,
            "attempt_id": attempt_id,
            "section": document.section,
            "url_requested": document.url_requested,
            "url_final": document.url_final, "host": document.host,
            "http_status": document.http_status,
            "request_method": document.request_method,
            "request_headers": (json.dumps(document.request_headers)
                                if document.request_headers is not None else None),
            "response_headers": (json.dumps(document.response_headers)
                                 if document.response_headers is not None else None),
            "captured_at": document.captured_at,
            "browser_profile": browser_profile, "title": document.title,
            "markdown": document.markdown,
            "markdown_parser": document.markdown_parser,
            "scroll_stopped": document.scroll_stopped,
            "scroll_growing": document.scroll_growing,
            "screenshot_error": document.screenshot_error,
            "metadata": json.dumps(document.metadata),
        })
        for item in items:
            connection.execute(text("""
                INSERT INTO artifact
                    (capture_id, kind, ordinal, blob_key, content_type,
                     byte_size, sha256)
                VALUES (:c, :k, :o, :key, :ct, :size, :digest)
            """), {
                "c": capture_id, "k": item.kind, "o": item.ordinal,
                "key": item.key, "ct": item.content_type,
                "size": len(item.data), "digest": sha256(item.data),
            })
    return capture_id
