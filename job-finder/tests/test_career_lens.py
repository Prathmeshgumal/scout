import json

import httpx
import pytest
import respx

from job_finder.career_lens import CareerLens, CareerLensError, ScrapeConflict, jobs_state, wait_text

BASE = "https://cl.test"
CO = "https://www.linkedin.com/company/acme/"
JOB = "https://www.linkedin.com/jobs/view/4012345678/"
# Fields that career-lens refuses on its public routes. No call may send them.
REFUSED = {"tempo", "label"}


@pytest.fixture
async def lens():
    client = CareerLens(BASE, "cl_v1.test-key")
    yield client
    await client.aclose()


def sent(route) -> dict:
    return json.loads(route.calls.last.request.content)


@respx.mock
async def test_scrape_company_sends_about_and_jobs_freshness_and_the_key(lens):
    route = respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(
        return_value=httpx.Response(202, json={"request_id": "rq-1", "status": "queued"}))
    sub = await lens.scrape_company(CO)
    assert (sub.status_code, sub.body["request_id"]) == (202, "rq-1")
    assert route.calls.last.request.headers["Authorization"] == "Bearer cl_v1.test-key"
    assert sent(route) == {"url": CO, "freshness": "7d", "linkedin_sections": ["about", "jobs"]}
    await lens.scrape_company(CO, ["about"])
    assert sent(route)["linkedin_sections"] == ["about"]


@respx.mock
async def test_no_call_sends_a_field_that_career_lens_refuses(lens):
    routes = [
        respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={"request_id": "a"})),
        respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(202, json={"request_id": "b"})),
        respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=httpx.Response(202, json={"request_id": "c"})),
        respx.post(f"{BASE}/v1/scrape/linkedin-job").mock(return_value=httpx.Response(202, json={"request_id": "d"})),
        respx.post(f"{BASE}/v1/scrape/linkedin-job-search").mock(
            return_value=httpx.Response(202, json={"request_id": "e"})),
    ]
    await lens.search("go engineer")
    await lens.scrape_url("https://example.com")
    await lens.scrape_company(CO)
    await lens.scrape_job(JOB)
    await lens.search_jobs("go", location="Pune", posted_within="day", workplace_types=["remote"], job_pages=2)
    for route in routes:
        assert not REFUSED & set(sent(route)), route


@respx.mock
async def test_search_posts_compact_request(lens):
    route = respx.post(f"{BASE}/v1/search").mock(
        return_value=httpx.Response(200, json={"request_id": "rq-3", "status": "succeeded", "results": []}))
    await lens.search("backend engineer pune")
    assert sent(route) == {"query": "backend engineer pune", "wait_seconds": 0, "priority": "high", "freshness": "7d"}


@respx.mock
async def test_a_job_search_sends_only_the_filters_that_it_has(lens):
    route = respx.post(f"{BASE}/v1/scrape/linkedin-job-search").mock(
        return_value=httpx.Response(202, json={"request_id": "rq-s", "status": "queued"}))
    await lens.search_jobs("go engineer")
    assert sent(route) == {"keywords": "go engineer", "freshness": "7d"}
    await lens.search_jobs("go", location="Pune", posted_within="week", workplace_types=["hybrid"], job_pages=4)
    assert sent(route) == {"keywords": "go", "location": "Pune", "posted_within": "week",
                           "workplace_types": ["hybrid"], "job_pages": 4, "freshness": "7d"}


@respx.mock
async def test_a_job_search_result_keeps_the_running_status(lens):
    respx.get(f"{BASE}/v1/linkedin-job-search/rq-s").mock(side_effect=[
        httpx.Response(202, json={"status": "queued", "result": None}),
        httpx.Response(200, json={"status": "succeeded", "result": {"items": []}})])
    assert (await lens.job_search_result("rq-s")).status_code == 202
    assert (await lens.job_search_result("rq-s")).body["result"] == {"items": []}


@respx.mock
async def test_saved_reads_return_none_on_404(lens):
    respx.get(f"{BASE}/v1/linkedin-company").mock(return_value=httpx.Response(404, json={"detail": "nothing"}))
    job = respx.get(f"{BASE}/v1/linkedin-job").mock(return_value=httpx.Response(404, json={"detail": "nothing"}))
    assert await lens.get_company(CO) is None
    assert await lens.get_job(JOB) is None
    assert job.calls.last.request.url.params["url"] == JOB


@respx.mock
async def test_a_pending_scrape_with_other_sections_is_a_conflict(lens):
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=httpx.Response(409, json={
        "detail": "an outstanding request for this URL has a different LinkedIn section selection"}))
    with pytest.raises(ScrapeConflict, match="different LinkedIn section selection"):
        await lens.scrape_company(CO, ["about"])


@respx.mock
async def test_errors_are_readable_and_never_show_the_key(lens):
    respx.get(f"{BASE}/v1/requests/rq-x").mock(return_value=httpx.Response(401, json={"detail": "bad key"}))
    with pytest.raises(CareerLensError, match="rejected the API key") as error:
        await lens.request_status("rq-x")
    assert "cl_v1" not in str(error.value)
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(
        return_value=httpx.Response(400, json={"detail": "Enter a linkedin company URL"}))
    with pytest.raises(CareerLensError, match="Enter a linkedin company URL"):
        await lens.scrape_company("https://example.com")
    respx.get(f"{BASE}/v1/requests/rq-y").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CareerLensError, match="unreachable"):
        await lens.request_status("rq-y")
    respx.get(f"{BASE}/v1/requests/rq-z").mock(return_value=httpx.Response(503, json={"detail": "x"}))
    with pytest.raises(CareerLensError, match="HTTP 503"):
        await lens.request_status("rq-z")


