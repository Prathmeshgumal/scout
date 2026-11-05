from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from career_lens.capture.writer import BlobMissing
from career_lens.google.store import (
    EXTRACTOR,
    GoogleDocumentCorrupt,
    GoogleDocumentMissing,
    load_extraction,
    store_extraction,
)
from career_lens.store.blobs import sha256
from career_lens.store.ids import CAPTURE, EXTRACTION, REQUEST, new_id


NOW = datetime(2026, 8, 17, 10, tzinfo=UTC)
GOLDEN = Path(__file__).parent / "fixtures" / "google" / "success.json"


class FakeStore:
    def __init__(self, *, head_size: int | None | object = ...):
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.head_size = head_size

    def put(self, key, data, content_type):
        self.calls.append(("put", key))
        self.objects[key] = data
        self.content_types[key] = content_type

    def head(self, key):
        self.calls.append(("head", key))
        if self.head_size is ...:
            return len(self.objects[key]) if key in self.objects else None
        return self.head_size

    def get(self, key):
        self.calls.append(("get", key))
        return self.objects[key]

    def url(self, key, expires=3600):
        return f"https://objects.test/{key}?expires={expires}"


@pytest.fixture
def document() -> dict:
    return json.loads(GOLDEN.read_text())


@pytest.fixture
def google_facts(app, document):
    request_id = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, query_requested, query_normalised, google_top_n,
             kind, status, requested_at)
        VALUES (:request, 'Enterprise AI', 'enterprise ai', 1,
                'google_search', 'running', :now)
    """), {"request": request_id, "now": NOW})
    attempt_id = app.execute(text("""
        INSERT INTO scrape_attempt (request_id, attempt_no, started_at)
        VALUES (:request, 1, :now) RETURNING id
    """), {"request": request_id, "now": NOW}).scalar_one()

    captures = []
    for section, url, host in (
        ("serp", document["serp"]["url_final"], "www.google.co.in"),
        ("result-001", "https://example.com/article", "example.com"),
    ):
        capture_id = new_id(CAPTURE)
        app.execute(text("""
            INSERT INTO capture
                (id, request_id, attempt_id, section, url_requested, url_final,
                 host, captured_at)
            VALUES (:capture, :request, :attempt, :section, :url, :url,
                    :host, :now)
        """), {
            "capture": capture_id, "request": request_id,
            "attempt": attempt_id, "section": section, "url": url,
            "host": host, "now": NOW,
        })
        captures.append((section, capture_id))
    document["request"]["request_id"] = request_id
    document["serp"]["capture_id"] = captures[0][1]
    document["results"][0]["capture"]["capture_id"] = captures[1][1]
    return request_id, attempt_id, captures


def test_store_extraction_writes_stable_verified_json(
        app, app_url, document, google_facts):
    request_id, attempt_id, captures = google_facts
    store = FakeStore()

    extraction_id = store_extraction(
        app_url, request_id=request_id, attempt_id=attempt_id,
        capture_ids=captures, document=document, store=store)

    row = app.execute(text("""
        SELECT extractor, extractor_version, outcome, document_key,
               document_sha256, document_bytes
        FROM extraction WHERE id=:id
    """), {"id": extraction_id}).one()
    payload = store.objects[row.document_key]
    assert row.extractor == "google_search_json"
    assert row.extractor_version == "1.0"
    assert row.outcome == "ok"
    assert row.document_key == f"extractions/{extraction_id}/document.json"
    assert row.document_sha256 == sha256(payload)
    assert row.document_bytes == len(payload)
    assert store.content_types[row.document_key] == "application/json"
    assert payload == json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    assert store.calls[:2] == [
        ("put", row.document_key), ("head", row.document_key),
    ]
    assert app.execute(text("""
        SELECT section, capture_id FROM extraction_input
        WHERE extraction_id=:id ORDER BY section
    """), {"id": extraction_id}).all() == sorted(captures)
    observation = app.execute(text("""
        SELECT extraction_id, request_id FROM google.search_observation
        WHERE extraction_id=:id
    """), {"id": extraction_id}).one()
    assert observation == (extraction_id, request_id)
    assert app.execute(text("""
        SELECT count(*) FROM google.search_result
        WHERE observation_id=(
            SELECT id FROM google.search_observation WHERE extraction_id=:id)
    """), {"id": extraction_id}).scalar_one() == len(document["results"])


def test_projection_failure_rolls_back_all_database_rows_but_keeps_json(
        app, app_url, document, google_facts, monkeypatch):
    request_id, attempt_id, captures = google_facts
    store = FakeStore()

    def fail_projection(*args, **kwargs):
        raise RuntimeError("projection stopped")

    monkeypatch.setattr("career_lens.google.store.project", fail_projection)
    with pytest.raises(RuntimeError, match="projection stopped"):
        store_extraction(
            app_url, request_id=request_id, attempt_id=attempt_id,
            capture_ids=captures, document=document, store=store)

    assert len(store.objects) == 1
    assert app.execute(text("""
        SELECT count(*) FROM extraction WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0
    assert app.execute(text("""
        SELECT count(*) FROM google.search_observation WHERE request_id=:request
    """), {"request": request_id}).scalar_one() == 0
    assert app.execute(text(
        "SELECT count(*) FROM google.search_result")).scalar_one() == 0


