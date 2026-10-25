"""Subresources fetched through the page that referenced them.

Fetched rather than intercepted: the browser has already downloaded the image
to draw it, but a response served from cache will not hand its body back, and a
capture that silently keeps nothing is the failure this whole store is built to
avoid. `page.request` carries the session's cookies and proxy, so the fetch
looks like the page asking for its own image, which is what it is.

Through the page rather than a separate client because these URLs are signed
and short-lived -- days, not months. Whatever is not taken now cannot be taken
later.
"""

from __future__ import annotations

from dataclasses import dataclass

TIMEOUT_MS = 15_000

# An avatar is tens of kilobytes and a cover a few hundred. A megabyte is
# already unusual; eight means we are downloading something that is not an
# image, and the point of a ceiling is that nothing decides that for us.
MAX_BYTES = 8 * 1024 * 1024

IMAGE_TYPES = ("image/",)


class MediaFetchFailed(RuntimeError):
    """The image could not be fetched, or what came back was not an image."""


@dataclass(frozen=True)
class Media:
    """One fetched subresource, ready to store."""

    data: bytes
    content_type: str
    url: str


def fetch(page, url: str) -> Media:
    """Fetch one subresource through `page`'s session.

    Raises `MediaFetchFailed` for anything that is not a usable image, so the
    caller records a reason rather than storing an error page as a photograph.
    """
    response = page.request.get(url, timeout=TIMEOUT_MS)
    if not response.ok:
        raise MediaFetchFailed(f"{response.status} fetching {url}")

    content_type = (response.headers or {}).get("content-type", "")
    kind = content_type.split(";")[0].strip().lower()
    if not kind.startswith(IMAGE_TYPES):
        raise MediaFetchFailed(f"{url} served {kind or 'no content type'}")

    data = response.body()
    if not data:
        raise MediaFetchFailed(f"{url} served an empty body")
    if len(data) > MAX_BYTES:
        raise MediaFetchFailed(
            f"{url} served {len(data)} bytes, over the {MAX_BYTES} ceiling")
    return Media(data=data, content_type=kind, url=url)


def extension_for(content_type: str) -> str:
    """The stored key's suffix. Named from the type served, not from the URL,
    which carries no extension at all."""
    return {
        "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif", "image/avif": "avif",
        "image/svg+xml": "svg",
    }.get(content_type, "bin")
