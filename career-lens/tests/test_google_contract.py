from __future__ import annotations

import json
from pathlib import Path

import pytest

from career_lens.google.contract import (
    InvalidGoogleDocument,
    InvalidGoogleRequest,
    failed_envelope,
    normalise_query,
    normalise_top_n,
    summary,
    validate_document,
)


FIXTURES = Path(__file__).parent / "fixtures" / "google"


@pytest.fixture(params=("success", "partial", "failed"))
def golden_document(request) -> dict:
    return json.loads((FIXTURES / f"{request.param}.json").read_text())


@pytest.fixture
def success_document() -> dict:
    return json.loads((FIXTURES / "success.json").read_text())


def test_query_normalization_preserves_display_case_and_casefolds_identity():
    assert normalise_query("  Straße\u00a0 Research  ") == (
        "Straße Research", "strasse research")


@pytest.mark.parametrize("value", ["", "   ", 7, None, "x" * 501])
def test_unusable_queries_are_rejected(value):
    with pytest.raises(InvalidGoogleRequest):
        normalise_query(value)


@pytest.mark.parametrize("value,want", [(None, 5), (0, 0), (5, 5)])
def test_top_n_boundaries(value, want):
    assert normalise_top_n(value) == want


@pytest.mark.parametrize("value", [True, False, -1, 6, 21, 4.5, "5"])
def test_top_n_rejects_booleans_and_non_integer_values(value):
    with pytest.raises(InvalidGoogleRequest):
        normalise_top_n(value)


def test_nonstandard_linkedin_status_does_not_invalidate_the_document(success_document):
    capture = next(item["capture"] for item in success_document["results"] if item["capture"])
    capture["http_status"] = 999
    validate_document(success_document)


def test_golden_documents_satisfy_the_public_contract(golden_document):
    assert validate_document(golden_document) is golden_document


def test_a_document_written_under_the_old_ceiling_is_still_readable(
        success_document):
    """Requests are capped at five now. Stored documents are re-validated on
    every read, so holding them to today's ceiling would make every search run
    under the old one unreadable -- which is not what lowering a limit means."""
    success_document["search"]["requested_top_n"] = 12

    assert validate_document(success_document) is success_document


def test_a_document_beyond_even_the_old_ceiling_is_refused(success_document):
    success_document["search"]["requested_top_n"] = 21

    with pytest.raises(InvalidGoogleDocument):
        validate_document(success_document)


def test_summary_is_derived_from_result_entries(success_document):
    assert summary(success_document["results"]) == {
        "examined": 2,
        "selected": 1,
        "captured": 1,
        "partial": 0,
        "failed": 0,
        "skipped": 1,
    }


def test_summary_must_match_result_entries(success_document):
    success_document["summary"]["captured"] += 1
    with pytest.raises(InvalidGoogleDocument, match="summary"):
        validate_document(success_document)


@pytest.mark.parametrize("key,value", [
    ("html", "<html>"),
    ("markdown", "# heading"),
    ("screenshots", []),
    ("screenshot_url", "https://storage.test/image.png"),
    ("base64", "aW1hZ2U="),
])
def test_document_never_accepts_embedded_artifact_payloads(
        success_document, key, value):
    success_document["results"][0]["capture"][key] = value
    with pytest.raises(InvalidGoogleDocument, match=key):
        validate_document(success_document)


def test_top_level_shape_is_closed(success_document):
    success_document["debug"] = {}
    with pytest.raises(InvalidGoogleDocument, match="top-level"):
        validate_document(success_document)


def test_every_examined_result_is_unique_and_ranked(success_document):
    success_document["results"][1]["normalised_url"] = (
        success_document["results"][0]["normalised_url"])
    with pytest.raises(InvalidGoogleDocument, match="unique"):
        validate_document(success_document)


def test_result_ranks_are_dense_and_one_based(success_document):
    success_document["results"][1]["rank"] = 3
    with pytest.raises(InvalidGoogleDocument, match="rank"):
        validate_document(success_document)


def test_results_accept_only_http_destinations(success_document):
    success_document["results"][0]["url"] = "javascript:alert(1)"
    with pytest.raises(InvalidGoogleDocument, match="HTTP"):
        validate_document(success_document)


def test_selection_reason_and_selected_flag_must_agree(success_document):
    success_document["results"][0]["selection"]["selected"] = False
    with pytest.raises(InvalidGoogleDocument, match="selection"):
        validate_document(success_document)


def test_unselected_result_cannot_have_a_capture(success_document):
    success_document["results"][1]["capture"] = dict(
        success_document["results"][0]["capture"])
    with pytest.raises(InvalidGoogleDocument, match="unselected"):
        validate_document(success_document)


def test_selected_result_requires_a_capture_envelope(success_document):
    success_document["results"][0]["capture"] = None
    with pytest.raises(InvalidGoogleDocument, match="selected"):
        validate_document(success_document)


def test_successful_capture_requires_a_durable_capture_id(success_document):
    success_document["results"][0]["capture"]["capture_id"] = None
    with pytest.raises(InvalidGoogleDocument, match="capture_id"):
        validate_document(success_document)


def test_failed_capture_uses_the_closed_failure_vocabulary():
    document = json.loads((FIXTURES / "partial.json").read_text())
    document["results"][1]["capture"]["error"]["kind"] = "surprise"
    with pytest.raises(InvalidGoogleDocument, match="failure kind"):
        validate_document(document)


def test_failed_capture_may_retain_durable_http_evidence(success_document):
    capture = success_document["results"][0]["capture"]
    capture["status"] = "failed"
    capture["http_status"] = 403
    capture["error"] = {
        "kind": "blocked",
        "detail": "destination returned HTTP 403",
    }
    success_document["outcome"] = "partial"
    success_document["summary"]["captured"] = 0
    success_document["summary"]["failed"] = 1

    assert validate_document(success_document) is success_document
    assert capture["capture_id"] is not None
    assert capture["text"]


def test_text_character_counts_are_exact(success_document):
    success_document["serp"]["text_characters"] += 1
    with pytest.raises(InvalidGoogleDocument, match="text_characters"):
        validate_document(success_document)


def test_serp_is_explicitly_never_scrolled(success_document):
    success_document["serp"]["completeness"]["scroll_stopped"] = "bottom"
    with pytest.raises(InvalidGoogleDocument, match="not_scrolled"):
        validate_document(success_document)


def test_destination_completeness_reports_a_boolean_growth_flag(
        success_document):
    success_document["results"][0]["capture"]["completeness"][
        "scroll_growing"] = 0
    with pytest.raises(InvalidGoogleDocument, match="scroll_growing"):
        validate_document(success_document)


def test_ok_outcome_cannot_hide_result_failures(success_document):
    success_document["outcome"] = "failed"
    with pytest.raises(InvalidGoogleDocument, match="outcome"):
        validate_document(success_document)


def test_failed_envelope_is_a_valid_terminal_document():
    document = failed_envelope({
        "request_id": "rq-before-evidence",
        "status": "failed",
        "requested_at": "2026-08-17T13:00:00Z",
        "started_at": "2026-08-17T13:01:00Z",
        "finished_at": "2026-08-17T13:01:10Z",
        "query": "Enterprise AI",
        "normalised_query": "enterprise ai",
        "top_n": 3,
        "failure_kind": "browser_error",
        "failure_detail": "Browser exited before a readable page was captured.",
    })

    assert validate_document(document) is document
    assert document["outcome"] == "failed"
    assert document["serp"] is None
    assert document["results"] == []
    assert document["failure"] == {
        "kind": "browser_error",
        "detail": "Browser exited before a readable page was captured.",
    }
