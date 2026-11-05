import asyncio
import json

import httpx
import pytest
import respx
from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage

from job_finder.career_lens import CareerLens
from job_finder.hooks import QUEUED
from job_finder.tools.career_lens import (
    BLOCK_LIMIT,
    BLOCK_STOP_REASON,
    LINKEDIN_JOB_REFUSAL,
    ToolContext,
    build_tools,
    failure_kind,
)
from job_finder.translate import Translator

BASE = "https://cl.test"
CO = "https://www.linkedin.com/company/acme/"
JOB = "https://www.linkedin.com/jobs/view/4012345678/"
DESCRIPTION = "We build payment systems in Go and Python. " * 10


@pytest.fixture
async def ctx(store, recorder):
    goal = await store.create_goal("g")
    lens = CareerLens(BASE, "cl_v1.test-key")
    yield ToolContext(goal["id"], store, recorder, lens, poll_interval_s=0, poll_timeout_s=0.05,
                      resolve=lambda host: [])  # no DNS in tests
    await lens.aclose()


def tool(ctx, name):
    return next(t for t in build_tools(ctx) if t.name == name).handler


def text(result) -> str:
    return result["content"][0]["text"]


def queued(request_id, kind="linkedin_company", url=CO):
    return httpx.Response(202, json={"request_id": request_id, "status": "queued", "target": {"kind": kind, "url": url}})


async def role(artifacts, goal_id, url=JOB) -> str:
    [row] = await artifacts.save_jobs(goal_id, [{"url": url, "title": "Backend Engineer", "company_name": "Acme"}],
                                      "job-discovery")
    return row["job_id"]


@respx.mock
async def test_google_search_returns_results(ctx):
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-1", "status": "succeeded", "query": "acme",
        "results": [{"rank": 1, "title": "Acme", "url": "https://acme.com", "snippet": "anvils"}]}))
    result = await tool(ctx, "google_search")({"query": "acme"})
    assert "is_error" not in result
    assert "1. Acme\n   https://acme.com" in text(result)


@respx.mock
async def test_google_search_still_running_is_queued_for_the_watcher(ctx, store):
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-9", "status": "queued", "query": "acme", "results": None}))
    respx.get(f"{BASE}/v1/search/rq-9").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-9", "status": "running", "query": "acme", "results": None}))
    respx.get(f"{BASE}/v1/requests/rq-9").mock(return_value=httpx.Response(200, json={"status": "running"}))
    result = await tool(ctx, "google_search")({"query": "acme"})
    assert "queued as rq-9" in text(result)
    [pending] = await store.pending_scrapes()
    assert (pending["kind"], pending["request_id"]) == ("google_search", "rq-9")


@respx.mock
async def test_google_turned_off_is_a_failed_search_not_no_jobs(ctx):
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-g", "status": "queued", "query": "acme", "results": None}))
    respx.get(f"{BASE}/v1/requests/rq-g").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-g", "status": "queued", "wait_reason": "no_google_capable_workers"}))
    cancel = respx.post(f"{BASE}/v1/requests/rq-g/cancel").mock(return_value=httpx.Response(200, json={}))
    result = await tool(ctx, "google_search")({"query": "acme"})
    assert result["is_error"] and "Google search has no worker" in text(result)
    assert 'not "no jobs"' in text(result) and cancel.called


@respx.mock
async def test_career_lens_down_gives_an_error_result_not_an_exception(ctx):
    respx.get(f"{BASE}/v1/linkedin-company").mock(side_effect=httpx.ConnectError("down"))
    result = await tool(ctx, "get_company")({"linkedin_url": CO})
    assert result["is_error"] is True and "unreachable" in text(result)
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(401, json={"detail": "no"}))
    result = await tool(ctx, "google_search")({"query": "acme"})
    assert result["is_error"] is True and "rejected the API key" in text(result)


# Companies


def company(meta: dict, items: list[dict] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"entity": {"url": CO}, "observed_at": "2026-11-04T10:00:00Z",
                                     "data": {"profile": {"name": "Acme"},
                                              "jobs": {"data": {"items": items or []}}},
                                     "meta": {"jobs": meta}})


ITEM = {"title": "Backend Engineer", "url": JOB, "location": "Pune", "posted_at": "2 days ago",
        "posting_status": "posted"}


