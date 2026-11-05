"""A small async client for the consumer API of career-lens (/v1), and what its replies mean for job search."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

FRESHNESS = "7d"  # default: career-lens reuses a result newer than this instead of a new capture
CANCEL_TIMEOUT_S = 5.0  # a stop waits at most this long for career-lens
RATE_LIMIT_RETRIES = 3  # after a 429, the tries again of one call
MAX_RATE_LIMIT_WAIT_S = 10.0  # the longest wait before a try again, whatever Retry-After says
SECTIONS = ("about", "jobs")  # the LinkedIn company sections that job-finder uses
POSTED_WITHIN = ("day", "week", "month")  # the time filters of a LinkedIn job search
WORKPLACE_TYPES = ("on_site", "hybrid", "remote")
MAX_JOB_PAGES = 20  # career-lens reads 1 to 20 result pages; about 25 roles each
GOOGLE_OFF = {"no_google_capable_workers", "google_budget_exhausted"}
WAIT_TEXTS = {
    "accounts_off_shift": "LinkedIn accounts are off shift",
    "shift_not_planned": "LinkedIn accounts have no shift planned",
    "all_workers_busy": "All career-lens workers are busy",
    "accounts_cooling_down": "LinkedIn accounts wait between actions",
    "minimum_interval": "LinkedIn accounts wait between actions",
    "budgets_exhausted": "LinkedIn accounts used their daily budget",
    "no_google_capable_workers": "Google search has no worker",
    "google_budget_exhausted": "Google search used its budget",
}


def wait_text(reason: str | None) -> str | None:
    """A wait reason as plain words. Other text from career-lens stays as it is."""
    return WAIT_TEXTS.get(reason, reason) if reason else None


def jobs_state(body: dict | None) -> dict:
    """What a company reply says about its jobs section. state is one of: complete (all of the list), none (a complete
    read with no roles), absent (no jobs section), partial, not_requested, not_captured, failed. Only none and absent
    mean no jobs. `text` is the sentence that the agent reads."""
    body = body or {}
    meta = (body.get("meta") or {}).get("jobs") or {}
    items = [item for item in (((body.get("data") or {}).get("jobs") or {}).get("data") or {}).get("items") or []
             if isinstance(item, dict)]
    status, pages = meta.get("status"), meta.get("captured_pages")
    status = "absent" if status == "not_available" else status  # career-lens: this company has no such tab
    if status == "parsed" or status == "partial":
        if status == "partial" or meta.get("reached_end") is not True:
            state = "partial"
        else:
            state = "complete" if items else "none"
    elif status in ("absent", "failed", "not_requested"):
        state = status
    elif status is None:
        state = "not_requested" if body and not body.get("meta") else "not_captured"
    else:
        state = "not_captured"
    observed = meta.get("observed_at") or body.get("observed_at")
    shown = f"{pages} page{'' if pages == 1 else 's'}" if pages else "some pages"
    text = {
        "complete": f"Jobs section: read to the end, {len(items)} roles.",
        "none": "Jobs section: read to the end, no open roles.",
        "absent": "Jobs section: LinkedIn shows no jobs section for this company. It has no open roles there.",
        "partial": f"Jobs section: PARTIAL. {len(items)} roles from {shown}. It is not the full list. Do not say that "
                   "these are all the roles.",
        "not_captured": "Jobs section: NOT READ. The request can succeed without this section. This is a failed read, "
                        "not \"no jobs\".",
        "failed": "Jobs section: the read FAILED. This is a failed read, not \"no jobs\".",
        "not_requested": "Jobs section: not requested yet. This says nothing about open roles.",
    }[state]
    return {"state": state, "items": len(items), "captured_pages": pages, "observed_at": observed, "text": text}


class CareerLensError(Exception):
    """A failure worth telling Claude or the user about; never contains the API key."""


class ScrapeConflict(CareerLensError):
    """career-lens has a pending scrape of the same URL with other sections (HTTP 409)."""


@dataclass
class Submission:
    status_code: int
    body: dict[str, Any]


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = None
    return detail if isinstance(detail, str) else f"HTTP {response.status_code}"


def _retry_after(response: httpx.Response) -> float:
    try:
        seconds = float(response.headers.get("Retry-After", ""))
    except ValueError:
        return 1.0
    return min(max(seconds, 0.0), MAX_RATE_LIMIT_WAIT_S) if seconds == seconds else 1.0


class CareerLens:
    def __init__(self, base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None,
                 freshness: str = FRESHNESS, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._sleep = sleep
        self.configured = bool(base_url and api_key)
        self.freshness = freshness
        self._client = client or httpx.AsyncClient(
            base_url=base_url or "http://unconfigured.invalid",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(35.0, connect=10.0),
        )

    async def health(self) -> str:
        """Check API availability. This endpoint does not validate the customer key."""
        if not self.configured:
            return "not_configured"
        try:
            response = await self._client.get("/healthz", timeout=3.0, follow_redirects=False)
            if response.status_code in (401, 403):
                return "access_denied"
            if response.status_code != 200:
                return "error"
            return "online" if response.json() == {"status": "ok"} else "error"
        except httpx.RequestError:
            return "unreachable"
        except ValueError:
            return "error"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, *, allow_404: bool = False, **kwargs: Any) -> httpx.Response:
        if not self.configured:
            raise CareerLensError("career-lens is not configured (set CAREER_LENS_BASE_URL and CAREER_LENS_API_KEY)")
        for retry in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                raise CareerLensError("career-lens is unreachable") from exc
            if response.status_code != 429:
                break
            if retry == RATE_LIMIT_RETRIES:
                raise CareerLensError("career-lens refused too many calls. Try again in a minute.")
            # career-lens refuses a call before it does any work, so a second try of a POST is safe.
            await self._sleep(_retry_after(response))
        if response.status_code in (401, 403):
            raise CareerLensError("career-lens rejected the API key")
        if response.status_code == 404 and allow_404:
            return response
        if response.status_code >= 500:
            raise CareerLensError(f"career-lens error (HTTP {response.status_code})")
        if response.status_code == 409:
            raise ScrapeConflict(_detail(response))
        if response.status_code >= 400:
            raise CareerLensError(f"career-lens refused the request: {_detail(response)}")
        return response

    async def search(self, query: str) -> Submission:
        """Submit a search. career-lens answers at once with the request id, so a stop can cancel the search while
        it waits in the queue. Its default holds the answer up to 20 seconds, for the results."""
        response = await self._call("POST", "/v1/search", json={
            "query": query, "wait_seconds": 0, "priority": "high", "freshness": self.freshness})
        return Submission(response.status_code, response.json())

    async def search_result(self, request_id: str) -> Submission:
        response = await self._call("GET", f"/v1/search/{request_id}")
        return Submission(response.status_code, response.json())

    async def scrape_url(self, url: str) -> Submission:
        """Ask for a capture of one web page in a real browser."""
        response = await self._call("POST", "/v1/scrape/url", json={"url": url, "freshness": self.freshness})
        return Submission(response.status_code, response.json())

    async def url_result(self, request_id: str) -> Submission:
        """The page capture with its Markdown. HTTP 202 while the capture is pending."""
        response = await self._call("GET", f"/v1/url/{request_id}", params={"include_markdown": "true"})
        return Submission(response.status_code, response.json())

    async def get_company(self, url: str) -> dict | None:
        """The saved copy of a LinkedIn company. None when career-lens has none."""
        response = await self._call("GET", "/v1/linkedin-company", params={"url": url}, allow_404=True)
        return None if response.status_code == 404 else response.json()

    async def scrape_company(self, url: str, sections: list[str] | None = None) -> Submission:
        """Ask for a scrape of a LinkedIn company. None asks for all the sections that job-finder uses."""
        response = await self._call("POST", "/v1/scrape/linkedin-company", json={
            "url": url, "freshness": self.freshness,
            "linkedin_sections": list(SECTIONS) if sections is None else sections})
        return Submission(response.status_code, response.json())

    async def search_jobs(self, keywords: str, *, location: str | None = None, posted_within: str | None = None,
                          workplace_types: list[str] | None = None, job_pages: int | None = None) -> Submission:
        """Ask for a LinkedIn keyword job search. It runs signed in, on a career-lens account, so it is slow."""
        body: dict[str, Any] = {"keywords": keywords, "freshness": self.freshness}
        for name, value in (("location", location), ("posted_within", posted_within),
                            ("workplace_types", workplace_types), ("job_pages", job_pages)):
            if value:
                body[name] = value
        response = await self._call("POST", "/v1/scrape/linkedin-job-search", json=body)
        return Submission(response.status_code, response.json())

    async def job_search_result(self, request_id: str) -> Submission:
        """One job search: HTTP 202 while it runs, else its status, failure and result."""
        response = await self._call("GET", f"/v1/linkedin-job-search/{request_id}")
        return Submission(response.status_code, response.json())

    async def get_job(self, url: str) -> dict | None:
        """The newest saved reading of a LinkedIn job posting, with its full text. None when career-lens has none."""
        response = await self._call("GET", "/v1/linkedin-job", params={"url": url}, allow_404=True)
        return None if response.status_code == 404 else response.json()

    async def scrape_job(self, url: str) -> Submission:
        """Ask for a signed-in read of one LinkedIn job posting."""
        response = await self._call("POST", "/v1/scrape/linkedin-job", json={"url": url, "freshness": self.freshness})
        return Submission(response.status_code, response.json())

    async def cancel(self, request_id: str, timeout_s: float = CANCEL_TIMEOUT_S) -> str | None:
        """Withdraw our interest in a request. career-lens stops the work when no other caller wants it.

        The answer: "cancelled", "stopping" (the worker stops at the next page) or "withdrawn" (another caller
        still wants the work). None when there is nothing to cancel, because the request finished or is not
        ours, and when career-lens does not answer. A cancel never fails the caller: our scrape is cancelled
        already, and a late result is not read."""
        if not self.configured:
            return None
        try:
            response = await self._client.post(f"/v1/requests/{request_id}/cancel", timeout=timeout_s)
            return response.json().get("cancel") if response.status_code in (200, 202) else None
        except (httpx.RequestError, ValueError):
            return None

    async def cancel_all(self, request_ids: Iterable[str]) -> dict[str, str | None]:
        """Cancel the requests at the same time. Each request id with its answer, as from cancel()."""
        ids = list(dict.fromkeys(request_ids))
        return dict(zip(ids, await asyncio.gather(*(self.cancel(i) for i in ids)), strict=True))

    async def request_status(self, request_id: str) -> dict:
        response = await self._call("GET", f"/v1/requests/{request_id}")
        return response.json()
