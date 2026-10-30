"""SQL for the work that agents share: job roles, companies, company facts, verdicts and application notes."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from .jobs import company_key, job_key, job_source, linkedin_job_id, role_key, usable_description
from .store import Queries

VERDICTS = ("fit", "partial", "not_fit", "unsure")
SOURCES = ("linkedin_company", "linkedin_search", "google", "job_board", "company_site")
EVIDENCE = ("listing_only", "snippet", "full_description")  # from weak to strong
LISTING_CANNOT_FIT = ("A role with listing_only evidence cannot be fit, because no agent read the job. Use partial or "
                      "unsure, and say why.")
ONLY_FIT_NOTES = "An application note is only for a role with the verdict fit."
CHECK_AGAIN = "(fit = 'unsure' OR (fit = 'partial' AND evidence_level = 'listing_only'))"
# A role waits for its scrapes before fit-check: while a scrape is pending, and after the data came until research
# saves again. Each scrape blocks for this time at most, so a slow or stuck scrape does not stop the goal.
SCRAPE_WAIT = "10 minutes"
# A goal's roles, with the stages that come from the data.
JOBS = f"""
    SELECT j.id::text AS id, j.company_id::text AS company_id, j.title, j.company_name, j.location, j.workplace_type,
           j.posted_at, j.posting_status, j.url, j.source, j.snippet, j.evidence_level, j.fit, j.fit_reasons,
           j.fit_count, j.fit_at, j.observed_at, j.created_at, j.updated_at,
           j.researched_at IS NOT NULL AS researched,
           coalesce(j.researched_at > j.fit_at, false) AS researched_since_fit,
           EXISTS (SELECT 1 FROM application_notes a WHERE a.job_id = j.id) AS noted,
           EXISTS (SELECT 1 FROM scrapes s WHERE s.job_id = j.id AND (
               (s.status = 'pending' AND s.submitted_at > now() - interval '{SCRAPE_WAIT}')
               OR (s.status = 'succeeded' AND s.finished_at > now() - interval '{SCRAPE_WAIT}'
                   AND NOT coalesce(j.researched_at > s.finished_at, false))
           )) AS waiting_scrapes
    FROM jobs j