@pytest.mark.parametrize("meta, items, said", [
    ({"status": "parsed", "reached_end": True, "captured_pages": 1}, [ITEM], "read to the end, 1 roles"),
    ({"status": "parsed", "reached_end": True, "captured_pages": 1}, [], "read to the end, no open roles"),
    ({"status": "parsed", "reached_end": False, "captured_pages": 3}, [ITEM], "PARTIAL. 1 roles from 3 pages"),
    ({"status": "not_captured"}, [], 'NOT READ. The request can succeed without this section'),
    ({"status": "failed"}, [], 'the read FAILED'),
    ({"status": "absent"}, [], "no jobs section"),
])
@respx.mock
async def test_get_company_says_what_its_jobs_section_means(ctx, artifacts, meta, items, said):
    respx.get(f"{BASE}/v1/linkedin-company").mock(return_value=company(meta, items))
    result = await tool(ctx, "get_company")({"linkedin_url": CO})
    assert said in text(result)
    if meta["status"] != "not_captured":
        [read] = await artifacts.list_job_reads(ctx.goal_id)
        assert read["company_name"] == "Acme"


@respx.mock
async def test_get_company_says_not_saved_yet(ctx):
    respx.get(f"{BASE}/v1/linkedin-company").mock(return_value=httpx.Response(404, json={"detail": "none"}))
    result = await tool(ctx, "get_company")({"linkedin_url": CO})
    assert "is_error" not in result and "nothing saved yet. Use scrape_company" in text(result)


@respx.mock
async def test_scrape_company_queues_with_about_and_jobs(ctx, store):
    submit = respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=queued("rq-c"))
    result = await tool(ctx, "scrape_company")({"linkedin_url": CO})
    body = json.loads(submit.calls.last.request.content)
    assert body == {"url": CO, "freshness": "7d", "linkedin_sections": ["about", "jobs"]}
    assert "tempo" not in body and "label" not in body
    assert QUEUED.findall(text(result)) == ["rq-c"]
    [pending] = await store.pending_scrapes()
    assert (pending["kind"], pending["wants_jobs"]) == ("linkedin_company", True)


@respx.mock
async def test_scrape_company_sends_the_chosen_sections(ctx, store):
    submit = respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=queued("rq-c"))
    await tool(ctx, "scrape_company")({"linkedin_url": CO, "linkedin_sections": "about"})
    assert json.loads(submit.calls.last.request.content)["linkedin_sections"] == ["about"]
    [pending] = await store.pending_scrapes()
    assert pending["wants_jobs"] is False


async def test_an_unknown_section_is_refused_before_any_request(ctx, store):
    result = await tool(ctx, "scrape_company")({"linkedin_url": CO, "linkedin_sections": ["people"]})
    assert result["is_error"] and "Unknown sections: people" in text(result)
    assert await store.pending_scrapes() == []


@respx.mock
async def test_a_scrape_for_a_role_records_the_role(ctx, store, artifacts):
    job_id = await role(artifacts, ctx.goal_id)
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=queued("rq-c"))
    await tool(ctx, "scrape_company")({"linkedin_url": CO, "job_id": job_id})
    [scrape] = await store.list_scrapes(ctx.goal_id)
    assert scrape["request_id"] == "rq-c"
    other = (await store.create_goal("other"))["id"]
    other_role = await role(artifacts, other)
    refused = await tool(ctx, "scrape_company")({"linkedin_url": CO, "job_id": other_role})
    assert refused["is_error"] and "Unknown job_id" in text(refused)


@respx.mock
async def test_scrape_already_fresh_records_nothing(ctx, store):
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-f", "status": "satisfied"}))
    result = await tool(ctx, "scrape_company")({"linkedin_url": CO})
    assert "already fresh" in text(result) and await store.pending_scrapes() == []


@respx.mock
async def test_a_pending_scrape_with_other_sections_is_not_an_error(ctx, store):
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=httpx.Response(409, json={"detail": "busy"}))
    result = await tool(ctx, "scrape_company")({"linkedin_url": CO})
    assert "is_error" not in result and "already scraping" in text(result)


# LinkedIn job search


