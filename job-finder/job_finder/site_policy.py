"""The web pages that agents may read. Agents read jobs, companies and people for a job search, nothing else.

read_page checks each URL before career-lens opens it in the worker's Chrome, which runs on this computer."""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

NOT_ABOUT_JOBS = "is a kind of site that is not about jobs (adult, gambling, piracy or dark web)"
LOCAL = "is a local or private address on this network"
NOT_A_PAGE = "is not a web page: give a URL that starts with https:// or http://"

BLOCKED_DOMAINS = frozenset(
    line.strip().lower() for line in (Path(__file__).parent / "blocked_sites.txt").read_text().splitlines()
    if line.strip() and not line.startswith("#"))
BLOCKED_TLDS = frozenset({"onion", "xxx", "porn", "sex", "adult", "casino", "bet", "poker"})
# A whole word of the host name: "free-porn-site.net" matches, "essex.ac.uk" does not.
BLOCKED_WORDS = frozenset({"porn", "xxx", "hentai", "nsfw", "escort", "escorts", "casino", "torrent", "torrents"})
LOCAL_NAMES = ("localhost", ".localhost", ".local", ".internal", ".lan", ".home.arpa")
WORD = re.compile(r"[a-z0-9]+")


def blocked_reason(host: str) -> str | None:
    """Why agents must not read this host name, or None. Only the name: blocked_url_reason checks addresses."""
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if labels[-1] in BLOCKED_TLDS:
        return NOT_ABOUT_JOBS
    if any(".".join(labels[i:]) in BLOCKED_DOMAINS for i in range(len(labels))):
        return NOT_ABOUT_JOBS
    if BLOCKED_WORDS.intersection(WORD.findall(host)):
        return NOT_ABOUT_JOBS
    return None


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    return not ip.is_global or ip.is_multicast


def resolve_host(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def blocked_url_reason(url: str, *, resolve: Callable[[str], list[str]] = resolve_host) -> str | None:
    """Why agents must not read this URL, or None. A host that does not resolve passes: career-lens reports it."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return NOT_A_PAGE
    if parts.scheme.lower() not in ("http", "https") or not host:
        return NOT_A_PAGE
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(LOCAL_NAMES) or _private(host):
        return LOCAL
    if reason := blocked_reason(host):
        return reason
    try:
        ipaddress.ip_address(host)
        return None  # a public address
    except ValueError:
        pass
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError):
        return None
    if any(_private(address) for address in addresses):
        return LOCAL
    return None
