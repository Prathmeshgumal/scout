"""The API exposes exactly the routes that its consumer uses."""

from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from career_lens.api.main import create_app
from career_lens.api.db import database
from career_lens.auth.service import create_api_key
from career_lens.store import db

EXPECTED = {
    ("GET", "/healthz"), ("POST", "/v1/scrape/{path}"),
    ("GET", "/v1/requests/{request_id}"), ("POST", "/v1/requests/{request_id}/cancel"),
    ("GET", "/v1/linkedin-individual"), ("GET", "/v1/linkedin-company"),
    ("GET", "/v1/url"), ("GET", "/v1/url/{request_id}"),
    ("GET", "/v1/google-search"), ("GET", "/v1/google-search/{request_id}"),
    ("POST", "/v1/search"), ("GET", "/v1/search/{request_id}"),
    ("GET", "/v1/linkedin-job"), ("GET", "/v1/linkedin-job-search/{request_id}"),
    ("GET", "/operator/api/overview"), ("POST", "/operator/api/accounts"),
    ("PUT", "/operator/api/accounts/{account_id}/password"),
    ("PUT", "/operator/api/accounts/{account_id}/limits"),
    ("GET", "/operator/api/api-keys"), ("POST", "/operator/api/api-keys"),
    ("POST", "/operator/api/api-keys/{key_id}/revoke"),
    ("PUT", "/operator/api/accounts/{account_id}/shift"),
    ("POST", "/operator/api/workers/{worker_id}/assignments"),
    ("POST", "/operator/api/assignments/{assignment_id}/{command}"),
    ("PATCH", "/operator/api/workers/{worker_id}/google-policy"),
}


def app_routes():
    return {
        (method.upper(), path)
        for path, item in create_app().openapi()["paths"].items()
        for method in item
    }


def test_routes_are_exactly_the_surface():
    assert app_routes() == EXPECTED


def test_docs_pages_need_no_key():
    client = TestClient(create_app())
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


@pytest.fixture
def client_with_key(app, app_url):
    issued = create_api_key(app, "test", datetime.now(UTC))

    def override():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    instance = create_app()
    instance.dependency_overrides[database] = override
    with TestClient(instance) as client:
        yield client, issued.token


def test_unknown_scrape_path_is_404(client_with_key):
    client, key = client_with_key
    response = client.post("/v1/scrape/not-a-site", headers={"Authorization": f"Bearer {key}"},
                           json={"url": "https://example.org"})
    assert response.status_code == 404


def test_missing_or_wrong_key_is_401_without_echo(client_with_key):
    client, _ = client_with_key
    assert client.get("/v1/requests/x").status_code == 401
    wrong = "cl_v1.not-a-real-key"
    response = client.get("/v1/requests/x", headers={"Authorization": f"Bearer {wrong}"})
    assert response.status_code == 401
    assert wrong not in response.text