@respx.mock
async def test_a_job_search_is_queued_with_its_filters_and_no_tempo_or_label(ctx, store):
    submit = respx.post(f"{BASE}/v1/scrape/linkedin-job-search").mock(return_value=queued(
        "rq-s", "linkedin_job_search", "https://www.linkedin.com/jobs/search/?keywords=go&location=Pune"))
    result = await tool(ctx, "search_linkedin_jobs")({
        "keywords": " go  engineer ", "location": "Pune", "posted_within": "week",
        "workplace_types": ["remote", "Hybrid"], "job_pages": 3})
    assert json.loads(submit.calls.last.request.content) == {
        "keywords": "go engineer", "location": "Pune", "posted_within": "week",
        "workplace_types": ["remote", "hybrid"], "job_pages": 3, "freshness": "7d"}
    assert QUEUED.findall(text(result)) == ["rq-s"] and "get_job_search" in text(result)
    [pending] = await store.pending_scrapes()
    assert pending["kind"] == "linkedin_job_search"
    assert (await store.usage(ctx.goal_id))["fresh_scrapes"] == 1


@pytest.mark.parametrize("args, said", [
    ({}, "Give keywords."),
    ({"keywords": "go", "posted_within": "year"}, "posted_within must be one of"),
    ({"keywords": "go", "workplace_types": ["office"]}, "Unknown workplace types: office"),
    ({"keywords": "go", "job_pages": 40}, "job_pages must be a number from 1 to 20"),
])
async def test_a_bad_job_search_is_refused_before_any_request(ctx, store, args, said):
    result = await tool(ctx, "search_linkedin_jobs")(args)
    assert result["is_error"] and said in text(result)
    assert await store.pending_scrapes() == []


def search_result(*, reached_end, items):
    return httpx.Response(200, json={
        "request_id": "rq-s", "status": "succeeded", "failure": None,
        "result": {"keywords": "go engineer", "location": "Pune", "observed_at": "2026-11-04T10:00:00Z",
                   "captured_pages": 2, "reached_end": reached_end, "items": items}})


@respx.mock
async def test_a_job_search_result_lists_roles_without_the_viewers_insights(ctx):
    item = {**ITEM, "company_name": "Acme", "insights": ["2 connections work here"]}
    respx.get(f"{BASE}/v1/linkedin-job-search/rq-s").mock(return_value=search_result(reached_end=True, items=[item]))
    out = text(await tool(ctx, "get_job_search")({"request_id": "rq-s"}))
    assert "Read to the end: 1 roles." in out
    assert f"- Backend Engineer (Pune; posted 2 days ago) {JOB} — Acme" in out
    assert "connections" not in out and "source linkedin_search" in out


@respx.mock
async def test_a_job_search_cut_by_its_pages_is_partial(ctx):
    respx.get(f"{BASE}/v1/linkedin-job-search/rq-s").mock(return_value=search_result(reached_end=False, items=[ITEM]))
    out = text(await tool(ctx, "get_job_search")({"request_id": "rq-s"}))
    assert "PARTIAL: 1 roles from 2 pages" in out and "Do not say that these are all the roles" in out


@respx.mock
async def test_a_running_or_failed_job_search_is_not_no_jobs(ctx):
    respx.get(f"{BASE}/v1/linkedin-job-search/rq-s").mock(side_effect=[
        httpx.Response(202, json={"request_id": "rq-s", "status": "queued", "result": None}),
        httpx.Response(200, json={"request_id": "rq-s", "status": "failed", "result": None,
                                  "failure": {"cause": "blocked", "detail": "checkpoint"}})])
    running = await tool(ctx, "get_job_search")({"request_id": "rq-s"})
    assert "still runs" in text(running) and 'not "no jobs"' in text(running)
    failed = await tool(ctx, "get_job_search")({"request_id": "rq-s"})
    assert failed["is_error"] and "failed (blocked)" in text(failed) and 'not "no jobs"' in text(failed)
    assert finished("get_job_search", failed)["failure_kind"] == "blocked"


@respx.mock
async def test_a_fresh_job_search_is_read_at_once(ctx, store):
    respx.post(f"{BASE}/v1/scrape/linkedin-job-search").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-s", "status": "satisfied"}))
    respx.get(f"{BASE}/v1/linkedin-job-search/rq-s").mock(return_value=search_result(reached_end=True, items=[ITEM]))
    out = text(await tool(ctx, "search_linkedin_jobs")({"keywords": "go"}))
    assert "Read to the end: 1 roles." in out and await store.pending_scrapes() == []


# LinkedIn job postings


def posting(status="open", description=DESCRIPTION):
    return httpx.Response(200, json={"observed_at": "2026-11-04T10:00:00Z", "job": {
        "job_url": JOB, "title": "Backend Engineer", "company_name": "Acme", "location": "Pune",
        "workplace_type": "Hybrid", "posted_at": "1 day ago", "posting_status": status,
        "description_text": description}})


