"""Accepting a request. One implementation, two callers.

The insert and enqueue happen in one transaction: a job for a request that
does not exist is impossible, and so is a request nobody will pick up.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..capture.options import (
    LINKEDIN_COMPANY_SECTIONS,
    LINKEDIN_PERSON_SECTIONS,
    normalise_linkedin_sections,
    normalise_max_attempts,
    normalise_tempo,
)
from ..capture.urls import canonical, classify
from ..google.contract import normalise_requested_query, normalise_top_n
from ..observe import freshness, resolve
from ..store.db import transaction
from ..store.ids import REQUEST, new_id


class KindMismatch(ValueError):
    """The URL is not the kind the path said it was."""


class InvalidLabel(ValueError):
    """A caller-supplied request label is not usable."""


class RequestOptionsConflict(ValueError):
    """An outstanding URL was requested with a different capture scope."""


class InvalidPriority(ValueError):
    """A request priority is outside the public queue vocabulary."""


class Submission(NamedTuple):
    """What became of one submission.

    A NamedTuple rather than a widening tuple because three of the four
    fields are only interesting when freshness was asked for, and every
    caller that ignores freshness should keep reading the first two by name.
    """

    request_id: str
    created: bool
    # Set only when the stored history already answered. `sections` maps each
    # requested unit to the capture that answered for it.
    satisfied_by: dict | None = None
    # The units that were not recent enough, when freshness was asked for and
    # not met. Without this, "freshness never satisfies this target" is a
    # silent condition an operator cannot diagnose.
    stale: tuple[str, ...] = ()
    coverage: dict | None = None
    status: str = "queued"
    sections: tuple[str, ...] | None = None


MAX_LABEL_LENGTH = 120
REQUEST_PRIORITIES = ("normal", "high")


def normalise_label(label: str | None) -> str | None:
    if label is None:
        return None
    if not isinstance(label, str):
        raise InvalidLabel("label must be text")
    value = " ".join(label.split())
    if not value:
        return None
    if len(value) > MAX_LABEL_LENGTH:
        raise InvalidLabel(
            f"label must be at most {MAX_LABEL_LENGTH} characters")
    return value


def normalise_priority(priority: object | None) -> str:
    value = "normal" if priority is None else priority
    if not isinstance(value, str) or value not in REQUEST_PRIORITIES:
        raise InvalidPriority("priority must be normal or high")
    return value


def display_target(label: str | None, *, url: str | None,
                   query: str | None) -> str:
    """The supplied label, or a compact name derived from the target."""
    if label:
        return label
    if query is not None:
        return query
    if url is None:
        return ""
    parts = urlsplit(url)
    segments = [unquote(part) for part in parts.path.split("/") if part]
    host = (parts.hostname or "").removeprefix("www.")
    if (host == "linkedin.com" or host.endswith(".linkedin.com")) and len(segments) >= 2:
        if segments[0] in {"in", "company"}:
            return segments[1].replace("-", " ").replace("_", " ")
    path = "/".join(segments)
    return f"{host}/{path}" if path else (host or url)


def record_interest(connection, request_id: str, api_key_id: str | None) -> None:
    """Note that a caller wants a request: an API key, or with None a caller
    that has no key. A key that withdrew and asks again wants it again."""
    if api_key_id is None:
        connection.execute(text("""
            INSERT INTO request_interest (request_id, created_at)
            VALUES (:r, :t)
            ON CONFLICT (request_id) WHERE api_key_id IS NULL DO NOTHING
        """), {"r": request_id, "t": datetime.now(UTC)})
        return
    connection.execute(text("""
        INSERT INTO request_interest (request_id, api_key_id, created_at)
        VALUES (:r, :k, :t)
        ON CONFLICT (request_id, api_key_id) WHERE api_key_id IS NOT NULL
        DO UPDATE SET withdrawn_at = NULL
    """), {"r": request_id, "k": api_key_id, "t": datetime.now(UTC)})


def submit(url: str, *, label: str | None = None,
           tempo: object | None = None,
           linkedin_sections: object | None = None,
           max_attempts: object | None = None,
           expected_kind: str | None = None,
           database: str | None = None,
           freshness_window: object | None = None,
           api_key_id: str | None = None,
           connection: Connection | None = None) -> Submission:
    """Submit a URL, or answer it from what is already stored.

    An outstanding request is reused. A URL whose prior requests have all
    finished may be requested again.

    `api_key_id` is the consumer's key, which may later withdraw its interest.
    None is a caller without a key, whose interest keeps the work alive.
    """
    normalised = canonical(url)
    kind = classify(normalised)
    if expected_kind is not None and kind != expected_kind:
        raise KindMismatch(
            f"{normalised} is a {kind} URL, but it was posted to the "
            f"{expected_kind} endpoint")

    tempo_name = normalise_tempo(tempo, kind=kind)
    selected_sections = (
        normalise_linkedin_sections(linkedin_sections, kind=kind)
        if linkedin_sections is not None
        else None
    )
    stored_sections = (
        sorted(selected_sections) if selected_sections is not None else None
    )
    request_label = normalise_label(label)
    attempt_ceiling = normalise_max_attempts(max_attempts)
    seconds = freshness.parse(freshness_window)
    # Composite submissions link their domain record in the same transaction.
    with (nullcontext(connection) if connection is not None
          else transaction(database)) as connection:
        # Before deduplication on purpose: a caller who said 24h and can be
        # answered from two hours ago should not be made to wait on a run
        # they do not need.
        answer = None
        coverage = None
        if seconds is not None:
            freshness_selection = selected_sections
            answer = _satisfy(connection, kind=kind, key=normalised,
                              seconds=seconds, selected=freshness_selection,
                              now=datetime.now(UTC))
            coverage = assess_coverage(connection, kind=kind, key=normalised,
                                       seconds=seconds, selected=freshness_selection,
                                       answer=answer)
            if answer.satisfied:
                return _satisfied_request(
                    connection, url=url, normalised=normalised, kind=kind,
                    label=request_label, tempo=tempo_name,
                    sections=stored_sections,
                    max_attempts=attempt_ceiling,
                    seconds=seconds, answer=answer)._replace(
                        coverage=coverage, status="satisfied",
                        sections=tuple(_freshness_roles(kind, freshness_selection)))

        # FOR KEY SHARE: a cancel takes FOR UPDATE on this row, so it cannot
        # count the interests between this read and the one recorded below. A
        # request that a cancel closed meanwhile no longer matches, and the
        # caller gets a new one.
        outstanding = connection.execute(text("""
            SELECT id, status, linkedin_sections
            FROM scrape_request
            WHERE url_normalised = :u
              AND status IN ('queued', 'assigned', 'running')
            FOR KEY SHARE
        """), {"u": normalised}).one_or_none()
        if outstanding is not None:
            if outstanding.linkedin_sections != stored_sections:
                raise RequestOptionsConflict(
                    "an outstanding request for this URL has a different "
                    "LinkedIn section selection")
            record_interest(connection, outstanding.id, api_key_id)
            return Submission(outstanding.id, False,
                              stale=tuple(answer.stale) if answer else (), coverage=coverage,
                              status=outstanding.status,
                              sections=tuple(_freshness_roles(kind, selected_sections)))

        request_id = new_id(REQUEST)
        insert_request = text("""
            INSERT INTO scrape_request
                (id, label, url_requested, url_normalised, kind, status, tempo,
                 linkedin_sections, requested_at,
                 max_attempts, freshness_seconds)
            VALUES (:id, :label, :raw, :norm, :kind, 'queued', :tempo,
                    :sections, :t, :max_attempts, :freshness)
        """).bindparams(bindparam("sections", type_=JSONB))
        connection.execute(insert_request, {
               "id": request_id, "raw": url, "norm": normalised,
               "label": request_label, "kind": kind, "tempo": tempo_name,
               "sections": stored_sections,
               "t": datetime.now(UTC), "max_attempts": attempt_ceiling,
               "freshness": seconds})
        record_interest(connection, request_id, api_key_id)

        from ..queue.enqueue import enqueue_dispatch
        enqueue_dispatch(connection)
    return Submission(request_id, True,
                      stale=tuple(answer.stale) if answer else (), coverage=coverage,
                      sections=tuple(_freshness_roles(kind, selected_sections)))


def submit_google(
    query: object,
    *,
    top_n: object = None,
    label: str | None = None,
    priority: object | None = None,
    tempo: object | None = None,
    database: str | None = None,
    freshness_window: object | None = None,
    api_key_id: str | None = None,
    connection: Connection | None = None,
) -> tuple[Submission, str, int]:
    """Submit a query, or answer it from the SERP we already stored.

    Returns the submission alongside the normalised public target, which the
    caller echoes back and cannot re-derive. `api_key_id` is as for `submit`.
    """
    display, identity = normalise_requested_query(query)
    count = normalise_top_n(top_n)
    # The same validator the URL path uses. `_work_google` has always paced
    # these runs from this column; until now nothing could set it, so every
    # query took the default whatever the operator wanted.
    tempo_name = normalise_tempo(tempo)
    request_label = normalise_label(label)
    request_priority = normalise_priority(priority)

    seconds = freshness.parse(freshness_window)
    with (nullcontext(connection) if connection is not None
          else transaction(database)) as connection:
        coverage = None
        if seconds is not None:
            stored = _google_satisfied(
                connection, identity=identity, count=count,
                since=datetime.now(UTC) - timedelta(seconds=seconds))
            if stored is not None:
                return (_satisfied_search(
                    connection, display=display, identity=identity,
                    count=count, label=request_label, tempo=tempo_name,
                    priority=request_priority,
                    seconds=seconds, stored=stored)._replace(
                        status="satisfied", coverage={"freshness_seconds": seconds,
                        "sections": {"serp": {"state": "satisfied"}}}),
                    display, count)
            old = _google_satisfied(connection, identity=identity, count=count,
                                    since=datetime(1970, 1, 1, tzinfo=UTC))
            failure = connection.execute(text("""
                SELECT failure_kind FROM google.search_observation
                WHERE query_normalised=:query AND requested_top_n=:count
                  AND searched_at <= :now
                ORDER BY searched_at DESC, id DESC LIMIT 1
            """), {"query": identity, "count": count, "now": datetime.now(UTC)}).scalar()
            coverage = {"freshness_seconds": seconds, "sections": {
                "serp": {"state": "expired" if old else "failed" if failure else "missing"}}}

        # FOR KEY SHARE, for the reason given in `submit`.
        outstanding = connection.execute(text("""
            SELECT id, priority, status FROM scrape_request
            WHERE query_normalised=:query AND google_top_n=:top_n
              AND status IN ('queued', 'assigned', 'running')
            FOR KEY SHARE
        """), {"query": identity, "top_n": count}).one_or_none()
        if outstanding is not None:
            if request_priority == "high" and outstanding.priority != "high":
                connection.execute(text("""
                    UPDATE scrape_request SET priority='high' WHERE id=:request
                """), {"request": outstanding.id})
            record_interest(connection, outstanding.id, api_key_id)
            return Submission(outstanding.id, False, status=outstanding.status,
                              coverage=coverage), display, count

        request_id = new_id(REQUEST)
        connection.execute(text("""
            INSERT INTO scrape_request
                (id, label, priority, query_requested, query_normalised,
                 google_top_n,
                 kind, status, tempo, requested_at, freshness_seconds)
            VALUES (:id, :label, :priority, :display, :identity, :top_n,
                    'google_search', 'queued', :tempo, :now, :freshness)
        """), {
            "id": request_id,
            "label": request_label,
            "priority": request_priority,
            "display": display,
            "identity": identity,
            "top_n": count,
            "tempo": tempo_name,
            "now": datetime.now(UTC),
            "freshness": seconds,
        })
        record_interest(connection, request_id, api_key_id)

        from ..queue.enqueue import enqueue_dispatch
        enqueue_dispatch(connection)
    return Submission(request_id, True, coverage=coverage), display, count


# What must be fresh for a request of each kind. A generic URL is absent
# because it has no sections: its one section is `page`, which `_freshness_roles`
# returns for any kind not named here, and `resolve.GENERIC` is already the
# name of its kind.
FRESHNESS_SECTIONS = {
    "linkedin_person": (resolve.PERSON, "profile", LINKEDIN_PERSON_SECTIONS),
    "linkedin_company": (resolve.COMPANY, "company", LINKEDIN_COMPANY_SECTIONS),
}


def _freshness_roles(kind: str, selected) -> list[str]:
    """The head document, and every section this request would capture.

    A null selection means the scrape would capture everything, so everything
    must be fresh: a full request cannot be answered by a reading that only
    looked at one section.
    """
    if kind not in FRESHNESS_SECTIONS:
        return ["page"]
    _, head, every = FRESHNESS_SECTIONS[kind]
    return [head, *sorted(selected if selected is not None else every)]


def assess_coverage(connection, *, kind: str, key: str, seconds: int,
                    selected, answer) -> dict:
    """Explain freshness using the same selection rules, without changing them."""
    from .consumer import section_path

    now = datetime.now(UTC)
    historic = _satisfy(connection, kind=kind, key=key,
                        seconds=int((now - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()),
                        selected=selected, now=now)
    latest = {}
    failed_sections = {}
    if kind in FRESHNESS_SECTIONS:
        latest = resolve.walk(connection, kind=FRESHNESS_SECTIONS[kind][0],
                              key=key, at=now, allow_stale=False)
        failed_sections = {name: reading.status for name, reading in latest.items()
                           if reading.grade == resolve.FAILED}
    elif kind == "generic":
        failure = connection.execute(text("""
            SELECT a.cause FROM scrape_attempt a JOIN scrape_request r ON r.id=a.request_id
            WHERE r.url_normalised=:key AND r.kind=:kind AND a.ended_at <= :now
            ORDER BY a.ended_at DESC, a.id DESC LIMIT 1
        """), {"key": key, "kind": kind, "now": now}).scalar()
        if failure:
            failed_sections = {"page": failure}
    sections = {}
    for name in _freshness_roles(kind, selected):
        state = ("satisfied" if name not in answer.stale else
                 "expired" if name not in historic.stale else
                 "failed" if name in failed_sections else "missing")
        sections[section_path(name, kind)] = {"state": state}
        if state == "failed":
            sections[section_path(name, kind)]["status"] = failed_sections[name]
    return {"freshness_seconds": seconds, "sections": sections}


def _satisfy(connection, *, kind: str, key: str, seconds: int,
             selected, now):
    """Whether the stored history already answers this request."""
    resolve_kind = FRESHNESS_SECTIONS.get(kind, (kind,))[0]
    return freshness.satisfies(
        connection, kind=resolve_kind, key=key, freshness_seconds=seconds,
        sections=_freshness_roles(kind, selected), now=now)


def _satisfied_request(connection, *, url, normalised, kind, label, tempo,
                       sections, max_attempts, seconds, answer) -> Submission:
    """Record a request that needed no work, and what answered it.

    A real row rather than an early return with no id: the id is the
    acknowledgement this API promises, and the console reads requests.
    """
    request_id = new_id(REQUEST)
    now = datetime.now(UTC)
    insert = text("""
        INSERT INTO scrape_request
            (id, label, url_requested, url_normalised, kind, status, tempo,
             linkedin_sections, requested_at,
             max_attempts, freshness_seconds, satisfied_at)
        VALUES (:id, :label, :raw, :norm, :kind, 'satisfied', :tempo,
                :sections, :t, :max_attempts, :freshness, :t)
    """).bindparams(bindparam("sections", type_=JSONB))
    connection.execute(insert, {
        "id": request_id, "raw": url, "norm": normalised, "label": label,
        "kind": kind, "tempo": tempo, "sections": sections, "t": now,
        "max_attempts": max_attempts, "freshness": seconds})
    satisfied_by = _record_satisfaction(connection, request_id, answer)
    return Submission(request_id, False, satisfied_by=satisfied_by)


def _record_satisfaction(connection, request_id: str, answer) -> dict:
    """Which stored captures this answer rests on."""
    for section, resolution in answer.by_section.items():
        connection.execute(text("""
            INSERT INTO request_satisfaction
                (request_id, section, capture_id, observed_at)
            VALUES (:r, :section, :c, :t)
        """), {"r": request_id, "section": section,
               "c": resolution.capture_id, "t": resolution.observed_at})
    return {
        # The guarantee actually delivered: every requested unit is at least
        # this recent. Not left to be derived from the map, because this is
        # the number a caller should act on.
        "oldest_observed_at": answer.oldest_observed_at.isoformat(),
        "sections": {section: resolution.capture_id
                  for section, resolution in answer.by_section.items()},
    }


def _google_satisfied(connection, *, identity: str, count: int, since):
    """The newest search for this query and depth since `since`, if any.

    `serp_capture_id` is on the observation already, so this needs no join,
    and a SERP has no sections, so it needs no walk.

    `partial` counts alongside `ok`: the SERP was read and a ranking came
    back, and some destinations failing is not a reason to run the search
    again. Only `failed` means we have no ranking, and that never satisfies.
    """
    return connection.execute(text("""
        SELECT id, searched_at, serp_capture_id
        FROM google.search_observation
        WHERE query_normalised = :q AND requested_top_n = :n
          AND searched_at >= :since AND outcome IN ('ok', 'partial')
          AND failure_kind IS NULL
        ORDER BY searched_at DESC, id DESC LIMIT 1
    """), {"q": identity, "n": count, "since": since}).one_or_none()


def _satisfied_search(connection, *, display, identity, count, label, tempo,
                      priority, seconds, stored) -> Submission:
    """Record a search answered from a SERP we already hold."""
    request_id = new_id(REQUEST)
    now = datetime.now(UTC)
    connection.execute(text("""
        INSERT INTO scrape_request
            (id, label, priority, query_requested, query_normalised, google_top_n,
             kind, status, tempo, requested_at,
             freshness_seconds, satisfied_at)
        VALUES (:id, :label, :priority, :display, :identity, :top_n,
                'google_search',
                'satisfied', :tempo, :now, :freshness, :now)
    """), {"id": request_id, "label": label, "priority": priority,
           "display": display,
           "identity": identity, "top_n": count, "tempo": tempo,
           "now": now, "freshness": seconds})
    connection.execute(text("""
        INSERT INTO request_satisfaction
            (request_id, section, capture_id, observed_at)
        VALUES (:r, 'serp', :c, :t)
    """), {"r": request_id, "c": stored.serp_capture_id,
           "t": stored.searched_at})
    return Submission(request_id, False, satisfied_by={
        "oldest_observed_at": stored.searched_at.isoformat(),
        "sections": {"serp": stored.serp_capture_id},
    })
