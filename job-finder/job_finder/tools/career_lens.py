"""The agent's web tools: plain Python functions over career-lens, served in-process to the SDK."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from collections.abc import Iterator
from typing import Any
from urllib.parse import urljoin

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, create_sdk_mcp_server, tool

from ..artifact_store import ArtifactStore
from ..career_lens import (
    GOOGLE_OFF,
    MAX_JOB_PAGES,
    POSTED_WITHIN,
    SECTIONS,
    WORKPLACE_TYPES,
    CareerLensError,
    ScrapeConflict,
    jobs_state,
    wait_text,
)
from ..jobs import is_linkedin_job_url, is_linkedin_url, linkedin_job_id, looks_like_sign_in
from ..page_reader import PageReaderError
from ..recap import scrape_line
from ..site_policy import blocked_url_reason
from ..summarize import format_search_results, summarize_company, summarize_job, summarize_job_search
from . import ToolContext, result

SERVER_NAME = "career_lens"
ACTIVE = {"queued", "assigned", "running"}
FRESH = {"satisfied", "succeeded"}
QUEUED_NOTE = ("LinkedIn scrapes are slow and run only in the working hours of the career-lens accounts. Continue with "
               "other work, then call wait_for_scrapes with the request ids. Google search and read_page do not "
               "depend on these accounts.")
WAIT_SCHEMA = {"type": "object", "properties": {"request_ids": {
    "type": "array", "items": {"type": "string"},
    "description": "Request ids from scrape_company, search_linkedin_jobs, scrape_linkedin_job or google_search."}}, "required": ["request_ids"]}
COMPANY_SCHEMA = {"type": "object", "properties": {
    "linkedin_url": {"type": "string", "description": "LinkedIn company URL, e.g. https://www.linkedin.com/company/acme/"},
}, "required": ["linkedin_url"]}
SCRAPE_SCHEMA = {"type": "object", "properties": {
    **COMPANY_SCHEMA["properties"],
    "linkedin_sections": {"type": "array", "items": {"type": "string", "enum": list(SECTIONS)},
                          "description": "The sections to scrape. Default: about and jobs."},
    "job_id": {"type": "string", "description": "The role that the scrape is for, from list_jobs. Leave it out when "
                                                "you scrape a company to find its roles."},
}, "required": ["linkedin_url"]}
PAGE_SCHEMA = {"type": "object", "properties": {
    "url": {"type": "string", "description": "The URL of the page, e.g. https://boards.greenhouse.io/acme/jobs/1"},
    "prompt": {"type": "string", "description": "What to find on the page, e.g. \"Give the full job description, word "
                                                "for word\"."},
}, "required": ["url", "prompt"]}

LINKEDIN_JOB_REFUSAL = ("read_page cannot read LinkedIn job pages: they show only a sign-in modal. Use get_linkedin_job "
                        "for a LinkedIn job posting. Or find another copy of the role on the careers page of the "
                        "employer or on a job board.")
LINKEDIN_PAGE_REFUSAL = ("This is a LinkedIn page. Use get_company for a company and get_linkedin_job for a job posting. "
                         "Do not read LinkedIn profiles.")
SIGN_IN_WALL = ("The page {url} is a sign-in wall. It has no job text, so do not save it as a description. Use the search "
                "snippet, or find another copy of the role.")
AUTH_WALL = re.compile(r"linkedin\.com/(authwall|login|uas/login|checkpoint)", re.IGNORECASE)
# A website as agents write it: a domain, maybe with a path, with no scheme. For example "acme.ai/team".
BARE_SITE = re.compile(r"[a-z0-9-]+(\.[a-z0-9-]+)+(/\S*)?", re.IGNORECASE)
RELATIVE_LINK = re.compile(r"\]\((?![a-z][a-z0-9+.-]*:|#)([^)\s]+)\)", re.IGNORECASE)
NOT_NO_JOBS = "This is a failed search, not \"no jobs\". Tell the main agent. Do not report that no roles exist."
# The error replies of these tools when career-lens reports a failed request, with the failure kind in brackets.
# The report reads the kind back from them (see failure_kind). Keep them the same as the replies below.
FAILED_REPLIES = {
    "read_page": re.compile(r"Could not read \S+: the page capture failed \(([a-z_]+)\)\. Use a different source\."),
    "google_search": re.compile(rf'Google search for ".*" failed \(([a-z_]+)\)\. {re.escape(NOT_NO_JOBS)}', re.DOTALL),
    "scrape_company": re.compile(r".+: the scrape failed \(([a-z_]+)\)\. This is a failed read, not \"no jobs\"\."),
    "search_linkedin_jobs": re.compile(r"LinkedIn job search .+ failed \(([a-z_]+)\)\. " + re.escape(NOT_NO_JOBS),
                                       re.DOTALL),
    "get_job_search": re.compile(r"LinkedIn job search \S+ failed \(([a-z_]+)\)\. " + re.escape(NOT_NO_JOBS),
                                 re.DOTALL),
}
JOB_SEARCH_SCHEMA = {"type": "object", "properties": {
    "keywords": {"type": "string", "description": "What to search for, e.g. \"backend engineer python\". Put the "
                                                 "place in location, not here."},
    "location": {"type": "string", "description": "One city, region or country, e.g. \"Bengaluru\"."},
    "posted_within": {"type": "string", "enum": list(POSTED_WITHIN), "description": "Only roles posted within this time."},
    "workplace_types": {"type": "array", "items": {"type": "string", "enum": list(WORKPLACE_TYPES)},
                        "description": "Only roles with these workplace types."},
    "job_pages": {"type": "integer", "minimum": 1, "maximum": MAX_JOB_PAGES,
                  "description": "Result pages to read, about 25 roles each. Default 2. Each page takes time."},
}, "required": ["keywords"]}
SEARCH_RESULT_SCHEMA = {"type": "object", "properties": {
    "request_id": {"type": "string", "description": "The request id from search_linkedin_jobs."}},
    "required": ["request_id"]}
JOB_SCHEMA = {"type": "object", "properties": {
    "url": {"type": "string", "description": "The LinkedIn job URL, e.g. https://www.linkedin.com/jobs/view/4012345678/"},
    "job_id": {"type": "string", "description": "The role of this goal that the posting is, from list_jobs. When you "
                                                "give it, the full description is saved on that role."},
}, "required": ["url"]}
DEFAULT_JOB_PAGES = 2
BLOCK_LIMIT = 3  # refused pages in one session that stop the goal
BLOCK_STOP_REASON = ("Stopped: the agent tried to open pages that are not about jobs or are on this network "
                     f"{BLOCK_LIMIT} times. Look at the Activity tab, then retry with a clearer goal.")


def failure_kind(tool: str, reply: str) -> str | None:
    """The failure kind of a failed career-lens request, read from the error reply of the tool that made it, e.g.
    "not_found". None for other tools and other replies: only the exact reply formats of this module match."""
    pattern = FAILED_REPLIES.get(tool)
    match = pattern.fullmatch(reply) if pattern else None
    return match.group(1) if match else None


def absolute_links(markdown: str, base: str) -> str:
    """Make the relative links of a page absolute. Captures from an older converter keep them."""
    return RELATIVE_LINK.sub(lambda match: f"]({urljoin(base, match.group(1))})", markdown)


def _sections(raw: Any) -> list[str] | None:
    """The sections that the agent asked for. None means the default sections."""
    if raw is None:
        return None
    names = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(names, list):
        raise ValueError("linkedin_sections must be a list of section names.")
    names = list(dict.fromkeys(str(name).strip().lower() for name in names if str(name).strip()))
    unknown = [name for name in names if name not in SECTIONS]
    if unknown:
        raise ValueError(f"Unknown sections: {', '.join(unknown)}. Use: {', '.join(SECTIONS)}.")
    return names


async def _record_read(ctx: ToolContext, url: str, body: dict) -> str:
    """Remember what a saved copy says about the jobs of a company. A copy with no jobs section says nothing."""
    state = jobs_state(body)
    if state["state"] in ("complete", "none", "partial", "absent", "failed"):
        name = ((body.get("data") or {}).get("profile") or {}).get("name")
        await ArtifactStore(ctx.store.pool, ctx.research_max_age_days).record_job_read(
            ctx.goal_id, url, name, state["state"], state["items"], state["captured_pages"], state["observed_at"])
    return state["text"]


async def _get_company(ctx: ToolContext, args: dict) -> dict:
    url = str(args.get("linkedin_url") or "").strip()
    if not url:
        return result("Give linkedin_url.", error=True)
    try:
        body = await ctx.career_lens.get_company(url)
    except CareerLensError as exc:
        return result(f"LinkedIn company {url}: could not read ({exc}).", error=True)
    if not body:
        return result(f"LinkedIn company {url}: nothing saved yet. Use scrape_company to request it.")
    await _record_read(ctx, url, body)
    return result(summarize_company(body))


async def _scrape(ctx: ToolContext, args: dict) -> dict:
    url = str(args.get("linkedin_url") or "").strip()
    if not url:
        return result("Give linkedin_url.", error=True)
    job_id = str(args.get("job_id") or "").strip() or None
    if job_id:
        try:
            uuid.UUID(job_id)
            known = await ctx.store.has_job(ctx.goal_id, job_id)
        except ValueError:
            known = False
        if not known:
            return result(f"Unknown job_id {job_id}: give the id of a role of this goal, from list_jobs.", error=True)
    try:
        sections = _sections(args.get("linkedin_sections"))
    except ValueError as exc:
        return result(str(exc), error=True)
    chosen = list(SECTIONS) if sections is None else sections
    try:
        submission = await ctx.career_lens.scrape_company(url, sections)
    except ScrapeConflict:
        return result(f"LinkedIn company {url}: career-lens is already scraping this URL with other sections. Do not "
                      "request it again now. If you queued it, wait for it with wait_for_scrapes. Then read it with "
                      "get_company.")
    except CareerLensError as exc:
        return result(f"LinkedIn company {url}: could not request a scrape ({exc}).", error=True)
    body = submission.body
    status, request_id = body.get("status"), body.get("request_id", "")
    target = (body.get("target") or {}).get("url") or url
    scope = ", ".join(chosen) or "base page only"
    if status in ACTIVE:
        await ctx.store.add_scrape(ctx.goal_id, request_id, "linkedin_company", target, True, job_id,
                                   wants_jobs="jobs" in chosen)
        await ctx.recorder.record(ctx.goal_id, "scrape_queued", request_id=request_id, kind="linkedin_company",
                                  target=target)
        return result(f"LinkedIn company {target} ({scope}): queued as {request_id}.\n{QUEUED_NOTE}")
    if status in FRESH:
        return result(f"LinkedIn company {target} ({scope}): already fresh (scraped within "
                      f"{ctx.career_lens.freshness}). Use get_company to read it.")
    reason = (body.get("failure") or {}).get("kind") or status or "unknown"
    return result(f"LinkedIn company {target}: the scrape failed ({reason}). This is a failed read, not \"no jobs\".",
                  error=True)


async def _wait(ctx: ToolContext, args: dict) -> dict:
    """Wait until the watcher marks the scrapes as finished, or until the wait limit."""
    raw = args.get("request_ids")
    ids = [str(i).strip() for i in (raw if isinstance(raw, list) else [raw]) if str(i or "").strip()]
    if not ids:
        return result("Give request_ids: the ids of your queued scrapes.", error=True)
    deadline = time.monotonic() + ctx.scrape_wait_s
    while True:
        rows = await ctx.store.scrapes_by_request(ctx.goal_id, ids)
        pending = [row for row in rows if row["status"] == "pending"]
        if not pending or time.monotonic() >= deadline:
            break
        await asyncio.sleep(ctx.poll_interval_s)
    if not rows:
        return result("No scrapes with these request ids in this goal.", error=True)
    # This agent reads the results now, so the watcher must not resume the goal for them later.
    await ctx.store.mark_reported(ctx.goal_id, [row["request_id"] for row in rows if row["status"] != "pending"])
    lines = [scrape_line(row) for row in rows]
    unknown = [i for i in ids if i not in {row["request_id"] for row in rows}]
    if unknown:
        lines.append(f"Unknown request ids: {', '.join(unknown)}.")
    if pending:
        minutes = ctx.scrape_wait_s / 60
        reasons = sorted({text for row in pending if (text := wait_text(row.get("wait_reason")))})
        why = f" Now: {'; '.join(reasons)}." if reasons else ""
        lines.append(f"Still pending after {minutes:g} minute{'' if minutes == 1 else 's'}.{why} Do not wait again. Save "
                     "what you have, list these scrapes in pending_scrapes in your report, and finish. The main agent "
                     "sends you a message when they finish. Waiting is not \"no jobs\".")
    else:
        lines.append("Read the new data with get_company, get_job_search or get_linkedin_job.")
    return result("\n".join(lines))


@contextlib.contextmanager
def _waiting(ctx: ToolContext, request_id: str) -> Iterator[None]:
    """While a tool waits on a request, a stop of the goal cancels it in career-lens."""
    ctx.waiting.add(request_id)
    try:
        yield
    finally:
        ctx.waiting.discard(request_id)


async def _read_page(ctx: ToolContext, args: dict) -> dict:
    """Capture one page in career-lens, wait for it, then answer the prompt from its Markdown."""
    url = str(args.get("url") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    if BARE_SITE.fullmatch(url):
        url = f"https://{url}"
    if not url.lower().startswith(("http://", "https://")):
        return result("Give the full URL of a web page, starting with https://.", error=True)
    if not prompt:
        return result("Give a prompt: what to find on the page.", error=True)
    if ctx.reader is None:
        return result("read_page is not configured: it has no small model to read the page.", error=True)
    if is_linkedin_job_url(url):
        return result(LINKEDIN_JOB_REFUSAL, error=True)
    if is_linkedin_url(url):
        return result(LINKEDIN_PAGE_REFUSAL, error=True)
    if reason := await asyncio.to_thread(blocked_url_reason, url, resolve=ctx.resolve):
        return await _refuse(ctx, url, reason)
    try:
        submission = await ctx.career_lens.scrape_url(url)
        request_id = submission.body["request_id"]
        with _waiting(ctx, request_id):
            page = await ctx.career_lens.url_result(request_id)
            deadline = time.monotonic() + ctx.poll_timeout_s
            while page.status_code == 202 and time.monotonic() < deadline:
                await asyncio.sleep(ctx.poll_interval_s)
                page = await ctx.career_lens.url_result(request_id)
    except CareerLensError as exc:
        return result(f"Could not read {url}: {exc}.", error=True)
    if page.status_code == 202:
        # career-lens gives the same request to the same URL while it is active, so a new call continues it.
        return result(f"The page {url} is still loading after {ctx.poll_timeout_s / 60:g} minutes "
                      f"(request {request_id}). Do other work, then call read_page again with the same URL.")
    capture = page.body.get("capture") or {}
    if capture.get("status") == "failed":
        reason = (capture.get("error") or {}).get("kind") or "unknown"
        return result(f"Could not read {url}: the page capture failed ({reason}). Use a different source.",
                      error=True)
    final = capture.get("url_final") or url
    markdown = absolute_links(capture.get("markdown") or capture.get("text") or "", final)
    if AUTH_WALL.search(final) or looks_like_sign_in(markdown):
        return result(SIGN_IN_WALL.format(url=final), error=True)
    if not markdown.strip():
        return result(f"The page {final} has no readable text. Use a different source.", error=True)
    try:
        answer = await ctx.reader.answer(final, markdown, prompt)
    except PageReaderError as exc:
        await ctx.store.add_spend(ctx.goal_id, exc.cost_usd)
        return result(f"Read {final}, but could not answer the prompt: {exc}.", error=True)
    await ctx.store.add_spend(ctx.goal_id, answer.cost_usd)  # the small model is part of the goal's spend
    return result(f"Page: {final}\n\n{answer.text}")


async def _refuse(ctx: ToolContext, url: str, reason: str) -> dict:
    """read_page refuses a blocked page. Too many refusals in one session stop the goal."""
    await ctx.recorder.record(ctx.goal_id, "site_blocked", tool="read_page", url=url, reason=reason)
    ctx.blocked += 1
    if ctx.blocked >= BLOCK_LIMIT and ctx.on_block_limit is not None:
        ctx.on_block_limit(BLOCK_STOP_REASON)
    return result(f"Refused: {url} {reason}. Agents read only pages for a job search. Use a different source.",
                  error=True)


async def _without_blocked(ctx: ToolContext, body: dict) -> tuple[dict, int]:
    """The search reply without results on blocked sites. Only the names: no DNS for each result."""
    results = body.get("results") or []
    kept = [row for row in results if not blocked_url_reason(str(row.get("url") or ""), resolve=lambda host: [])
            or not str(row.get("url") or "").strip()]
    removed = len(results) - len(kept)
    if removed:
        await ctx.recorder.record(ctx.goal_id, "site_blocked", tool="google_search", query=body.get("query", ""),
                                  count=removed)
    return {**body, "results": kept}, removed


async def _search(ctx: ToolContext, args: dict) -> dict:
    query = (args.get("query") or "").strip()
    if not query:
        return result("Give a query.", error=True)
    try:
        submission = await ctx.career_lens.search(query)
        request_id = submission.body["request_id"]
        with _waiting(ctx, request_id):
            deadline = time.monotonic() + ctx.poll_timeout_s
            while submission.status_code == 202 and time.monotonic() < deadline:
                # The search reply has no wait reason. The status of the request has it.
                wait = (await ctx.career_lens.request_status(request_id)).get("wait_reason")
                if wait in GOOGLE_OFF:
                    reason = wait_text(wait)
                    await ctx.career_lens.cancel(request_id)
                    return result(f'Google search for "{query}" cannot run: {reason}. {NOT_NO_JOBS}', error=True)
                await asyncio.sleep(ctx.poll_interval_s)
                submission = await ctx.career_lens.search_result(request_id)
    except CareerLensError as exc:
        return result(f"Google search failed: {exc}. {NOT_NO_JOBS}", error=True)
    body = submission.body
    if submission.status_code == 202:
        await ctx.store.add_scrape(ctx.goal_id, body["request_id"], "google_search", query, False)
        await ctx.recorder.record(ctx.goal_id, "scrape_queued", request_id=body["request_id"], kind="google_search",
                                  target=query)
        return result(f'Google search for "{query}" is still running; queued as {body["request_id"]}. '
                      "Do other work, then call wait_for_scrapes with this id, or search again later with "
                      "the same query.")
    if body.get("status") in ("failed", "cancelled"):
        reason = (body.get("failure") or {}).get("kind") or body.get("status")
        return result(f'Google search for "{query}" failed ({reason}). {NOT_NO_JOBS}', error=True)
    body, removed = await _without_blocked(ctx, body)
    found = format_search_results(body)
    if removed:
        found += f"\n(Removed {removed} result{'' if removed == 1 else 's'} on blocked sites.)"
    return result(found)


def _job_search_args(args: dict) -> dict:
    """The search terms that the agent gave, checked. A ValueError says what is wrong."""
    keywords = " ".join(str(args.get("keywords") or "").split())
    if not keywords:
        raise ValueError("Give keywords.")
    posted = str(args.get("posted_within") or "").strip().lower() or None
    if posted and posted not in POSTED_WITHIN:
        raise ValueError(f"posted_within must be one of: {', '.join(POSTED_WITHIN)}.")
    raw = args.get("workplace_types") or []
    types = [str(t).strip().lower().replace("-", "_") for t in (raw if isinstance(raw, list) else [raw]) if str(t).strip()]
    if unknown := [t for t in types if t not in WORKPLACE_TYPES]:
        raise ValueError(f"Unknown workplace types: {', '.join(unknown)}. Use: {', '.join(WORKPLACE_TYPES)}.")
    try:
        pages = int(args.get("job_pages") or DEFAULT_JOB_PAGES)
    except (TypeError, ValueError):
        raise ValueError(f"job_pages must be a number from 1 to {MAX_JOB_PAGES}.") from None
    if not 1 <= pages <= MAX_JOB_PAGES:
        raise ValueError(f"job_pages must be a number from 1 to {MAX_JOB_PAGES}.")
    location = " ".join(str(args.get("location") or "").split()) or None
    return {"keywords": keywords, "location": location, "posted_within": posted,
            "workplace_types": list(dict.fromkeys(types)) or None, "job_pages": pages}


def _search_failed(label: str, body: dict) -> dict:
    reason = (body.get("failure") or {}).get("cause") or (body.get("failure") or {}).get("kind") or body.get("status")
    return result(f"LinkedIn job search {label} failed ({reason or 'unknown'}). {NOT_NO_JOBS}", error=True)


async def _search_jobs(ctx: ToolContext, args: dict) -> dict:
    try:
        terms = _job_search_args(args)
    except ValueError as exc:
        return result(str(exc), error=True)
    label = f'"{terms["keywords"]}"' + (f" in {terms['location']}" if terms["location"] else "")
    try:
        submission = await ctx.career_lens.search_jobs(**terms)
    except ScrapeConflict:
        return result(f"LinkedIn job search {label}: career-lens already runs this search with other settings. Search "
                      "again later, or change the keywords.")
    except CareerLensError as exc:
        return result(f"LinkedIn job search {label}: could not request it ({exc}). {NOT_NO_JOBS}", error=True)
    body = submission.body
    status, request_id = body.get("status"), body.get("request_id", "")
    if status in ACTIVE:
        target = (body.get("target") or {}).get("url") or label
        await ctx.store.add_scrape(ctx.goal_id, request_id, "linkedin_job_search", target, True)
        await ctx.recorder.record(ctx.goal_id, "scrape_queued", request_id=request_id, kind="linkedin_job_search",
                                  target=target)
        return result(f"LinkedIn job search {label}: queued as {request_id}.\n{QUEUED_NOTE} Then read the roles "
                      "with get_job_search.")
    if status in FRESH:
        return await _job_search(ctx, {"request_id": request_id})
    return _search_failed(label, body)


async def _job_search(ctx: ToolContext, args: dict) -> dict:
    request_id = str(args.get("request_id") or "").strip()
    if not request_id:
        return result("Give request_id: the id from search_linkedin_jobs.", error=True)
    try:
        submission = await ctx.career_lens.job_search_result(request_id)
    except CareerLensError as exc:
        return result(f"LinkedIn job search {request_id}: could not read it ({exc}).", error=True)
    body = submission.body
    if submission.status_code == 202:
        return result(f"LinkedIn job search {request_id} still runs. Wait for it with wait_for_scrapes. Waiting is not "
                      "\"no jobs\".")
    if not body.get("result"):
        return _search_failed(request_id, body)
    return result(summarize_job_search(body))


async def _save_description(ctx: ToolContext, job_id: str, body: dict) -> str:
    """Save the posting's full text on the role. A sentence that says what happened."""
    job = body.get("job") or {}
    why = await ArtifactStore(ctx.store.pool, ctx.research_max_age_days).save_job_research(
        ctx.goal_id, job_id, job.get("description_text"), "full_description", job.get("job_url"))
    return f"Not saved on the role: {why}" if why else "Saved on the role as full_description."