@respx.mock
async def test_get_linkedin_job_saves_the_full_description_on_the_role(ctx, artifacts):
    job_id = await role(artifacts, ctx.goal_id)
    respx.get(f"{BASE}/v1/linkedin-job").mock(return_value=posting())
    out = text(await tool(ctx, "get_linkedin_job")({"url": JOB, "job_id": job_id}))
    assert "Title: Backend Engineer" in out and "Workplace: Hybrid" in out and "We build payment" in out
    assert out.endswith("Saved on the role as full_description.")
    job = await artifacts.get_job(ctx.goal_id, job_id)
    assert (job["evidence_level"], job["description_text"]) == ("full_description", DESCRIPTION)


@respx.mock
async def test_a_closed_or_empty_posting_says_so_and_is_not_saved_as_text(ctx, artifacts):
    job_id = await role(artifacts, ctx.goal_id)
    respx.get(f"{BASE}/v1/linkedin-job").mock(return_value=posting("closed", "Short."))
    out = text(await tool(ctx, "get_linkedin_job")({"url": JOB, "job_id": job_id}))
    assert "Status: CLOSED" in out and "Not saved on the role" in out
    assert (await artifacts.get_job(ctx.goal_id, job_id))["evidence_level"] == "listing_only"


@respx.mock
async def test_get_linkedin_job_says_not_saved_yet(ctx):
    respx.get(f"{BASE}/v1/linkedin-job").mock(return_value=httpx.Response(404, json={"detail": "none"}))
    result = await tool(ctx, "get_linkedin_job")({"url": JOB})
    assert "is_error" not in result and "Use scrape_linkedin_job" in text(result)


@pytest.mark.parametrize("name", ["get_linkedin_job", "scrape_linkedin_job"])
async def test_the_job_tools_take_only_linkedin_job_urls(ctx, name):
    result = await tool(ctx, name)({"url": "https://jobs.lever.co/acme/1"})
    assert result["is_error"] and "jobs/view" in text(result)


@respx.mock
async def test_scrape_linkedin_job_queues_for_the_role(ctx, store, artifacts):
    job_id = await role(artifacts, ctx.goal_id)
    submit = respx.post(f"{BASE}/v1/scrape/linkedin-job").mock(return_value=queued("rq-j", "linkedin_job", JOB))
    result = await tool(ctx, "scrape_linkedin_job")({"url": JOB, "job_id": job_id})
    assert json.loads(submit.calls.last.request.content) == {"url": JOB, "freshness": "7d"}
    assert QUEUED.findall(text(result)) == ["rq-j"]
    [scrape] = await store.pending_scrapes()
    assert scrape["kind"] == "linkedin_job" and (await store.usage(ctx.goal_id))["fresh_scrapes"] == 1
    assert (await artifacts.pipeline(ctx.goal_id))["waiting_scrapes"] == 1


async def test_read_page_sends_linkedin_jobs_to_get_linkedin_job(ctx):
    from job_finder.page_reader import Answer

    class Reader:
        async def answer(self, *args):
            return Answer("x", 0)

    ctx.reader = Reader()
    result = await tool(ctx, "read_page")({"url": "https://in.linkedin.com/jobs/view/backend-at-acme-42", "prompt": "q"})
    assert result["is_error"] and text(result) == LINKEDIN_JOB_REFUSAL


# Waiting


async def test_wait_for_scrapes_reports_finished_scrapes_and_marks_them_reported(ctx, store):
    scrape = await store.add_scrape(ctx.goal_id, "rq-7", "linkedin_company", CO, True)
    await store.finish_scrape(scrape["id"], "succeeded", None)
    result = await tool(ctx, "wait_for_scrapes")({"request_ids": ["rq-7"]})
    assert "is_error" not in result
    assert f"- rq-7 (linkedin_company, {CO}): succeeded" in text(result)
    assert "Read the new data" in text(result)
    assert await store.count_unreported(ctx.goal_id) == 0  # the watcher does not resume the goal for it


