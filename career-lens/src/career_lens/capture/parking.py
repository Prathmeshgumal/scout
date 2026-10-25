"""Conservative recognition of observed, domain-specific sale redirects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterator
from urllib.parse import unquote, urlsplit

from .failures import CaptureFailed
from .writer import Document


def parked_domain(requested: str, landed: str) -> str | None:
    """Recognize an exact requested-domain listing, never content keywords.

    Sales words in content, a different hostname, or even a marketplace host
    alone do not establish parking. Only an observed provider listing for the
    exact requested domain does. Unknown providers remain ordinary captures.
    """
    try:
        source, target = urlsplit(requested), urlsplit(landed)
        if (source.scheme not in {"http", "https"}
                or target.scheme not in {"http", "https"}
                or target.hostname != "forsale.godaddy.com"
                or target.port not in {None, 80, 443}
                or target.username is not None or target.password is not None):
            return
        domain = (source.hostname or "").removeprefix("www.").encode("idna").decode()
        path = unquote(target.path).rstrip("/").casefold()
        if not domain or path != f"/forsale/{domain}":
            return
    except (ValueError, UnicodeError):
        return
    return domain


def reject_parked_redirect(
        requested: str, landed: str, *, http_status: int | None = None,
        ) -> Iterator[Document]:
    """Retain navigation evidence before terminating an unavailable website."""
    domain = parked_domain(requested, landed)
    if domain is None:
        return

    # No HTML/text is read from the sale page. Query/fragment tracking is not
    # needed to establish the listing's identity and is not retained.
    final_url = urlsplit(landed)._replace(query="", fragment="").geturl()
    yield Document(
        section="page", url_requested=requested, url_final=final_url,
        host="forsale.godaddy.com", http_status=http_status,
        captured_at=datetime.now(UTC), title=None, html=None,
        scroll_stopped="not_started",
        metadata={"navigation": {
            "outcome": "domain_for_sale", "provider": "godaddy",
            "listed_domain": domain,
        }},
    )
    raise CaptureFailed("not_found", f"Requested domain is parked / for sale: {domain}")
