from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from career_lens.capture.writer import Document
from career_lens.generic.contract import InvalidGenericDocument, build, validate


def _document() -> dict:
    return build(Document(
        section="page", url_requested="https://example.com/article",
        url_final=None, host="example.com", http_status=200,
        captured_at=datetime(2026, 8, 20, tzinfo=UTC), title="Article",
        html=b"", text="Rendered text", scroll_stopped="bottom",
        scroll_growing=False,
    ), capture_id="cp-one", normalised_url="https://example.com/article")


def test_builder_produces_a_strict_google_destination_shape():
    document = validate(_document())

    assert document["schema_version"] == "1.0"
    assert document["capture"]["url_final"] == document["url"]
    assert set(document["capture"]) == {
        "capture_id", "mode", "status", "url_requested", "url_final",
        "host", "http_status", "title", "captured_at", "text",
        "text_characters", "completeness", "error",
    }


def test_a_document_written_before_the_version_key_still_reads():
    """Stored documents are re-validated on every read, so requiring the key
    the first version never wrote would make those documents unreadable."""
    document = _document()
    del document["schema_version"]

    assert validate(document) is document


def test_a_document_from_another_schema_is_refused():
    document = _document()
    document["schema_version"] = "2.0"

    with pytest.raises(InvalidGenericDocument):
        validate(document)


def _failed_document() -> dict:
    """What the reader answers for a request that never reached a page."""
    document = _document()
    document["capture"].update(
        capture_id=None, status="failed", url_final=None, captured_at=None,
        text=None, text_characters=0, http_status=None,
        completeness={"scroll_stopped": "not_started", "scroll_growing": False},
        error={"kind": "timeout", "detail": "page did not settle"},
    )
    document["capture"]["title"] = None
    document["title"] = None
    return document


def test_a_failed_capture_is_a_document_rather_than_a_gap():
    """The reader answers a failed request with this shape, so the contract
    has to accept it -- otherwise the API serves documents its own validator
    refuses, and the backfill cannot store what the reader produced."""
    document = validate(_failed_document())

    assert document["capture"]["status"] == "failed"
    assert document["capture"]["capture_id"] is None


@pytest.mark.parametrize("corrupt", [
    lambda value: value["capture"].update(error=None),
    lambda value: value["capture"].update(error={
        "kind": "invented", "detail": "not a known failure"}),
    lambda value: value["capture"].update(text_characters=7),
])
def test_a_failed_capture_still_has_to_say_what_happened(corrupt):
    document = _failed_document()
    corrupt(document)

    with pytest.raises(InvalidGenericDocument):
        validate(document)


@pytest.mark.parametrize("corrupt", [
    lambda value: value.update(rank=2),
    lambda value: value["capture"].update(extra="not canonical"),
    lambda value: value["capture"].update(url_final=None),
    lambda value: value["capture"].update(host="other.example"),
    lambda value: value["capture"].update(
        status="partial", error=None),
])
def test_validator_rejects_structural_and_semantic_corruption(corrupt):
    document = deepcopy(_document())
    corrupt(document)

    with pytest.raises(InvalidGenericDocument):
        validate(document)


def test_markdown_attached_by_the_reader_is_part_of_the_contract():
    """The reader attaches Markdown on request, and what the API serves has to
    satisfy the contract it claims to serve."""
    document = _document()
    document["capture"]["markdown"] = "# Article\n\n[Acme](https://acme.com/)"
    assert validate(document) is document

    document["capture"]["markdown"] = None
    assert validate(document) is document

    document["capture"]["markdown"] = ["not", "text"]
    with pytest.raises(InvalidGenericDocument):
        validate(document)