async def _role(ctx: ToolContext, args: dict) -> tuple[str | None, dict | None]:
    """The role id that the agent gave, checked. (None, error reply) when it is wrong."""
    job_id = str(args.get("job_id") or "").strip() or None
    if job_id is None:
        return None, None
    try:
        uuid.UUID(job_id)
        known = await ctx.store.has_job(ctx.goal_id, job_id)
    except ValueError:
        known = False
    if not known:
        return None, result(f"Unknown job_id {job_id}: give the id of a role of this goal, from list_jobs.", error=True)
    return job_id, None


async def _get_job(ctx: ToolContext, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if linkedin_job_id(url) is None:
        return result("Give the URL of a LinkedIn job posting: https://www.linkedin.com/jobs/view/<number>/.",
                      error=True)
    job_id, refused = await _role(ctx, args)
    if refused:
        return refused
    try:
        body = await ctx.career_lens.get_job(url)
    except CareerLensError as exc:
        return result(f"LinkedIn job {url}: could not read ({exc}).", error=True)
    if not body:
        return result(f"LinkedIn job {url}: nothing saved yet. Use scrape_linkedin_job to request it.")
    text = summarize_job(body)
    if job_id:
        text += "\n\n" + await _save_description(ctx, job_id, body)
    return result(text)


async def _scrape_job(ctx: ToolContext, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if linkedin_job_id(url) is None:
        return result("Give the URL of a LinkedIn job posting: https://www.linkedin.com/jobs/view/<number>/.",
                      error=True)
    job_id, refused = await _role(ctx, args)
    if refused:
        return refused
    try:
        submission = await ctx.career_lens.scrape_job(url)
    except ScrapeConflict:
        return result(f"LinkedIn job {url}: career-lens already reads it. Wait, then use get_linkedin_job.")
    except CareerLensError as exc:
        return result(f"LinkedIn job {url}: could not request a read ({exc}).", error=True)
    body = submission.body
    status, request_id = body.get("status"), body.get("request_id", "")
    target = (body.get("target") or {}).get("url") or url
    if status in ACTIVE:
        await ctx.store.add_scrape(ctx.goal_id, request_id, "linkedin_job", target, True, job_id)
        await ctx.recorder.record(ctx.goal_id, "scrape_queued", request_id=request_id, kind="linkedin_job",
                                  target=target)
        return result(f"LinkedIn job {target}: queued as {request_id}.\n{QUEUED_NOTE} Then read it with "
                      "get_linkedin_job.")
    if status in FRESH:
        return result(f"LinkedIn job {target}: already fresh. Use get_linkedin_job to read it.")
    reason = (body.get("failure") or {}).get("kind") or status or "unknown"
    return result(f"LinkedIn job {target}: the read failed ({reason}). Use the snippet, or find another copy of the "
                  "role.", error=True)


def build_tools(ctx: ToolContext) -> list[SdkMcpTool]:
    read_only = ToolAnnotations(readOnlyHint=True)

    @tool("google_search",
          "Search Google through career-lens. Returns up to 10 ranked results with snippets. Accepts "
          "operators such as site: and quotes, e.g. 'site:linkedin.com/jobs/view \"Backend Engineer\" \"Pune\"'.",
          {"query": str}, annotations=read_only)
    async def google_search(args: dict[str, Any]) -> dict[str, Any]:
        return await _search(ctx, args)

    @tool("read_page",
          "Read one web page in a real browser through career-lens. A small model reads the page and answers your "
          "prompt. Not for LinkedIn: use get_linkedin_job for a LinkedIn job posting.",
          PAGE_SCHEMA, annotations=read_only)
    async def read_page(args: dict[str, Any]) -> dict[str, Any]:
        return await _read_page(ctx, args)

    @tool("get_company",
          "Read the saved copy of a LinkedIn company (instant, free): its facts, the state of its jobs section, and "
          "its roles. Says 'nothing saved yet' when a scrape is needed.",
          COMPANY_SCHEMA, annotations=read_only)
    async def get_company(args: dict[str, Any]) -> dict[str, Any]:
        return await _get_company(ctx, args)

    @tool("scrape_company",
          "Ask career-lens to scrape a LinkedIn company fresh (slow, budgeted). It runs only in the working hours of "
          "the accounts. Use it only when the saved copy is missing or old.", SCRAPE_SCHEMA)
    async def scrape_company(args: dict[str, Any]) -> dict[str, Any]:
        return await _scrape(ctx, args)

    @tool("wait_for_scrapes",
          "Wait until queued scrapes finish, for a limited time. Then read the new data with get_company, "
          "get_job_search or get_linkedin_job.",
          WAIT_SCHEMA, annotations=read_only)
    async def wait_for_scrapes(args: dict[str, Any]) -> dict[str, Any]:
        return await _wait(ctx, args)

    @tool("search_linkedin_jobs",
          "Search LinkedIn jobs by keywords, signed in, through career-lens (slow, budgeted). It runs only in the "
          "working hours of the accounts. Returns a request id. Wait with wait_for_scrapes, then read the roles with "
          "get_job_search.", JOB_SEARCH_SCHEMA)
    async def search_linkedin_jobs(args: dict[str, Any]) -> dict[str, Any]:
        return await _search_jobs(ctx, args)

    @tool("get_job_search",
          "Read the roles that a LinkedIn job search found. Says if the search still runs, failed, or did not read "
          "all pages.", SEARCH_RESULT_SCHEMA, annotations=read_only)
    async def get_job_search(args: dict[str, Any]) -> dict[str, Any]:
        return await _job_search(ctx, args)

    @tool("get_linkedin_job",
          "Read the saved copy of a LinkedIn job posting (instant, free): its facts and full description. Give job_id "
          "to save the description on that role. Says 'nothing saved yet' when a read is needed.",
          JOB_SCHEMA, annotations=read_only)
    async def get_linkedin_job(args: dict[str, Any]) -> dict[str, Any]:
        return await _get_job(ctx, args)

    @tool("scrape_linkedin_job",
          "Ask career-lens to read a LinkedIn job posting signed in (slow, budgeted). Use it only when "
          "get_linkedin_job has nothing saved.", JOB_SCHEMA)
    async def scrape_linkedin_job(args: dict[str, Any]) -> dict[str, Any]:
        return await _scrape_job(ctx, args)

    return [google_search, read_page, get_company, scrape_company, wait_for_scrapes, search_linkedin_jobs,
            get_job_search, get_linkedin_job, scrape_linkedin_job]


def build_server(ctx: ToolContext):
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=build_tools(ctx))
