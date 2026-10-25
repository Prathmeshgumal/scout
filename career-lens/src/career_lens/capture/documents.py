"""One captured page, as the immutable writer's contract.

The last neutral step of every recipe. It knows about a `PageCapture` and a
`Document` and nothing about any site: the section name is the caller's, and
so is anything site-specific that reached `capture.extra`. Lifted out of
`capture/driver.py` unchanged so that a package which composes the page
primitives directly does not have to reimplement the conversion, or import a
LinkedIn-heavy module to reach it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from . import detect, markdown
from .writer import Document


def to_document(capture, section: str, recorder=None) -> Document:
    """Convert one neutral page capture into the immutable writer contract."""
    # The URL this page actually landed on is what its own links resolve
    # against -- not the URL we asked for, which a redirect may have moved.
    landed = capture.final_url or capture.url_requested
    text = (markdown.to_markdown(capture.html_bytes, landed)
            if capture.html_bytes else None)
    host = detect.host_of(landed)
    return Document(
        section=section,
        url_requested=capture.url_requested,
        url_final=capture.final_url,
        host=host,
        http_status=capture.status,
        captured_at=datetime.now(UTC),
        title=capture.title,
        html=capture.html_bytes,
        text=capture.text,
        request_method=capture.request_method,
        request_headers=capture.request_headers,
        response_headers=capture.response_headers,
        screenshots=list(capture.screenshot_bytes),
        images=dict(capture.images),
        markdown=text,
        markdown_parser=markdown.PARSER if text else None,
        # `or None`: PageCapture defaults this to "", and an empty string in
        # the column would read as a reason we failed to record rather than as
        # a page nobody scrolled.
        scroll_stopped=capture.scroll_stopped or None,
        scroll_growing=capture.scroll_growing,
        screenshot_error=capture.screenshot_error,
        metadata=dict(capture.extra),
        # Drained here rather than at the page load: this is the moment
        # the capture becomes a Document, so everything that settled
        # while it was open belongs to it and nothing later does.
        har=(recorder.take(section, url=capture.final_url or capture.url_requested,
                           title=capture.title)
             if recorder is not None else None),
    )