async def test_wait_for_scrapes_returns_when_the_watcher_finishes_a_scrape(ctx, store):
    scrape = await store.add_scrape(ctx.goal_id, "rq-8", "linkedin_company", "u", True)
    ctx.poll_interval_s = 0.01

    async def watcher():
        await asyncio.sleep(0.05)
        await store.finish_scrape(scrape["id"], "failed", "blocked")

    finisher = asyncio.create_task(watcher())
    result = await tool(ctx, "wait_for_scrapes")({"request_ids": ["rq-8"]})
    await finisher
    assert "rq-8 (linkedin_company, u): failed (blocked)" in text(result)


async def test_wait_for_scrapes_stops_at_the_limit_and_keeps_pending_scrapes(ctx, store):
    await store.add_scrape(ctx.goal_id, "rq-9", "linkedin_company", CO, True)
    ctx.scrape_wait_s = 0
    result = await tool(ctx, "wait_for_scrapes")({"request_ids": ["rq-9", "rq-unknown"]})
    assert "rq-9" in text(result) and "Still pending" in text(result)
    assert "The main agent sends you a message when they finish." in text(result)
    assert "Unknown request ids: rq-unknown." in text(result)
    assert len(await store.pending_scrapes()) == 1


async def test_wait_for_scrapes_needs_known_ids(ctx):
    assert (await tool(ctx, "wait_for_scrapes")({"request_ids": []}))["is_error"] is True
    assert (await tool(ctx, "wait_for_scrapes")({"request_ids": ["rq-none"]}))["is_error"] is True




PAGE_URL = "https://example.com/startups"


class FakeReader:
    def __init__(self, answer="Acme: https://acme.com", error=None, cost=0.0):
        self.answer_text, self.error, self.cost, self.calls = answer, error, cost, []

    async def answer(self, url, markdown, prompt):
        from job_finder.page_reader import Answer
        self.calls.append((url, markdown, prompt))
        if self.error:
            raise self.error
        return Answer(self.answer_text, self.cost)


def page(markdown="# Startups\n\n[Acme](https://acme.com)", status="succeeded", **capture):
    return httpx.Response(200, json={"capture": {
        "status": status, "url_final": PAGE_URL, "title": "Startups", "markdown": markdown, **capture}})


def mock_page(*responses):
    respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(
        202, json={"request_id": "rq-p", "status": "queued"}))
    return respx.get(f"{BASE}/v1/url/rq-p").mock(side_effect=list(responses))


@respx.mock
async def test_read_page_waits_then_answers_the_prompt_from_the_markdown(ctx):
    mock_page(httpx.Response(202, json={"status": "running"}), page())
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "List the companies with links."})
    assert "is_error" not in result
    assert text(result) == f"Page: {PAGE_URL}\n\nAcme: https://acme.com"
    assert ctx.reader.calls == [(PAGE_URL, "# Startups\n\n[Acme](https://acme.com)", "List the companies with links.")]


@respx.mock
async def test_a_stop_can_cancel_what_a_tool_waits_on(ctx):
    """While read_page and google_search wait on career_lens, their request is in the session's waiting set."""
    seen = []

    def waiting(response):
        def answer(request):
            seen.append(set(ctx.waiting))
            return response
        return answer

    respx.get(f"{BASE}/v1/requests/rq-s").mock(return_value=httpx.Response(200, json={"status": "running"}))
    respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(
        202, json={"request_id": "rq-p", "status": "queued"}))
    respx.get(f"{BASE}/v1/url/rq-p").mock(side_effect=waiting(page()))
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(202, json={
        "request_id": "rq-s", "status": "queued", "query": "acme", "results": None}))
    respx.get(f"{BASE}/v1/search/rq-s").mock(side_effect=waiting(httpx.Response(200, json={
        "request_id": "rq-s", "status": "succeeded", "query": "acme", "results": []})))
    ctx.reader = FakeReader()
    await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    await tool(ctx, "google_search")({"query": "acme"})
    assert seen == [{"rq-p"}, {"rq-s"}]
    assert ctx.waiting == set()  # done: nothing to cancel


@respx.mock
async def test_read_page_reads_a_website_given_as_a_domain(ctx):
    submit = respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(
        202, json={"request_id": "rq-p", "status": "queued"}))
    respx.get(f"{BASE}/v1/url/rq-p").mock(return_value=page())
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": "acme.ai/team", "prompt": "q"})  # as the jobs keep websites
    assert "is_error" not in result
    assert json.loads(submit.calls.last.request.content)["url"] == "https://acme.ai/team"