async def test_unconfigured_client_refuses_cleanly():
    client = CareerLens("", "")
    with pytest.raises(CareerLensError, match="not configured"):
        await client.request_status("rq-1")
    assert await client.health() == "not_configured"
    await client.aclose()


@respx.mock
async def test_health_tells_down_from_up(lens):
    respx.get(f"{BASE}/healthz").mock(side_effect=[httpx.Response(200, json={"status": "ok"}),
                                                   httpx.ConnectError("down"), httpx.Response(500)])
    assert [await lens.health() for _ in range(3)] == ["online", "unreachable", "error"]


@respx.mock
async def test_url_result_asks_for_the_markdown_and_keeps_the_pending_status(lens):
    route = respx.get(f"{BASE}/v1/url/rq-p").mock(side_effect=[
        httpx.Response(202, json={"request_id": "rq-p", "status": "running", "result": None}),
        httpx.Response(200, json={"capture": {"status": "succeeded", "markdown": "# List"}})])
    assert (await lens.url_result("rq-p")).status_code == 202
    assert (await lens.url_result("rq-p")).body["capture"]["markdown"] == "# List"
    assert route.calls.last.request.url.params["include_markdown"] == "true"


@respx.mock
async def test_a_configured_freshness_is_sent_with_every_capture():
    client = CareerLens(BASE, "cl_v1.test-key", freshness="24h")
    routes = [respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={"request_id": "a"})),
              respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(202, json={"request_id": "b"})),
              respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(
                  return_value=httpx.Response(202, json={"request_id": "c"}))]
    await client.search("acme")
    await client.scrape_url("https://example.com")
    await client.scrape_company(CO)
    await client.aclose()
    assert [sent(route)["freshness"] for route in routes] == ["24h"] * 3


@respx.mock
async def test_cancel_gives_the_answer_of_career_lens(lens):
    respx.post(f"{BASE}/v1/requests/rq-1/cancel").mock(
        return_value=httpx.Response(200, json={"request_id": "rq-1", "status": "cancelled", "cancel": "cancelled"}))
    respx.post(f"{BASE}/v1/requests/rq-2/cancel").mock(
        return_value=httpx.Response(202, json={"request_id": "rq-2", "status": "running", "cancel": "stopping"}))
    assert await lens.cancel_all(["rq-1", "rq-2", "rq-1"]) == {"rq-1": "cancelled", "rq-2": "stopping"}


@respx.mock
async def test_cancel_never_fails_the_caller(lens):
    respx.post(f"{BASE}/v1/requests/rq-done/cancel").mock(return_value=httpx.Response(409, json={"detail": "x"}))
    respx.post(f"{BASE}/v1/requests/rq-down/cancel").mock(side_effect=httpx.ConnectError("refused"))
    respx.post(f"{BASE}/v1/requests/rq-odd/cancel").mock(return_value=httpx.Response(200, text="not json"))
    assert await lens.cancel_all(["rq-done", "rq-down", "rq-odd"]) == dict.fromkeys(["rq-done", "rq-down", "rq-odd"])
    assert await CareerLens("", "").cancel("rq-1") is None


# What a company reply says about its jobs (limits 3 and 4 of the spec)


def company(status=None, reached_end=None, items=0, pages=None):
    meta = {} if status is None else {"jobs": {"status": status, "reached_end": reached_end, "captured_pages": pages}}
    return {"meta": meta, "data": {"jobs": {"data": {"items": [{"title": f"r{i}"} for i in range(items)]}}}}


@pytest.mark.parametrize("body, state", [
    (company("parsed", True, 4, 1), "complete"),
    (company("parsed", True, 0, 1), "none"),
    (company("parsed", False, 75, 3), "partial"),
    (company("partial", True, 10, 1), "partial"),
    (company("absent"), "absent"),
    (company("not_available"), "absent"),
    (company("failed"), "failed"),
    (company("not_captured"), "not_captured"),
    (company("weird"), "not_captured"),
    ({"meta": {"about": {}}, "data": {}}, "not_captured"),
    ({"data": {}}, "not_requested"),
])
def test_jobs_state_tells_a_failed_read_from_no_jobs(body, state):
    assert jobs_state(body)["state"] == state


def test_a_section_with_no_payload_is_a_failed_read_not_a_crash():
    # career-lens keeps the data key, with null, for each section that it did not capture.
    body = {"meta": {"jobs": {"status": "not_captured"}}, "data": {"jobs": {"data": None}}}
    assert jobs_state(body)["state"] == "not_captured"


def test_only_none_and_absent_say_no_jobs():
    for state, body in (("partial", company("parsed", False, 75, 3)), ("not_captured", company("not_captured")),
                        ("failed", company("failed"))):
        assert "no open roles" not in jobs_state(body)["text"], state
    assert "PARTIAL. 75 roles from 3 pages" in jobs_state(company("parsed", False, 75, 3))["text"]


def test_wait_reasons_become_plain_words():
    assert wait_text("accounts_off_shift") == "LinkedIn accounts are off shift"
    assert wait_text("all_workers_busy") == "All career-lens workers are busy"
    assert wait_text("something new") == "something new"
    assert wait_text(None) is None
