"""The public wire contract is not the operator/storage contract."""

from copy import deepcopy
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from auth_support import operator_client
from career_lens.api import consumer, v1
from career_lens.api.main import create_app
from career_lens.api.requests import Submission
from career_lens.store import db

NOW = datetime(2026, 9, 21, tzinfo=UTC)


def database_override(app_url):
    def database():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    return database


def assert_sorted_fields(value):
    if isinstance(value, dict):
        assert list(value) == sorted(value)
        for item in value.values():
            assert_sorted_fields(item)
    elif isinstance(value, list):
        for item in value:
            assert_sorted_fields(item)


def test_metadata_omits_unused_fields_without_losing_false_or_zero():
    assert consumer.SectionMeta(status="not_captured").model_dump() == {
        "status": "not_captured", "observed_at": None,
    }
    meta = consumer.SectionMeta(
        status="parsed", reached_end=False, captured_pages=0,
        fallback={"reason": "stale"}, diagnostics={"coverage": "partial"},
    ).model_dump()
    assert meta["reached_end"] is False
    assert meta["captured_pages"] == 0
    assert meta["fallback"] == {"reason": "stale"}
    assert meta["diagnostics"] == {"coverage": "partial"}
    assert "source_url" not in meta


def test_query_sorting_preserves_result_order_and_null_domain_fields():
    result = consumer.GoogleQueryResponse(
        query="example", top_n=2, as_of=NOW.isoformat(), observed_at=NOW.isoformat(),
        results=[{"title": "Z", "rank": 1, "snippet": None}, {"title": "A", "rank": 2}],
    ).model_dump()
    assert_sorted_fields(result)
    assert [item["title"] for item in result["results"]] == ["Z", "A"]
    assert result["results"][0]["snippet"] is None


def assert_mirrored(data, meta):
    assert set(data) == set(meta)
    for key, value in meta.items():
        if "status" not in value:
            assert_mirrored(data[key], value)


def strip_capture_ids(meta):
    for value in meta.values():
        if "status" in value:
            value.pop("capture_id")
        else:
            strip_capture_ids(value)


def test_entity_metadata_is_complete_without_mutating_operator_payload():
    body = {
        "entity": {"kind": "linkedin_company", "url": "https://example.test"},
        "observed_at": NOW.isoformat(),
        "images": {},
        "capture_id": "cp-private",
        "request_id": "rq-private",
        "extractor_version": "7.0",
        "meta": {
            "company": {"observed_at": NOW.isoformat(), "capture_id": "cp-private"}
        },
        "data": {
            "name": "Example",
            "jobs": {"status": "not_captured", "data": None},
            "events": {"status": "not_available", "data": None},
        },
    }
    before = deepcopy(body)
    result = consumer.entity_response(body, cutoff=NOW)
    assert result["schema_version"] == "2.0"
    assert result["data"]["profile"] == {"name": "Example"}
    assert result["meta"]["profile"]["observed_at"] == NOW.isoformat()
    assert_mirrored(result["data"], result["meta"])
    assert result["meta"]["jobs"]["status"] == "not_captured"
    assert result["meta"]["events"]["status"] == "not_available"
    assert "status" not in result["data"]["jobs"]
    assert not ({"capture_id", "request_id", "extractor_version"} & result.keys())
    assert body == before


def test_nested_person_sections_use_public_paths():
    body = {
        "entity": {"kind": "linkedin_profile"},
        "observed_at": NOW.isoformat(),
        "images": {},
        "meta": {
            "activity-posts": {"status": "parsed", "observed_at": NOW.isoformat()}
        },
        "data": {
            "activity": {"posts": {"status": "parsed", "entries": []}},
            "recommendations": {"received": {"status": "not_captured", "entries": []}},
        },
    }
    result = consumer.entity_response(body, cutoff=NOW)
    assert result["meta"]["activity"]["posts"]["observed_at"] == NOW.isoformat()
    assert result["meta"]["recommendations"]["received"]["status"] == "not_captured"
    assert_mirrored(result["data"], result["meta"])


def test_interest_category_paths_are_mirrored_including_legacy_spellings():
    body = {
        "entity": {"kind": "linkedin_profile"},
        "observed_at": NOW.isoformat(),
        "images": {},
        "meta": {"interests": {"status": "parsed"}},
        "data": {
            "name": "Example",
            "interests": {
                "status": "parsed",
                "captured_categories": ["top-voices"],
                "categories": {"top-voices": {"status": "parsed", "entries": []}},
            },
        },
    }
    before = deepcopy(body)
    result = consumer.entity_response(body, cutoff=NOW)
    assert_mirrored(result["data"], result["meta"])
    assert "summary" not in result["data"]["interests"]
    assert "categories" not in result["meta"]["interests"]
    assert result["meta"]["interests"]["top_voices"]["status"] == "parsed"
    assert result["data"]["interests"]["companies"] is None
    assert body == before


def test_reused_submission_reports_real_state():
    import json

    response = v1._submitted(
        Submission("rq-example", False, status="running"),
        {"kind": "linkedin_person", "url": "https://example.test"},
    )
    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["status"] == "running"
    assert "deduplicated" not in response.body.decode()
    assert response.headers["Location"] == "/v1/requests/rq-example"


def test_public_errors_versioned_and_capture_parameter_rejected(app_url):
    app = create_app()
    app.dependency_overrides[v1.database] = database_override(app_url)
    client = operator_client(app, app_url)
    for url in ("/v1/linkedin-company?url=x&capture=cp-x", "/v1/linkedin-company"):
        response = client.get(url)
        assert response.status_code in {400, 422}
        assert response.json()["schema_version"] == "1.0"


def test_request_response_does_not_expose_execution_details():
    body = {
        "request_id": "rq-x",
        "status": "failed",
        "target": {},
        "kind": "generic",
        "attempts": [{"browser_profile": "private"}],
        "failure": {"kind": "browser_error", "detail": "/private/profile/email"},
    }
    result = consumer.request_response(body)
    assert "attempts" not in result
    assert result["failure"]["kind"] == "browser_error"
    assert "/private" not in str(result)


def test_generic_url_history_does_not_leak_nested_capture_ids():
    result = consumer.detailed_result(
        {
            "url": "https://example.test",
            "result": {
                "capture": {
                    "capture_id": "cp-private",
                    "text": "Useful content",
                    "error": None,
                }
            },
        }
    )
    assert result["result"]["capture"] == {"text": "Useful content", "error": None}


def test_unexpected_application_error_is_safe_and_versioned():
    app = create_app()

    @app.get("/v1/test-failure")
    def fail():
        raise RuntimeError("private database connection string")

    response = TestClient(app, raise_server_exceptions=False).get("/v1/test-failure")
    assert response.status_code == 500
    assert response.json() == {
        "schema_version": "1.0",
        "detail": "Internal service error",
    }