@respx.mock
async def test_read_page_makes_relative_links_absolute(ctx):
    mock_page(page("[Team](/team) [Jobs](careers.html) [Top](#top) [Mail](mailto:a@b.c) [Acme](https://acme.com)"))
    ctx.reader = FakeReader()
    await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    [(_, markdown, _)] = ctx.reader.calls
    assert markdown == ("[Team](https://example.com/team) [Jobs](https://example.com/careers.html) [Top](#top) "
                        "[Mail](mailto:a@b.c) [Acme](https://acme.com)")


@respx.mock
async def test_read_page_reports_a_failed_capture(ctx):
    mock_page(page(markdown=None, status="failed", error={"kind": "blocked", "detail": "x"}))
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    assert result["is_error"] is True and "the page capture failed (blocked)" in text(result)


@respx.mock
async def test_read_page_still_loading_after_the_wait(ctx):
    mock_page().side_effect = None
    respx.get(f"{BASE}/v1/url/rq-p").mock(return_value=httpx.Response(202, json={"status": "running"}))
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    assert "is still loading" in text(result) and "call read_page again" in text(result)


@respx.mock
async def test_read_page_reports_a_small_model_error(ctx):
    from job_finder.page_reader import PageReaderError
    mock_page(page())
    ctx.reader = FakeReader(error=PageReaderError("the page reader failed (HTTP 529)"))
    result = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    assert result["is_error"] is True and "HTTP 529" in text(result)


@pytest.mark.parametrize("url", ["https://www.linkedin.com/company/acme/", "https://in.linkedin.com/in/jane/",
                                 "https://www.linkedin.com/jobs/view/42/", "not a url"])
async def test_read_page_refuses_profiles_and_bad_urls_without_calling_career_lens(ctx, url):
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": url, "prompt": "q"})
    assert result["is_error"] is True


@pytest.mark.parametrize("args", [{"url": PAGE_URL}, {"url": PAGE_URL, "prompt": "  "}])
async def test_read_page_needs_a_prompt_without_calling_career_lens(ctx, args):
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")(args)
    assert result["is_error"] is True and "Give a prompt" in text(result)


async def test_read_page_without_a_small_model_is_an_error(ctx):
    result = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    assert result["is_error"] is True and "not configured" in text(result)


def test_read_page_requires_url_and_prompt_in_its_schema(ctx):
    [read_page] = [t for t in build_tools(ctx) if t.name == "read_page"]
    assert read_page.input_schema["required"] == ["url", "prompt"]


# Source unavailable: the translator reads the failure kind back from the real error replies of these tools.

PARKED = {"kind": "not_found", "detail": "Requested domain is parked / for sale: example.com"}
BROKEN = {"kind": "browser_error", "detail": "the browser closed"}


def finished(name: str, result: dict) -> dict:
    """The tool_finished event that the translator makes from the result of a real tool call."""
    t = Translator()
    t.translate(AssistantMessage(model="m", parent_tool_use_id="a1", content=[
        ToolUseBlock(id="u1", name=f"mcp__career_lens__{name}", input={})]))
    [event] = t.translate(UserMessage(parent_tool_use_id="a1", content=[ToolResultBlock(
        tool_use_id="u1", content=result["content"], is_error=result.get("is_error", False))]))
    return event.data


def failed_scrape(failure: dict) -> httpx.Response:
    return httpx.Response(200, json={"request_id": "rq-f", "status": "failed", "failure": failure})


