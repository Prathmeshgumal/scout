"""Transport-neutral Google search document assembly."""

import pytest
from datetime import UTC, datetime

from career_lens.capture.writer import Document

from career_lens.google import contract
from career_lens.google.execution import build_search_document, result_part

SERP_URL = "https://www.google.com/search?q=example"


def serp(status="succeeded"):
    return {
        "capture_id": "cap-serp",
        "status": status,
        "url_final": SERP_URL,
        "title": "example - Google Search",
        "text": "results",
        "text_characters": 7,
        "completeness": {"scroll_stopped": "not_scrolled", "scroll_growing": False},
    }


def result_entry(rank, *, selected=True, reason="top_n", capture_status="succeeded"):
    capture = None
    if selected:
        capture = {
            "capture_id": f"cap-{rank}",
            "mode": "generic",
            "status": capture_status,
            "url_requested": f"https://a{rank}.example/x",
            "url_final": f"https://a{rank}.example/x",
            "host": f"a{rank}.example",
            "http_status": 200,
            "title": "t",
            "captured_at": "2026-09-07T04:00:00+00:00",
            "text": "body",
            "text_characters": 4,
            "completeness": {"scroll_stopped": "stopped", "scroll_growing": False},
            "error": (
                None if capture_status == "succeeded"
                else {"kind": "timeout", "detail": "destination timed out"}
            ),
        }
    return {
        "rank": rank,
        "url": f"https://a{rank}.example/x",
        "normalised_url": f"https://a{rank}.example/x",
        "host": f"a{rank}.example",
        "title": "t",
        "snippet": "s",
        "selection": {"selected": selected, "reason": reason},
        "capture": capture,
    }


def envelope(**overrides):
    fields = {
        "request_id": "rq-1",
        "requested_at": "2026-09-07T03:59:00+00:00",
        "started_at": "2026-09-07T03:59:30+00:00",
        "finished_at": "2026-09-07T04:00:30+00:00",
        "query": "example",
        "normalised_query": "example",
        "requested_top_n": 1,
        "google_host": "www.google.com",
        "serp_url": SERP_URL,
        "searched_at": "2026-09-07T04:00:00+00:00",
        "browser_locale": "en-GB",
        "browser_timezone": "Asia/Kolkata",
        "serp": serp(),
        "results": [result_entry(1)],
        "failure": None,
    }
    fields.update(overrides)
    return build_search_document(**fields)


def test_a_clean_run_is_ok_and_validates():
    document = envelope()
    assert document["outcome"] == "ok"
    assert document["request"]["status"] == "succeeded"
    assert document["search"]["results_examined"] == 1
    contract.validate_document(document)


def test_a_failure_is_partial_and_marks_the_request_failed():
    document = envelope(
        failure={"kind": "blocked", "detail": "challenge"},
        results=[], requested_top_n=0)
    assert document["outcome"] == "partial"
    assert document["request"]["status"] == "failed"
    contract.validate_document(document)


def test_one_failed_capture_makes_the_run_partial():
    document = envelope(results=[result_entry(1, capture_status="failed")])
    assert document["outcome"] == "partial"
    assert document["request"]["status"] == "succeeded"
    contract.validate_document(document)


@pytest.mark.parametrize(("http_status", "head", "kind"), [
    (404, "Page not found", "not_found"),
    (200, "verify you are human", "blocked"),
    (500, "Server error", "browser_error"),
])
def test_failed_destinations_keep_their_capture_evidence(http_status, head, kind):
    observation = {**result_entry(1), "destination_signs": {
        "url": "https://a1.example/x", "head": head, "textLength": len(head),
    }}
    captured = Document(
        section="result-001", url_requested=observation["url"],
        url_final=observation["url"], host="a1.example", http_status=http_status,
        captured_at=datetime.now(UTC), title=head, html=b"<main>evidence</main>",
        text=head,
    )
    capture = result_part(observation, document=captured, capture_id="cap-1")["capture"]
    assert capture["status"] == "failed"
    assert capture["error"]["kind"] == kind
    assert capture["capture_id"] == "cap-1"
    assert capture["text"] == head


def test_a_partial_serp_makes_the_run_partial():
    # The contract will not accept `partial` unless the summary shows the
    # incompleteness, so a partial SERP is paired with a partial capture here.
    document = envelope(
        serp=serp(status="partial"),
        results=[result_entry(1, capture_status="partial")])
    assert document["outcome"] == "partial"
    contract.validate_document(document)


def test_the_summary_counts_what_the_results_say():
    document = envelope(
        requested_top_n=2,
        results=[result_entry(1), result_entry(2, selected=False,
                                               reason="beyond_top_n")])
    assert document["summary"]["examined"] == 2
    assert document["summary"]["selected"] == 1
    assert document["summary"]["captured"] == 1
    contract.validate_document(document)


def test_a_browser_environment_the_runtime_cannot_read_is_null_not_missing():
    # The contract allows null here; it does not allow absent fields.
    document = envelope(browser_locale=None, browser_timezone=None)
    assert document["search"]["browser_locale"] is None
    assert document["search"]["browser_timezone"] is None
    contract.validate_document(document)


def test_a_document_that_contradicts_itself_is_rejected():
    document = envelope(requested_top_n=0)
    with pytest.raises(Exception):
        contract.validate_document(document)
