"""Safe names for what comes back from a capture."""

from __future__ import annotations

import re


def slugify(value: str, *, limit: int = 60) -> str:
    """A filesystem-safe name for anything: a URL, a query, a host.

    Here rather than with the capture code that names files with it. It is
    string arithmetic, and this module is the one that has to work before a
    browser exists.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (cleaned[:limit] or "untitled").strip("-")