@pytest.mark.parametrize("failure", [PARKED, BROKEN])
@respx.mock
async def test_read_page_failed_capture_gives_its_failure_kind(ctx, failure):
    mock_page(page(markdown=None, status="failed", error=failure))
    ctx.reader = FakeReader()
    event = finished("read_page", await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"}))
    assert event["ok"] is False and event["failure_kind"] == failure["kind"]


@pytest.mark.parametrize("failure", [PARKED, BROKEN])
@respx.mock
async def test_google_search_failure_gives_its_failure_kind(ctx, failure):
    respx.post(f"{BASE}/v1/search").mock(return_value=failed_scrape(failure))
    event = finished("google_search", await tool(ctx, "google_search")({"query": 'site:jobs.lever.co "Backend"'}))
    assert event["ok"] is False and event["failure_kind"] == failure["kind"]


@pytest.mark.parametrize("failure", [PARKED, BROKEN])
@respx.mock
async def test_scrape_failure_gives_its_failure_kind(ctx, failure):
    respx.post(f"{BASE}/v1/scrape/linkedin-company").mock(return_value=failed_scrape(failure))
    event = finished("scrape_company", await tool(ctx, "scrape_company")({"linkedin_url": CO}))
    assert event["ok"] is False and event["failure_kind"] == failure["kind"]


@respx.mock
async def test_other_read_page_errors_give_no_failure_kind(ctx):
    ctx.reader = FakeReader()
    no_prompt = await tool(ctx, "read_page")({"url": PAGE_URL})
    respx.post(f"{BASE}/v1/scrape/url").mock(return_value=httpx.Response(401, json={"detail": "no"}))
    rejected = await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    for result in (no_prompt, rejected):
        event = finished("read_page", result)
        assert event["ok"] is False and "failure_kind" not in event


def test_failure_kind_matches_only_the_exact_replies_of_career_lens_tools():
    reply = "Could not read https://example.com: the page capture failed (not_found). Use a different source."
    assert failure_kind("read_page", reply) == "not_found"
    assert failure_kind("google_search", reply) is None  # a reply of another tool
    assert failure_kind("save_job_research", reply) is None  # not a career-lens tool
    assert failure_kind("read_page", reply.replace("Use a different", "Try another")) is None
    assert failure_kind("read_page", f"Note: {reply}") is None
    assert failure_kind("read_page", "Unknown page (not_found).") is None
    assert failure_kind("scrape_company", "") is None


@respx.mock
async def test_read_page_adds_the_cost_of_the_small_model_to_the_goal(ctx):
    mock_page(page())
    ctx.reader = FakeReader(cost=0.004)
    await tool(ctx, "read_page")({"url": PAGE_URL, "prompt": "q"})
    assert (await ctx.store.get_goal(ctx.goal_id))["spend_usd"] == pytest.approx(0.004)



@pytest.mark.parametrize("url", ["http://127.0.0.1:8090/console", "http://localhost:8000/", "https://www.pornhub.com/",
                                 "http://192.168.1.1/"])
async def test_read_page_refuses_blocked_sites_without_calling_career_lens(ctx, store, url):
    ctx.reader = FakeReader()
    result = await tool(ctx, "read_page")({"url": url, "prompt": "q"})  # respx is off: a call would fail
    assert result["is_error"] is True and text(result).startswith(f"Refused: {url} is ")
    assert "Use a different source" in text(result)
    [event] = [e for e in await store.list_events(ctx.goal_id, 0) if e["type"] == "site_blocked"]
    assert event["url"] == url and event["tool"] == "read_page"


async def test_read_page_refuses_a_name_that_resolves_to_this_computer(ctx):
    ctx.reader, ctx.resolve = FakeReader(), lambda host: ["127.0.0.1"]
    result = await tool(ctx, "read_page")({"url": "https://local.example.com/", "prompt": "q"})
    assert result["is_error"] is True and "local or private" in text(result)


async def test_too_many_refused_pages_stop_the_goal(ctx):
    stops = []
    ctx.reader, ctx.on_block_limit = FakeReader(), stops.append
    for _ in range(BLOCK_LIMIT - 1):
        await tool(ctx, "read_page")({"url": "https://www.pornhub.com/", "prompt": "q"})
    assert stops == []
    await tool(ctx, "read_page")({"url": "http://127.0.0.1/", "prompt": "q"})
    assert stops == [BLOCK_STOP_REASON]


@respx.mock
async def test_google_search_removes_results_on_blocked_sites(ctx, store):
    respx.post(f"{BASE}/v1/search").mock(return_value=httpx.Response(200, json={
        "request_id": "rq-1", "status": "succeeded", "query": "acme",
        "results": [{"rank": 1, "title": "Acme", "url": "https://acme.com", "snippet": "anvils"},
                    {"rank": 2, "title": "Bad", "url": "https://www.xvideos.com/acme", "snippet": "x"},
                    {"rank": 3, "title": "Local", "url": "http://10.0.0.1/", "snippet": "x"}]}))
    result = await tool(ctx, "google_search")({"query": "acme"})
    body = text(result)
    assert "1. Acme" in body and "xvideos" not in body and "10.0.0.1" not in body
    assert "Removed 2 results on blocked sites." in body
    [event] = [e for e in await store.list_events(ctx.goal_id, 0) if e["type"] == "site_blocked"]
    assert event["tool"] == "google_search" and event["count"] == 2
