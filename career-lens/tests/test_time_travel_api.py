"""Reading an entity as it stood at an instant, through the HTTP API."""

from __future__ import annotations

import json

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from career_lens.api.main import create_app
from auth_support import operator_client
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id

URL = "https://www.linkedin.com/in/jane/"
NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(app_url, monkeypatch):
    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    return operator_client(create_app(), app_url)


def _reading(app, *, at, sections, employer=None, url=URL, causes=None,
             contact_info=None, open_to_work=None):
    """One person observation read at `at`. `sections` is {name: status}."""
    rid, eid, cid = new_id(REQUEST), new_id(EXTRACTION), new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'succeeded', :t)
    """), {"r": rid, "u": url, "t": at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_profile_json', '1.7', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": at})
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at)
        VALUES (:c, :r, 'profile', :u, 'linkedin.com', :t)
    """), {"c": cid, "r": rid, "u": url, "t": at})
    app.execute(text("""
        INSERT INTO extraction_input (extraction_id, capture_id, section)
        VALUES (:e, :c, 'profile')
    """), {"e": eid, "c": cid})
    observation = app.execute(text("""
        INSERT INTO linkedin.person_observation
            (extraction_id, handle, profile_url, name, observed_at,
             capture_id, causes, contact_info, open_to_work)
        VALUES (:e, 'jane', :u, :name, :t, :c, CAST(:causes AS jsonb),
                CAST(:contact_info AS jsonb), CAST(:open_to_work AS jsonb))
        RETURNING id
    """), {"e": eid, "u": url, "t": at, "c": cid,
           "causes": json.dumps(causes) if causes is not None else None,
           "contact_info": (json.dumps(contact_info)
                            if contact_info is not None else None),
           "open_to_work": (json.dumps(open_to_work)
                            if open_to_work is not None else None),
           "name": f"Jane at {at:%Y-%m-%d}"}).scalar_one()
    for section, status in sections.items():
        app.execute(text("""
            INSERT INTO linkedin.person_section_status
                (observation_id, section, status, observed_at, capture_id)
            VALUES (:o, :s, :status, :t, :c)
        """), {"o": observation, "s": section, "status": status,
               "t": at, "c": cid})
    if employer is not None:
        app.execute(text("""
            INSERT INTO linkedin.person_employer (observation_id, ordinal, name)
            VALUES (:o, 0, :name)
        """), {"o": observation, "name": employer})
    return observation, cid


def _employers(body) -> list[str]:
    return [entry["company"] for entry in body["data"]["experience"]["entries"]]