def test_validation_happens_before_upload(app_url, document, google_facts):
    request_id, attempt_id, captures = google_facts
    document["summary"]["captured"] = 99
    store = FakeStore()

    with pytest.raises(ValueError, match="summary"):
        store_extraction(
            app_url, request_id=request_id, attempt_id=attempt_id,
            capture_ids=captures, document=document, store=store)
    assert store.calls == []


@pytest.mark.parametrize("head_size", [None, 1])
def test_unverified_object_creates_no_extraction(
        app, app_url, document, google_facts, head_size):
    request_id, attempt_id, captures = google_facts
    with pytest.raises(BlobMissing):
        store_extraction(
            app_url, request_id=request_id, attempt_id=attempt_id,
            capture_ids=captures, document=document,
            store=FakeStore(head_size=head_size))
    assert app.execute(text(
        "SELECT count(*) FROM extraction WHERE request_id=:request"),
        {"request": request_id}).scalar_one() == 0


def test_load_extraction_verifies_and_validates_document(
        app, app_url, document, google_facts):
    request_id, attempt_id, captures = google_facts
    store = FakeStore()
    store_extraction(
        app_url, request_id=request_id, attempt_id=attempt_id,
        capture_ids=captures, document=document, store=store)

    loaded = load_extraction(app, request_id, store)
    assert loaded == document


@pytest.mark.parametrize("mutation", ["size", "digest", "json"])
def test_load_rejects_corrupt_objects(
        app, app_url, document, google_facts, mutation):
    request_id, attempt_id, captures = google_facts
    store = FakeStore()
    extraction_id = store_extraction(
        app_url, request_id=request_id, attempt_id=attempt_id,
        capture_ids=captures, document=document, store=store)
    key = app.execute(text(
        "SELECT document_key FROM extraction WHERE id=:id"),
        {"id": extraction_id}).scalar_one()
    if mutation in {"size", "digest"}:
        store.objects[key] += b"x"
    else:
        bad = b"{}"
        other_id = new_id(EXTRACTION)
        other_key = f"extractions/{other_id}/document.json"
        store.objects[other_key] = bad
        newest = app.execute(text("""
            SELECT extracted_at FROM extraction WHERE id=:id
        """), {"id": extraction_id}).scalar_one() + timedelta(microseconds=1)
        app.execute(text("""
            INSERT INTO extraction
                (id, request_id, attempt_id, extractor, extractor_version,
                 extracted_at, outcome, document_key, document_sha256,
                 document_bytes)
            VALUES (:id, :request, :attempt, :extractor, '1.0', :now, 'ok',
                    :key, :digest, :size)
        """), {
            "id": other_id, "request": request_id, "attempt": attempt_id,
            "extractor": EXTRACTOR, "now": newest,
            "key": other_key, "digest": sha256(bad), "size": len(bad),
        })
    with pytest.raises(GoogleDocumentCorrupt):
        load_extraction(app, request_id, store)


def test_load_ignores_documents_from_other_extractors(
        app, google_facts, document):
    request_id, attempt_id, _ = google_facts
    payload = json.dumps(document).encode()
    key = "extractions/ex-other/document.json"
    app.execute(text("""
        INSERT INTO extraction
            (id, request_id, attempt_id, extractor, extractor_version,
             extracted_at, outcome, document_key, document_sha256,
             document_bytes)
        VALUES ('ex-other', :request, :attempt, 'other', '1.0', :now, 'ok',
                :key, :digest, :size)
    """), {
        "request": request_id, "attempt": attempt_id, "now": NOW,
        "key": key, "digest": sha256(payload), "size": len(payload),
    })
    store = FakeStore()
    store.objects[key] = payload

    with pytest.raises(GoogleDocumentMissing):
        load_extraction(app, request_id, store)
