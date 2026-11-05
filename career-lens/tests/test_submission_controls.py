"""Consumer submission inputs must not expose operator display/pacing controls."""

import pytest
from auth_support import operator_client

from career_lens.api import requests, v1
from career_lens.api.main import create_app
from career_lens.capture.options import default_tempo, normalise_tempo
from career_lens.store import db


def authenticated_client(app_url):
    application = create_app()

    def database():
        with db.connect(app_url) as connection:
            yield connection

    application.dependency_overrides[v1.database] = database
    return operator_client(application, app_url)


TARGETS = [
    ("linkedin-individual", {"url": "https://www.linkedin.com/in/example/"}),
    ("linkedin-company", {"url": "https://www.linkedin.com/company/example/"}),
    ("url", {"url": "https://example.com/"}),
    ("google-search", {"query": "example", "top_n": 0}),
]


@pytest.fixture
def http_client(monkeypatch, app_url):
    seen = []

    def submit(url, **kwargs):
        seen.append(kwargs)
        return requests.Submission("rq-example", True)

    def google(query, **kwargs):
        seen.append(kwargs)
        return requests.Submission("rq-example", True), query, kwargs.get("top_n", 0)

    monkeypatch.setattr(requests, "submit", submit)
    monkeypatch.setattr(requests, "submit_google", google)
    return authenticated_client(app_url), seen


@pytest.mark.parametrize("path,body", TARGETS)
@pytest.mark.parametrize("field,value", [("tempo", "human"), ("label", "Example"), ("typo", True)])
def test_public_rejects_operator_or_unknown_fields_before_submitting(http_client, path, body, field, value):
    client, seen = http_client
    response = client.post(f"/v1/scrape/{path}", json={**body, field: value})
    assert response.status_code == 400
    assert field in response.json()["detail"]
    assert not seen


@pytest.mark.parametrize("path,body", TARGETS)
def test_public_submission_uses_shared_human_default(http_client, path, body):
    client, seen = http_client
    response = client.post(f"/v1/scrape/{path}", json=body)
    assert response.status_code == 202
    assert normalise_tempo(seen[0]["tempo"], kind=seen[0].get("expected_kind")) == "human"
    assert seen[0]["label"] is None


@pytest.mark.parametrize("path,body", TARGETS[:-1])
def test_attempt_budget_remains_public(http_client, path, body):
    client, seen = http_client
    assert client.post(f"/v1/scrape/{path}", json={**body, "max_attempts": 5}).status_code == 202
    assert seen[0]["max_attempts"] == 5


@pytest.mark.parametrize("kind", [None, "generic", "linkedin_person", "linkedin_company", "google_search"])
def test_default_is_human_without_changing_explicit_overrides(kind):
    assert default_tempo(kind) == "human"
    assert normalise_tempo(None, kind=kind) == "human"
    assert normalise_tempo("research", kind=kind) == "research"
