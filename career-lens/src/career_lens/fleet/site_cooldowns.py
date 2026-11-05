"""Durable site/supervisor rate limits, independent of provider identity."""

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import os
import random
from urllib.parse import urlsplit

from sqlalchemy import text

from .retry import Retry, Terminal


def site_key(url: str) -> str:
    # Do not guess registrable domains (co.uk, private hosting, etc.). Known
    # provider subdomains share a site; arbitrary sites use their exact host.
    host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
    for provider in ("linkedin.com", "google.com"):
        if host == provider or host.endswith("." + provider):
            return provider
    return host


def retry_delay(header, *, now: datetime, strikes: int, rng=None) -> timedelta:
    if isinstance(header, str):
        try:
            value = header.strip()
            if value.isascii() and value.isdigit():
                return min(timedelta(seconds=int(value)), datetime.max.replace(tzinfo=UTC) - now)
            date = parsedate_to_datetime(value)
            if date.tzinfo is not None:
                return max(timedelta(0), date.astimezone(UTC) - now)
        except (ValueError, TypeError, OverflowError):
            pass
    seconds = (60, 300, 900)[min(max(strikes, 1), 3) - 1]
    return timedelta(seconds=min(900, seconds * (rng or random.SystemRandom()).uniform(.9, 1.1)))


def automatic_wait_limit() -> timedelta:
    try:
        seconds = int(os.environ.get("CAREER_LENS_RATE_LIMIT_MAX_WAIT_SECONDS", "900"))
        return timedelta(seconds=max(0, seconds))
    except (ValueError, OverflowError):
        return timedelta(minutes=15)


def settle_rate_limit(connection, attempt_id: int, *, now: datetime, metadata=None):
    row = connection.execute(text("""
        SELECT supervisor_id, rate_limit FROM scrape_attempt WHERE id=:id
    """), {"id": attempt_id}).one()
    metadata = metadata or row.rate_limit
    if not metadata:
        return None
    site = site_key(metadata["url"])
    strikes = 1
    previous = None
    if row.supervisor_id:
        # Dispatch locks this same supervisor before its final eligibility
        # check. Publishing a cooldown and assigning new work are serialized.
        connection.execute(text("SELECT id FROM supervisor WHERE id=:id FOR UPDATE"), {"id": row.supervisor_id})
        previous = connection.execute(text("""
            SELECT strikes, last_limited_at, not_before FROM site_cooldown
            WHERE supervisor_id=:node AND site=:site
        """), {"node": row.supervisor_id, "site": site}).one_or_none()
        if previous and previous.last_limited_at > now - timedelta(days=1):
            strikes = previous.strikes + 1
    due = now + retry_delay(metadata.get("retry_after"), now=now, strikes=strikes)
    if previous:
        due = max(due, previous.not_before)
    saved = {"url": metadata["url"], "http_status": 429,
             "retry_after": metadata.get("retry_after"), "site": site,
             "not_before": due.isoformat()}
    connection.execute(text("UPDATE scrape_attempt SET rate_limit=CAST(:data AS jsonb) WHERE id=:id"), {"id": attempt_id, "data": json.dumps(saved)})
    if row.supervisor_id:
        connection.execute(text("""
            INSERT INTO site_cooldown (supervisor_id, site, not_before, strikes, last_limited_at)
            VALUES (:node, :site, :due, :strikes, :now)
            ON CONFLICT (supervisor_id, site) DO UPDATE
            SET not_before=EXCLUDED.not_before, strikes=EXCLUDED.strikes,
                last_limited_at=EXCLUDED.last_limited_at
        """), {"node": row.supervisor_id, "site": site, "due": due, "strikes": strikes, "now": now})
    if due - now > automatic_wait_limit():
        return Terminal("site_rate_limit_long")
    return Retry(due - now, reason="site_rate_limit")


def filter_candidates(connection, request, candidates, reason, *, now):
    if not candidates:
        return candidates, reason
    site = site_key(request.url_normalised or "https://www.google.com/")
    cooldowns = dict(connection.execute(text("""
        SELECT supervisor_id, not_before FROM site_cooldown
        WHERE site=:site AND not_before > :now
    """), {"site": site, "now": now}).all())
    available = [item for item in candidates if item.supervisor_id not in cooldowns]
    if not available:
        # This is node eligibility, not request backoff. Keep checking other
        # nodes: a currently busy, unthrottled node can become available before
        # this cooldown expires.
        return [], "site_rate_limit"
    return available, reason


def failure_detail(connection, attempt_id, detail, decision):
    metadata = connection.execute(text("SELECT rate_limit FROM scrape_attempt WHERE id=:id"), {"id": attempt_id}).scalar_one()
    if not metadata or "not_before" not in metadata:
        return detail
    suffix = " Automatic retries stopped: delay exceeds the configured wait limit." if isinstance(decision, Terminal) and decision.reason == "site_rate_limit_long" else ""
    return f"HTTP 429: {metadata['site']} rate limited this node until {metadata['not_before']}.{suffix}"


def reset_after_success(connection, attempt_id: int, *, now: datetime) -> None:
    row = connection.execute(text("""
        SELECT a.supervisor_id, r.url_normalised
        FROM scrape_attempt a JOIN scrape_request r ON r.id=a.request_id
        WHERE a.id=:id
    """), {"id": attempt_id}).one()
    if row.supervisor_id:
        connection.execute(text("""
            UPDATE site_cooldown SET strikes=0
            WHERE supervisor_id=:node AND site=:site AND not_before <= :now
        """), {"node": row.supervisor_id,
               "site": site_key(row.url_normalised or "https://www.google.com/"), "now": now})
