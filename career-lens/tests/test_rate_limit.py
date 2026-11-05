"""The token bucket on the customer API: one bucket for each API key."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from career_lens.api import rate_limit
from career_lens.api.main import create_app
from career_lens.store.config import ConfigError
from auth_support import customer_client


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_bucket_starts_full_and_refuses_when_empty():
    clock = Clock()
    limiter = rate_limit.RateLimiter(capacity=3, per_second=1.0, clock=clock)
    assert [limiter.take("ak-1").allowed for _ in range(4)] == [True, True, True, False]


def test_a_refusal_says_when_the_next_token_comes():
    clock = Clock()
    limiter = rate_limit.RateLimiter(capacity=1, per_second=0.5, clock=clock)
    limiter.take("ak-1")
    refused = limiter.take("ak-1")
    assert not refused.allowed
    assert refused.retry_after == 2


def test_tokens_come_back_at_the_flow_rate_and_never_pass_the_capacity():
    clock = Clock()
    limiter = rate_limit.RateLimiter(capacity=2, per_second=1.0, clock=clock)
    limiter.take("ak-1"), limiter.take("ak-1")
    assert not limiter.take("ak-1").allowed
    clock.now += 1
    assert limiter.take("ak-1").allowed
    assert not limiter.take("ak-1").allowed
    clock.now += 3600
    assert limiter.take("ak-1").remaining == 1


def test_each_key_has_its_own_bucket():
    limiter = rate_limit.RateLimiter(capacity=1, per_second=1.0, clock=Clock())
    assert limiter.take("ak-1").allowed
    assert not limiter.take("ak-1").allowed
    assert limiter.take("ak-2").allowed


def test_the_defaults_apply_when_nothing_is_set(monkeypatch):
    monkeypatch.delenv(rate_limit.BURST_ENV, raising=False)
    monkeypatch.delenv(rate_limit.RATE_ENV, raising=False)
    limiter = rate_limit.from_env()
    assert (limiter.capacity, limiter.per_second) == (rate_limit.DEFAULT_BURST, rate_limit.DEFAULT_PER_SECOND)


@pytest.mark.parametrize(("name", "value"), [
    (rate_limit.BURST_ENV, "0"), (rate_limit.BURST_ENV, "-1"), (rate_limit.BURST_ENV, "1.5"),
    (rate_limit.BURST_ENV, "many"), (rate_limit.RATE_ENV, "0"), (rate_limit.RATE_ENV, "-2"),
    (rate_limit.RATE_ENV, "fast"), (rate_limit.RATE_ENV, "nan"), (rate_limit.RATE_ENV, "inf"),
])
def test_a_bad_setting_stops_the_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigError):
        rate_limit.from_env()


@pytest.fixture
def limited(app_url, monkeypatch):
    from career_lens.api import db as api_db
    from career_lens.store import db

    def database_override():
        connection = db.connect(app_url)
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setenv("CAREER_LENS_DATABASE_URL", app_url)
    monkeypatch.setenv(rate_limit.BURST_ENV, "2")
    monkeypatch.setenv(rate_limit.RATE_ENV, "1")
    app = create_app()
    app.dependency_overrides[api_db.database] = database_override
    clock = Clock()
    app.state.rate_limiter.clock = clock
    return app, clock


def test_the_api_refuses_a_key_with_an_empty_bucket(limited, app_url):
    app, _ = limited
    client = customer_client(app, app_url)
    first = client.get("/v1/requests/rq-none")
    assert first.status_code == 404
    assert client.get("/v1/requests/rq-none").status_code == 404
    refused = client.get("/v1/requests/rq-none")
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "1"
    assert refused.json()["detail"] == {"code": "rate_limited"}
    assert "schema_version" in refused.json()


def test_an_answer_tells_the_limit_and_what_is_left(limited, app_url):
    app, _ = limited
    client = customer_client(app, app_url)
    for remaining in ("1", "0"):
        response = client.get("/v1/requests/rq-none")
        assert response.status_code == 404
        assert response.headers["ratelimit-limit"] == "2"
        assert response.headers["ratelimit-remaining"] == remaining


def test_a_route_that_makes_its_own_reply_tells_the_limit_too(limited, app_url):
    app, _ = limited
    client = customer_client(app, app_url)
    response = client.post("/v1/search", json={"query": "go engineer", "wait_seconds": 0})
    assert response.status_code < 300
    assert response.headers["ratelimit-remaining"] == "1"


def test_the_bucket_fills_again_with_time(limited, app_url):
    app, clock = limited
    client = customer_client(app, app_url)
    for _ in range(2):
        client.get("/v1/requests/rq-none")
    assert client.get("/v1/requests/rq-none").status_code == 429
    clock.now += 1
    assert client.get("/v1/requests/rq-none").status_code == 404


def test_every_customer_route_counts(limited, app_url):
    app, _ = limited
    client = customer_client(app, app_url)
    calls = [
        lambda: client.get("/v1/linkedin-company", params={"url": "https://www.linkedin.com/company/acme/"}),
        lambda: client.get("/v1/google-search/rq-none"),
    ]
    for call in calls:
        assert call().status_code != 429
    refused = [client.get("/v1/url/rq-none"), client.get("/v1/linkedin-job-search/rq-none"),
               client.post("/v1/requests/rq-none/cancel"), client.post("/v1/search", json={"query": "go"})]
    assert {r.status_code for r in refused} == {429}


def test_a_request_with_no_key_is_refused_before_it_counts(limited, app_url):
    app, _ = limited
    anonymous = TestClient(app)
    for _ in range(5):
        assert anonymous.get("/v1/requests/rq-none").status_code == 401
    client = customer_client(app, app_url)
    assert client.get("/v1/requests/rq-none").status_code == 404


def test_two_keys_do_not_share_a_bucket(limited, app_url):
    app, _ = limited
    first, second = customer_client(app, app_url), customer_client(app, app_url)
    for _ in range(2):
        first.get("/v1/requests/rq-none")
    assert first.get("/v1/requests/rq-none").status_code == 429
    assert second.get("/v1/requests/rq-none").status_code == 404


def test_health_and_the_console_have_no_limit(limited):
    app, _ = limited
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/healthz").status_code == 200
        assert client.get("/operator/api/session").status_code == 200