def test_a_selective_scrape_does_not_erase_the_rest_of_the_profile(app, client):
    """The live defect: serving the newest observation whole loses every
    section that scrape did not request."""
    _reading(app, at=NOW - timedelta(days=9),
             sections={"experience": "parsed", "skills": "parsed"},
             employer="Acme")
    _reading(app, at=NOW - timedelta(hours=2),
             sections={"experience": "not_requested", "skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()

    assert _employers(body) == ["Acme"]


def test_at_returns_the_profile_as_it_stood_then(app, client):
    _reading(app, at=datetime(2026, 3, 1, tzinfo=UTC),
             sections={"experience": "parsed"}, employer="Acme")
    _reading(app, at=datetime(2026, 5, 1, tzinfo=UTC),
             sections={"experience": "parsed"}, employer="Globex")

    body = client.get("/v1/linkedin-individual", params={
        "url": URL, "at": "2026-04-01T00:00:00Z"}).json()

    assert _employers(body) == ["Acme"]
    assert body["as_of"].startswith("2026-04-01")


def test_at_before_anything_was_observed_is_a_404(app, client):
    _reading(app, at=NOW, sections={"skills": "parsed"})
    response = client.get("/v1/linkedin-individual", params={
        "url": URL, "at": "2020-01-01T00:00:00Z"})
    assert response.status_code == 404


def test_at_in_the_future_is_clamped_rather_than_refused(app, client):
    """A client's clock skew is not a 400: no belief exists after now."""
    _reading(app, at=NOW, sections={"skills": "parsed"})

    future = client.get("/v1/linkedin-individual", params={
        "url": URL, "at": "2099-01-01T00:00:00Z"})
    plain = client.get("/v1/linkedin-individual", params={"url": URL})

    assert future.status_code == 200
    assert future.json()["data"] == plain.json()["data"]


def test_a_drifted_section_no_longer_makes_the_whole_profile_unreadable(
        app, client):
    """One unreadable section must not put twenty readable ones out of reach."""
    _reading(app, at=NOW - timedelta(days=6),
             sections={"experience": "parsed"}, employer="Acme")
    _reading(app, at=NOW - timedelta(hours=8),
             sections={"experience": "drift", "skills": "parsed"})

    lenient = client.get("/v1/linkedin-individual",
                         params={"url": URL}).json()
    assert lenient["meta"]["skills"]["status"] == "parsed"
    assert _employers(lenient) == ["Acme"]
    # It walked back to a good reading, so that is what the status says.
    assert lenient["meta"]["experience"]["status"] == "parsed"

    strict = client.get("/v1/linkedin-individual", params={
        "url": URL, "allow_stale": "false"}).json()
    assert strict["data"]["experience"]["entries"] == []
    assert strict["meta"]["experience"]["status"] == "drift"
    # The rest of the profile is unaffected in both modes. This is the
    # assertion that fails if the whole-response 409 ever comes back.
    assert strict["meta"]["skills"]["status"] == "parsed"


def test_a_repeated_section_never_mixes_two_readings(app, client):
    """March's jobs beside June's would invent a history that never existed."""
    _reading(app, at=NOW - timedelta(days=30),
             sections={"experience": "parsed"}, employer="Acme")
    _reading(app, at=NOW - timedelta(days=1),
             sections={"experience": "parsed"}, employer="Globex")

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()

    assert _employers(body) == ["Globex"]


def test_a_section_stored_on_the_observation_row_walks_like_the_rest(
        app, client):
    """`causes` and `about_this_member` are columns rather than child tables.

    Reading them off the head observation would pair the older reading's
    status with the newest scrape's empty column -- a section reported as
    `parsed_inline` and empty in the same breath.
    """
    _reading(app, at=NOW - timedelta(days=9),
             sections={"causes": "parsed_inline"},
             causes=["Education", "Science and Technology"])
    _reading(app, at=NOW - timedelta(hours=2), sections={"skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()
    causes = body["data"]["causes"]
    assert body["meta"]["causes"]["status"] == "parsed_inline"
    assert causes["entries"] == ["Education", "Science and Technology"]


COMPANY_URL = "https://www.linkedin.com/company/acme/"


def _company_reading(app, *, at, sections, name="Acme"):
    """One company observation read at `at`. `sections` is {tab: status}."""
    rid, eid, cid = new_id(REQUEST), new_id(EXTRACTION), new_id(CAPTURE)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_company', 'succeeded', :t)
    """), {"r": rid, "u": COMPANY_URL, "t": at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'linkedin_company_json', '1.0', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": at})
    app.execute(text("""
        INSERT INTO capture (id, request_id, section, url_requested, host,
                             captured_at)
        VALUES (:c, :r, 'company', :u, 'linkedin.com', :t)
    """), {"c": cid, "r": rid, "u": COMPANY_URL, "t": at})
    app.execute(text("""
        INSERT INTO extraction_input (extraction_id, capture_id, section)
        VALUES (:e, :c, 'company')
    """), {"e": eid, "c": cid})
    observation = app.execute(text("""
        INSERT INTO linkedin.company_observation
            (extraction_id, slug, company_url, name, observed_at, capture_id)
        VALUES (:e, 'acme', :u, :name, :t, :c) RETURNING id
    """), {"e": eid, "u": COMPANY_URL, "name": name, "t": at,
           "c": cid}).scalar_one()
    for section, status in sections.items():
        app.execute(text("""
            INSERT INTO linkedin.company_section_status
                (observation_id, section, status, observed_at, capture_id)
            VALUES (:o, :s, :status, :t, :c)
        """), {"o": observation, "s": section, "status": status,
               "t": at, "c": cid})
    return observation, cid


def test_a_company_tab_walks_back_to_the_scrape_that_read_it(app, client):
    _company_reading(app, at=NOW - timedelta(days=9),
                     sections={"jobs": "parsed", "about": "parsed"})
    _company_reading(app, at=NOW - timedelta(hours=2),
                     sections={"jobs": "not_requested", "about": "parsed"})

    meta = client.get("/v1/linkedin-company",
                      params={"url": COMPANY_URL}).json()["meta"]

    assert meta["jobs"]["observed_at"].startswith("2026-06-01")
    assert meta["about"]["observed_at"].startswith("2026-06-10")


def test_a_tab_a_company_does_not_have_is_a_reading_not_a_gap(app, client):
    """"This company has no Life tab" is data. The walk stops on it rather
    than stepping back to an older scrape that found something."""
    _company_reading(app, at=NOW - timedelta(days=9),
                     sections={"life": "parsed"})
    _company_reading(app, at=NOW - timedelta(hours=2),
                     sections={"life": "not_available"})

    meta = client.get("/v1/linkedin-company",
                      params={"url": COMPANY_URL}).json()["meta"]

    assert meta["life"]["status"] == "not_available"
    assert meta["life"]["observed_at"].startswith("2026-06-10")


def test_a_company_at_an_instant_is_the_reading_from_then(app, client):
    _company_reading(app, at=datetime(2026, 3, 1, tzinfo=UTC),
                     sections={"about": "parsed"}, name="Acme Ltd")
    _company_reading(app, at=datetime(2026, 5, 1, tzinfo=UTC),
                     sections={"about": "parsed"}, name="Acme Global")

    body = client.get("/v1/linkedin-company", params={
        "url": COMPANY_URL, "at": "2026-04-01T00:00:00Z"}).json()

    assert body["data"]["profile"]["name"] == "Acme Ltd"


def _search(app, *, at, query="acme robotics", top_n=3, first="a.example",
            failure_kind=None):
    """One stored SERP for `query` at `at`, with one result.

    The result is unselected: a selected one has to carry a whole capture,
    and what these tests are about is which SERP answers, not what was done
    with its destinations.
    """
    rid, eid = new_id(REQUEST), new_id(EXTRACTION)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n, kind,
             status, requested_at)
        VALUES (:r, :q, :q, :n, 'google_search', 'succeeded', :t)
    """), {"r": rid, "q": query, "n": top_n, "t": at})
    app.execute(text("""
        INSERT INTO extraction (id, request_id, extractor, extractor_version,
                                extracted_at, outcome, document_key,
                                document_sha256, document_bytes)
        VALUES (:e, :r, 'google_search', '1.0', :t, 'ok', 'k', 'd', 1)
    """), {"e": eid, "r": rid, "t": at})
    observation = app.execute(text("""
        INSERT INTO google.search_observation
            (extraction_id, request_id, query_requested, query_normalised,
             requested_top_n, results_examined, searched_at, outcome,
             failure_kind, failure_detail,
             examined, selected, captured, partial, failed, skipped)
        VALUES (:e, :r, :q, :q, :n, 1, :t, :outcome,
                :failure_kind, :failure_detail,
                1, 1, 1, 0, 0, 0)
        RETURNING id
    """), {
        "e": eid, "r": rid, "q": query, "n": top_n, "t": at,
        "failure_kind": failure_kind,
        "failure_detail": "Google challenge" if failure_kind else None,
        "outcome": "partial" if failure_kind else "ok",
    }).scalar_one()
    app.execute(text("""
        INSERT INTO google.search_result
            (observation_id, rank, url, normalised_url, host, title, snippet,
             selected, selection_reason)
        VALUES (:o, 1, :u, :u, :h, 't', 's', false, 'beyond_top_n')
    """), {"o": observation, "u": f"https://{first}/", "h": first})
    return observation


def test_a_search_reads_back_by_its_query(app, client):
    _search(app, at=NOW - timedelta(hours=4))

    body = client.get("/v1/google-search", params={
        "query": "acme robotics", "top_n": 3}).json()

    assert body["results"][0]["host"] == "a.example"
    assert body["as_of"] is not None


def test_a_search_at_an_instant_returns_that_days_ranking(app, client):
    _search(app, at=datetime(2026, 3, 1, tzinfo=UTC), first="march.example")
    _search(app, at=datetime(2026, 5, 1, tzinfo=UTC), first="may.example")

    body = client.get("/v1/google-search", params={
        "query": "acme robotics", "top_n": 3,
        "at": "2026-04-01T00:00:00Z"}).json()

    assert body["results"][0]["host"] == "march.example"


def test_a_failed_observation_does_not_hide_the_latest_usable_ranking(
        app, client):
    from career_lens.api.requests import _google_satisfied

    usable = _search(
        app, at=NOW - timedelta(hours=2), first="usable.example")
    _search(
        app, at=NOW - timedelta(hours=1), first="blocked.example",
        failure_kind="blocked")

    body = client.get("/v1/google-search", params={
        "query": "acme robotics", "top_n": 3}).json()

    assert body["results"][0]["host"] == "usable.example"
    assert _google_satisfied(
        app, identity="acme robotics", count=3,
        since=NOW - timedelta(hours=3)).id == usable


def test_a_stored_top_three_does_not_answer_a_top_five_question(app, client):
    """top_n is part of the identity, exactly as the dedup index has it."""
    _search(app, at=NOW, top_n=3)

    response = client.get("/v1/google-search", params={
        "query": "acme robotics", "top_n": 5})

    assert response.status_code == 404


def test_a_count_from_before_the_cap_can_still_be_asked_for(app, client):
    """New searches are capped at five. A search that ran at ten before that
    is still stored, and reading it back means naming the count it ran at --
    so the lookup answers "nothing observed", not "that number is illegal"."""
    response = client.get("/v1/google-search", params={
        "query": "acme robotics", "top_n": 10})

    assert response.status_code == 404
    assert "top 10" in response.json()["detail"]


def test_reading_a_search_by_request_id_still_works(app, client):
    """The temporal route must not swallow the route that answers 'what did
    that one run return'."""
    response = client.get("/v1/google-search/rq-does-not-exist")
    assert response.status_code == 404
    assert "top" not in response.json()["detail"]


def _image(app, capture_id: str, kind: str, key: str) -> None:
    app.execute(text("""
        INSERT INTO artifact (capture_id, kind, ordinal, blob_key,
                              content_type, byte_size, sha256)
        VALUES (:c, :k, 0, :key, 'image/jpeg', 1234, :sha)
    """), {"c": capture_id, "k": kind, "key": key, "sha": key})


def test_a_profile_carries_the_pictures_the_reading_saw(app, client):
    """The avatar is stored as an artifact on the capture, not as a column on
    the observation, so nothing above `meta` pointed at it and neither the
    entity page nor the Playground could draw one."""
    _, capture = _reading(app, at=NOW, sections={"profile": "parsed"})
    _image(app, capture, "profile_image", "captures/x/profile-image.jpg")
    _image(app, capture, "cover_image", "captures/x/cover-image.jpg")

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()

    assert body["images"]["profile"], body["images"]
    assert body["images"]["cover"], body["images"]
    # Signed and time-limited, like every other artifact the console serves.
    assert "profile-image.jpg" in body["images"]["profile"]
    assert "cover-image.jpg" in body["images"]["cover"]


def test_a_profile_with_no_stored_pictures_says_so_rather_than_omitting_them(
        app, client):
    """A missing key and a photograph we never took read the same to a client
    that has to check for the field's existence first."""
    _reading(app, at=NOW, sections={"profile": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()

    assert body["images"] == {"profile": None, "cover": None}


def test_the_pictures_come_from_the_reading_being_read_not_the_newest(app):
    """Time travel's whole point: an older instant shows the avatar that
    reading saw, not the one on the profile today."""
    _, old = _reading(app, at=NOW - timedelta(days=30),
                      sections={"profile": "parsed"})
    _, new = _reading(app, at=NOW, sections={"profile": "parsed"})
    _image(app, old, "profile_image", "captures/old/profile-image.jpg")
    _image(app, new, "profile_image", "captures/new/profile-image.jpg")

    from career_lens.api import serialise

    then = serialise.person(app, URL, at=NOW - timedelta(days=15))
    now = serialise.person(app, URL)

    assert "captures/old/profile-image.jpg" in then["images"]["profile"]
    assert "captures/new/profile-image.jpg" in now["images"]["profile"]
def test_contact_info_walks_like_the_other_column_sections(app, client):
    """Stored on the observation row like `causes`, so it must resolve to the
    reading that actually captured it rather than to the newest scrape."""
    _reading(app, at=NOW - timedelta(days=9),
             sections={"contact-info": "parsed"},
             contact_info={"email": "jane@example.com", "websites": [],
                           "phones": [], "im": [], "other": [], "links": [],
                           "profile_url": None, "address": None,
                           "birthday": None, "connected_on": None})
    _reading(app, at=NOW - timedelta(hours=2), sections={"skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()
    contact = body["data"]["contact_info"]
    assert body["meta"]["contact_info"]["status"] == "parsed"
    assert contact["email"] == "jane@example.com"


def test_a_profile_with_no_contact_dialog_reports_only_the_status(app, client):
    """Nothing was read, and the status is what says so. Keys of null around
    it would claim a dialog that was opened and found empty."""
    _reading(app, at=NOW - timedelta(hours=2), sections={"skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()
    assert body["data"]["contact_info"] == {}
    assert body["meta"]["contact_info"]["status"] == "not_captured"


def test_open_to_work_walks_like_the_other_column_sections(app, client):
    _reading(app, at=NOW - timedelta(days=9),
             sections={"open-to-work": "parsed"},
             open_to_work={"job_titles": ["Data Engineer"],
                           "start_date": "Immediately"})
    _reading(app, at=NOW - timedelta(hours=2), sections={"skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()
    looking = body["data"]["open_to_work"]
    assert body["meta"]["open_to_work"]["status"] == "parsed"
    assert looking["job_titles"] == ["Data Engineer"]


def test_a_member_who_is_not_looking_reports_only_the_status(app, client):
    _reading(app, at=NOW - timedelta(hours=2), sections={"skills": "parsed"})

    body = client.get("/v1/linkedin-individual", params={"url": URL}).json()
    assert body["data"]["open_to_work"] == {}
    assert body["meta"]["open_to_work"]["status"] == "not_captured"