"""
JOB_FILTERS = {
    "all": "true",
    "needs_research": "NOT researched",
    # An unsure role, or a partial role that no agent read, goes back to research one time, after its first verdict.
    "research_again": f"{CHECK_AGAIN} AND fit_count = 1 AND NOT researched_since_fit",
    "needs_fit": f"researched AND NOT waiting_scrapes AND (fit IS NULL OR ({CHECK_AGAIN} AND researched_since_fit))",
    "fit": "fit = 'fit'",
    "partial": "fit = 'partial'",
    "needs_note": "fit = 'fit' AND NOT noted",
}
# The filters that show the roles that wait for a subagent. get_pipeline counts them.
WORK_FILTERS = ("needs_research", "research_again", "needs_fit", "needs_note")
MANDATE_REFUSAL = "Found by an earlier goal of this mandate."


def normalize_domain(website: str) -> str | None:
    """'https://www.Acme.com/about' -> 'acme.com'. None when the text has no host name with a dot."""
    text = (website or "").strip().lower()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    try:
        host = urlsplit(text).hostname or ""
    except ValueError:
        return None
    host = host.removeprefix("www.")
    return host if "." in host else None


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _rank(level: str | None) -> int:
    return EVIDENCE.index(level) if level in EVIDENCE else -1


def _better(new_level: str, new_url: str, old: dict) -> bool:
    """True when a new copy of a role is better. Stronger evidence wins. With equal evidence, a link that an agent can
    read wins over a LinkedIn link."""
    if _rank(new_level) != _rank(old["evidence_level"]):
        return _rank(new_level) > _rank(old["evidence_level"])
    return linkedin_job_id(old["url"]) is not None and linkedin_job_id(new_url) is None


class ArtifactStore(Queries):
    def __init__(self, pool, research_max_age_days: int = 30):
        super().__init__(pool)
        self.max_age = timedelta(days=research_max_age_days)

    # Roles

    async def save_jobs(self, goal_id: str, items: list[dict], created_by: str) -> list[dict]:
        """Save each role, or say why not. Each row of the reply has `job_id` and `new`, or `refused`, or `error`."""
        goal = await self._one("SELECT mandate_id::text AS mandate_id FROM goals WHERE id = %s", (goal_id,))
        mandate_id = goal["mandate_id"] if goal else None
        return [await self._save_job(goal_id, mandate_id, item, created_by) for item in items]

    async def _save_job(self, goal_id: str, mandate_id: str | None, item: dict, created_by: str) -> dict:
        url, title, company = _text(item.get("url")), _text(item.get("title")), _text(item.get("company_name"))
        if not (url and title and company):
            return {"title": title, "url": url, "error": "A role needs url, title and company_name."}
        location = _text(item.get("location"))
        source = item.get("source") if item.get("source") in SOURCES else job_source(url)
        snippet = _text(item.get("snippet"))
        evidence = "snippet" if snippet else "listing_only"
        key, role = job_key(url), role_key(company, title, location)
        shown = {"title": title, "company_name": company}
        same = await self._one(
            "SELECT id::text AS id, url, evidence_level FROM jobs WHERE goal_id = %s AND job_key = %s", (goal_id, key))
        if same:
            return {**shown, "job_id": same["id"], "new": False, "refused": "Already saved in this goal."}
        twin = await self._one(
            "SELECT id::text AS id, url, evidence_level FROM jobs WHERE goal_id = %s AND role_key = %s",
            (goal_id, role))
        if twin:
            if _better(evidence, url, twin):
                await self._one("""
                    UPDATE jobs SET url = %s, job_key = %s, linkedin_job_id = coalesce(%s, linkedin_job_id),
                           source = %s, snippet = coalesce(%s, snippet), evidence_level = %s, updated_at = now()
                    WHERE id = %s RETURNING id
                """, (url, key, linkedin_job_id(url), source, snippet, evidence, twin["id"]))
                return {**shown, "job_id": twin["id"], "new": False,
                        "refused": "The same role is saved already. This copy is better, so the saved role now uses it."}
            return {**shown, "job_id": twin["id"], "new": False,
                    "refused": "The same role is saved already, with evidence that is as good or better."}
        if mandate_id and await self._in_mandate(goal_id, mandate_id, key, role):
            await self.save_rejected(goal_id, [{"title": title, "company_name": company, "url": url,
                                                "reason": MANDATE_REFUSAL}], created_by)
            return {**shown, "refused": MANDATE_REFUSAL}
        company_row = await self._one("SELECT id::text AS id FROM companies WHERE name_key = %s ORDER BY created_at LIMIT 1",
                                      (company_key(company),))
        row = await self._one("""
            INSERT INTO jobs (goal_id, job_key, role_key, linkedin_job_id, url, source, source_url, title, company_name,
                              company_id, location, workplace_type, posted_at, posting_status, snippet, evidence_level,
                              observed_at, found_note, created_by)
            VALUES (%(goal)s, %(key)s, %(role)s, %(li)s, %(url)s, %(source)s, %(source_url)s, %(title)s, %(company)s,
                    %(company_id)s, %(location)s, %(workplace)s, %(posted)s, %(status)s, %(snippet)s, %(evidence)s,
                    coalesce(%(observed)s::timestamptz, now()), %(note)s, %(by)s)
            ON CONFLICT DO NOTHING RETURNING id::text AS id
        """, {"goal": goal_id, "key": key, "role": role, "li": linkedin_job_id(url), "url": url, "source": source,
              "source_url": _text(item.get("source_url")), "title": title, "company": company,
              "company_id": company_row["id"] if company_row else None, "location": location,
              "workplace": _text(item.get("workplace_type")), "posted": _text(item.get("posted_at")),
              "status": _text(item.get("posting_status")), "snippet": snippet, "evidence": evidence,
              "observed": _text(item.get("observed_at")), "note": _text(item.get("note")), "by": created_by})
        if row is None:  # another call saved the same role just now
            return {**shown, "new": False, "refused": "Already saved in this goal."}
        return {**shown, "job_id": row["id"], "new": True}

    async def _in_mandate(self, goal_id: str, mandate_id: str, key: str, role: str) -> bool:
        row = await self._one("""
            SELECT 1 AS found FROM jobs j JOIN goals g ON g.id = j.goal_id
            WHERE g.mandate_id = %s AND j.goal_id <> %s AND (j.job_key = %s OR j.role_key = %s) LIMIT 1
        """, (mandate_id, goal_id, key, role))
        return row is not None

    async def save_rejected(self, goal_id: str, items: list[dict], created_by: str) -> int:
        """Save the roles that an agent examined and did not take. A second rejection updates the reason."""
        count = 0
        for item in items:
            title, reason = _text(item.get("title")), _text(item.get("reason"))
            if not title or not reason:
                continue
            url = _text(item.get("url"))
            await self._one("""
                INSERT INTO rejected_jobs (goal_id, job_key, url, title, company_name, reason, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (goal_id, job_key) WHERE job_key IS NOT NULL
                DO UPDATE SET title = EXCLUDED.title, reason = EXCLUDED.reason, created_at = now()
                RETURNING id
            """, (goal_id, job_key(url) if url else None, url, title, _text(item.get("company_name")), reason,
                  created_by))
            count += 1
        return count

    async def list_rejected(self, goal_id: str) -> list[dict]:
        return await self._all("""
            SELECT title, company_name, url, reason, created_by, created_at FROM rejected_jobs
            WHERE goal_id = %s ORDER BY created_at
        """, (goal_id,))

    async def copy_jobs(self, from_goal: str, to_goal: str, created_by: str) -> int:
        """Copy the roles of a goal, without their verdicts: to test one subagent alone."""
        rows = await self._all("""
            INSERT INTO jobs (goal_id, job_key, role_key, linkedin_job_id, url, source, source_url, title, company_name,
                              company_id, location, workplace_type, posted_at, posting_status, snippet,
                              description_text, evidence_level, observed_at, researched_at, found_note, created_by)
            SELECT %(to)s, job_key, role_key, linkedin_job_id, url, source, source_url, title, company_name, company_id,
                   location, workplace_type, posted_at, posting_status, snippet, description_text, evidence_level,
                   observed_at, researched_at, 'copied from goal ' || %(from)s::text, %(by)s
            FROM jobs WHERE goal_id = %(from)s::uuid
            ON CONFLICT DO NOTHING RETURNING id
        """, {"to": to_goal, "from": from_goal, "by": created_by})
        return len(rows)

    async def list_jobs(self, goal_id: str, filter: str = "all") -> list[dict]:
        return await self._all(
            f"SELECT * FROM ({JOBS} WHERE j.goal_id = %(goal)s) l WHERE {JOB_FILTERS[filter]} ORDER BY created_at",
            {"goal": goal_id})

    async def fit_order(self, goal_id: str) -> list[str]:
        """The ids of the goal's fit roles, in the order that fit-check confirmed them."""
        rows = await self._all("SELECT id::text AS id FROM jobs WHERE goal_id = %s AND fit = 'fit' ORDER BY fit_at, id",
                               (goal_id,))
        return [row["id"] for row in rows]

    async def pipeline(self, goal_id: str) -> dict:
        return await self._one(f"""
            SELECT count(*)::int AS jobs,
                   count(*) FILTER (WHERE researched)::int AS researched,
                   count(*) FILTER (WHERE fit IS NOT NULL)::int AS checked,
                   count(*) FILTER (WHERE fit = 'fit')::int AS fit,
                   count(*) FILTER (WHERE fit = 'partial')::int AS partial,
                   count(*) FILTER (WHERE fit = 'not_fit')::int AS not_fit,
                   count(*) FILTER (WHERE fit = 'unsure')::int AS unsure,
                   count(*) FILTER (WHERE noted)::int AS noted,
                   {", ".join(f"count(*) FILTER (WHERE {JOB_FILTERS[name]})::int AS {name}" for name in WORK_FILTERS)},
                   count(*) FILTER (WHERE waiting_scrapes)::int AS waiting_scrapes,
                   (SELECT (brief->>'target_count')::int FROM goals WHERE id = %(goal)s) AS target,
                   (SELECT profile_ready FROM goals WHERE id = %(goal)s) AS profile_ready
            FROM ({JOBS} WHERE j.goal_id = %(goal)s) l
        """, {"goal": goal_id})

    async def get_job(self, goal_id: str, job_id: str, *, fresh: bool = False) -> dict | None:
        """Everything saved for one role. `fresh`: only company facts newer than the age limit, for the agents. The
        report shows all that the goal used."""
        job = await self._one("""
            SELECT j.role_key, j.id::text AS id, j.goal_id::text AS goal_id, j.url, j.source, j.source_url, j.title, j.company_name,
                   j.company_id::text AS company_id, j.location, j.workplace_type, j.posted_at, j.posting_status,
                   j.snippet, j.description_text, j.evidence_level, j.observed_at, j.found_note, j.researched_at,
                   j.fit, j.fit_reasons, j.fit_evidence, j.fit_at, j.fit_count, j.created_at,
                   c.name AS company, c.website, c.linkedin_url
            FROM jobs j LEFT JOIN companies c ON c.id = j.company_id WHERE j.goal_id = %s AND j.id = %s
        """, (goal_id, job_id))
        if job is None:
            return None
        newer = "now() - %(max_age)s" if fresh else "'-infinity'"
        job["research"] = await self._all(f"""
            SELECT DISTINCT ON (r.kind) r.kind, r.data, r.sources, r.created_by, r.created_at,
                   r.goal_id::text AS goal_id, coalesce(g.brief->>'title', g.title) AS goal_title
            FROM company_research r LEFT JOIN goals g ON g.id = r.goal_id
            WHERE r.company_id = %(company)s AND r.created_at > {newer} ORDER BY r.kind, r.created_at DESC
        """, {"company": job["company_id"], "max_age": self.max_age}) if job["company_id"] else []
        job["note"] = await self._one("""
            SELECT why_match, resume_points, gaps, created_by, created_at FROM application_notes WHERE job_id = %s
        """, (job_id,))
        return job

    async def job_cards(self) -> list[dict]:
        """The roles of all goals, newest first, with what a card and its filters need."""
        return await self._all("""
            SELECT j.id::text AS id, j.goal_id::text AS goal_id, coalesce(g.brief->>'title', g.title) AS goal_title,
                   g.mandate_id::text AS mandate_id, j.title, j.company_name, j.location, j.workplace_type, j.posted_at,
                   j.posting_status, j.url, j.source, j.evidence_level, j.observed_at, j.fit, j.fit_reasons,
                   j.researched_at IS NOT NULL AS researched, j.created_at
            FROM jobs j JOIN goals g ON g.id = j.goal_id ORDER BY j.created_at DESC LIMIT 1000
        """)

    # Other goals (read only, for the main agent)

    async def list_goals(self, goal_id: str, limit: int) -> list[dict]:
        """The other goals, newest first, with their counts. A mandate chat is not a goal with roles."""
        return await self._all("""
            SELECT g.id::text AS id, coalesce(g.brief->>'title', g.title) AS title, g.brief->>'criteria' AS criteria,
                   g.status, g.created_at, m.title AS mandate,
                   count(j.id)::int AS jobs,
                   count(j.id) FILTER (WHERE j.fit = 'fit')::int AS fit,
                   count(j.id) FILTER (WHERE j.fit = 'partial')::int AS partial,
                   count(j.id) FILTER (WHERE j.fit = 'not_fit')::int AS not_fit
            FROM goals g LEFT JOIN mandates m ON m.id = g.mandate_id LEFT JOIN jobs j ON j.goal_id = g.id
            WHERE g.id <> %s AND NOT EXISTS (SELECT 1 FROM mandates h WHERE h.home_goal_id = g.id)
            GROUP BY g.id, m.title ORDER BY g.created_at DESC LIMIT %s
        """, (goal_id, limit))

    async def goal_jobs(self, goal_id: str) -> list[dict]:
        """The roles of one goal: each with its verdict and reasons."""
        return await self._all("""
            SELECT title, company_name, location, url, fit, fit_reasons FROM jobs WHERE goal_id = %s ORDER BY created_at
        """, (goal_id,))

    # Research (description per goal, company facts shared across goals)

    async def save_job_research(self, goal_id: str, job_id: str, description: str | None, evidence: str,
                                source_url: str | None) -> str | None:
        """What research found for one role. Why it cannot save, or None when it saved. The evidence never goes down: a
        later read that found less keeps the earlier text."""
        if evidence == "full_description" and (why := usable_description(description)):
            return f"{why} Save the role with the evidence that you have."
        row = await self._one("""
            UPDATE jobs SET
                description_text = CASE WHEN %(text)s::text IS NOT NULL THEN %(text)s ELSE description_text END,
                evidence_level = CASE WHEN array_position(%(levels)s::text[], %(evidence)s)
                                           >= array_position(%(levels)s::text[], evidence_level)
                                      THEN %(evidence)s ELSE evidence_level END,
                source_url = coalesce(%(source)s, source_url), researched_at = now(), updated_at = now()
            WHERE id = %(job)s AND goal_id = %(goal)s RETURNING id
        """, {"text": description if evidence == "full_description" else None, "levels": list(EVIDENCE),
              "evidence": evidence, "source": source_url, "job": job_id, "goal": goal_id})
        return None if row else "Unknown job_id for this goal. Use list_jobs to find the ids."

    async def save_company(self, goal_id: str, job_id: str, *, name: str | None, website: str | None,
                           linkedin_url: str | None, data: dict, sources: list, created_by: str) -> str | None:
        """Add or update the company of a role, link the goal's roles of that company, and save its facts. The company
        id, or None when the role is not in this goal."""
        job = await self._one("SELECT company_name, company_id::text AS company_id FROM jobs WHERE goal_id = %s AND id = %s",
                              (goal_id, job_id))
        if job is None:
            return None
        name = name or job["company_name"]
        key, domain = company_key(name), normalize_domain(website or "")
        found = await self._one("""
            SELECT id::text AS id FROM companies WHERE (%(li)s::text IS NOT NULL AND linkedin_url = %(li)s)
                OR (%(domain)s::text IS NOT NULL AND domain = %(domain)s) OR name_key = %(key)s
            ORDER BY (linkedin_url = %(li)s) DESC NULLS LAST, (domain = %(domain)s) DESC NULLS LAST, created_at LIMIT 1
        """, {"li": linkedin_url, "domain": domain, "key": key})
        if found:
            row = await self._one("""
                UPDATE companies SET website = coalesce(%(website)s, website), updated_at = now(),
                    domain = CASE WHEN domain IS NULL AND NOT EXISTS (SELECT 1 FROM companies o WHERE o.domain = %(domain)s)
                                  THEN %(domain)s ELSE domain END,
                    linkedin_url = CASE WHEN linkedin_url IS NULL AND NOT EXISTS (SELECT 1 FROM companies o
                                                                                  WHERE o.linkedin_url = %(li)s)
                                        THEN %(li)s ELSE linkedin_url END
                WHERE id = %(id)s RETURNING id::text AS id
            """, {"website": website, "domain": domain, "li": linkedin_url, "id": found["id"]})
        else:
            row = await self._one("""
                INSERT INTO companies (name, name_key, domain, website, linkedin_url) VALUES (%s, %s, %s, %s, %s)
                RETURNING id::text AS id
            """, (name, key, domain, website, linkedin_url))
        await self._all("""
            UPDATE jobs SET company_id = %s, updated_at = now() WHERE goal_id = %s AND company_id IS NULL
              AND (id = %s OR lower(company_name) = lower(%s)) RETURNING id
        """, (row["id"], goal_id, job_id, job["company_name"]))
        await self._one("""
            INSERT INTO company_research (company_id, kind, data, sources, created_by, goal_id)
            VALUES (%s, 'facts', %s, %s, %s, %s) RETURNING id
        """, (row["id"], Jsonb(data), Jsonb(sources), created_by, goal_id))
        return row["id"]

    # Reads of a company's jobs section (per goal)

    async def record_job_read(self, goal_id: str, company_url: str, company_name: str | None, state: str, items: int = 0,
                              captured_pages: int | None = None, observed_at: str | None = None) -> None:
        await self._one("""
            INSERT INTO company_job_reads (goal_id, company_url, company_name, state, items, captured_pages, observed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (goal_id, company_url) DO UPDATE SET
                company_name = coalesce(EXCLUDED.company_name, company_job_reads.company_name),
                state = EXCLUDED.state, items = EXCLUDED.items, captured_pages = EXCLUDED.captured_pages,
                observed_at = EXCLUDED.observed_at, updated_at = now()
            RETURNING goal_id
        """, (goal_id, company_url, company_name, state, items, captured_pages, observed_at))

    async def list_job_reads(self, goal_id: str) -> list[dict]:
        return await self._all("""
            SELECT company_url, company_name, state, items, captured_pages, observed_at FROM company_job_reads
            WHERE goal_id = %s ORDER BY updated_at
        """, (goal_id,))

    # Verdicts and application notes (per goal)

    async def set_fit(self, goal_id: str, job_id: str, verdict: str, reasons: str, evidence: list) -> dict:
        """{"title": ...} when saved, else {"error": ...}. A role that no agent read cannot be fit."""
        job = await self._one("SELECT evidence_level FROM jobs WHERE goal_id = %s AND id = %s", (goal_id, job_id))
        if job is None:
            return {"error": "Unknown job_id for this goal. Use list_jobs to find the ids."}
        if verdict == "fit" and job["evidence_level"] == "listing_only":
            return {"error": LISTING_CANNOT_FIT}
        return await self._one("""
            UPDATE jobs SET fit = %s, fit_reasons = %s, fit_evidence = %s, fit_at = now(),
                            fit_count = fit_count + 1, updated_at = now()
            WHERE id = %s AND goal_id = %s RETURNING title
        """, (verdict, reasons, Jsonb(evidence), job_id, goal_id))

    async def save_note(self, goal_id: str, job_id: str, why_match: str, resume_points: str, gaps: str,
                        created_by: str) -> str | None:
        """Why the note cannot be saved, or None when it is saved. A new note replaces the earlier one."""
        job = await self._one("SELECT fit FROM jobs WHERE goal_id = %s AND id = %s", (goal_id, job_id))
        if job is None:
            return "Unknown job_id for this goal. Use list_jobs to find the ids."
        if job["fit"] != "fit":
            return ONLY_FIT_NOTES
        await self._one("""
            INSERT INTO application_notes (goal_id, job_id, why_match, resume_points, gaps, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET why_match = EXCLUDED.why_match, resume_points = EXCLUDED.resume_points,
                gaps = EXCLUDED.gaps, created_at = now()
            RETURNING id
        """, (goal_id, job_id, why_match, resume_points, gaps, created_by))
        return None

    # Gaps and notes for the report (per goal)

    async def save_notes(self, goal_id: str, agent: str, items: list[dict]) -> int:
        """Replace this agent's earlier items for the same roles. Items without a role are kept."""
        jobs = sorted({item["job_id"] for item in items if item.get("job_id")})
        rows = await self._all("""
            WITH gone AS (DELETE FROM report_notes
                          WHERE goal_id = %(goal)s AND agent = %(agent)s AND job_id = ANY(%(jobs)s::uuid[]))
            INSERT INTO report_notes (goal_id, job_id, agent, kind, text)
            SELECT %(goal)s, (item->>'job_id')::uuid, %(agent)s, item->>'kind', item->>'text'
            FROM jsonb_array_elements(%(items)s) WITH ORDINALITY AS t(item, n) ORDER BY n
            RETURNING id
        """, {"goal": goal_id, "agent": agent, "jobs": jobs, "items": Jsonb(items)})
        return len(rows)

    async def list_notes(self, goal_id: str) -> list[dict]:
        return await self._all("""
            SELECT job_id::text AS job_id, agent, kind, text, created_at FROM report_notes
            WHERE goal_id = %s ORDER BY id
        """, (goal_id,))
