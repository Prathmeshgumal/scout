"""The goal report: the result of a goal, built from the saved data and the events. No agent writes it."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

from .artifact_store import ArtifactStore
from .career_lens import wait_text
from .mandates import kickoff_instruction
from .profile import RESUME, public_profile
from .store import Store, goal_title

GOAL_KEYS = ("id", "title", "status", "status_reason", "created_at", "updated_at", "spend_usd", "max_spend_usd",
             "mandate_id", "soft_limit_at")
MAX_REQUEST = 300  # characters of the request in a report
VERDICT_ORDER = {"fit": 0, "partial": 1, "unsure": 2, None: 3, "not_fit": 4}
END_STATES = {"done", "stopped", "failed", "capped"}  # the goal does not work in these states
UNREAD_STATES = {"partial", "not_captured", "failed"}
# What each kind of subagent does now, for the line at the top of the Jobs page. {n} is the number of roles that wait.
AGENT_NOW = {"candidate-profile": "Building the candidate profile",
             "job-discovery": "Searching for roles",
             "job-research": "Reading job pages for {n}",
             "fit-check": "Checking the fit of {n}",
             "application-prep": "Writing application notes for {n}"}
NEEDS = {"job-research": "needs_research", "fit-check": "needs_fit", "application-prep": "needs_note"}


def _request(goal: dict, events: list[dict]) -> str | None:
    """The request of a goal that a mandate started: the instruction in its first message. A mandate goal has no
    user message at its start."""
    if not goal.get("mandate_id"):
        return None
    kickoff = next((e for e in events if e["type"] == "notice" and e.get("trigger") == "mandate_run"), None)
    return kickoff_instruction(kickoff.get("text", "")) if kickoff else None


def goal_timing(created_at: datetime, events: list[dict], now: datetime, ended_at: datetime | None = None) -> dict:
    """When the goal started and finished, and how long it worked. A goal works from its start, and again from each
    change back to running or waiting, until the next end state. finished_at is None while the goal works.
    ended_at: for a goal in an end state, the last change of its row. It ends a stretch that has no event for its end."""
    work, since, finished = 0.0, created_at, None
    for event in events:
        if event["type"] != "status_changed":
            continue
        at = datetime.fromisoformat(event["at"])
        if event.get("to") in END_STATES:
            if since is not None:
                work += (at - since).total_seconds()
            since, finished = None, at
        elif since is None:
            since, finished = at, None
    if since is not None:
        end = max(ended_at, since) if ended_at else now
        work += (end - since).total_seconds()
        finished = ended_at
    return {"started_at": created_at.isoformat(), "finished_at": finished.isoformat() if finished else None,
            "work_seconds": max(0, round(work))}


def funnel(p: dict) -> dict:
    """The counts at the top of the Jobs page."""
    return {"found": p["jobs"], "researched": p["researched"], "checked": p["checked"], "fit": p["fit"]}


def _count(n: int) -> str:
    return f"{n} role{'' if n == 1 else 's'}"


def now_line(status: str, tasks: list[str], pipeline: dict, waits: list[dict]) -> str:
    """One plain line about what happens now, from the store."""
    if status in END_STATES:
        return ""
    working = [AGENT_NOW[name].format(n=_count(pipeline.get(NEEDS.get(name, ""), 0)))
               for name in AGENT_NOW if name in tasks]
    if working:
        return ". ".join(working)
    reasons = sorted({text for wait in waits if (text := wait_text(wait.get("wait_reason")))})
    if reasons:
        return f"Waiting: {reasons[0][0].lower()}{reasons[0][1:]}"
    if waits:
        return f"Waiting for {len(waits)} LinkedIn scrape{'' if len(waits) == 1 else 's'}"
    return "Deciding the next step"


def _slug(url: str) -> str:
    """A company name from its LinkedIn link, when career-lens gave no name."""
    parts = [part for part in urlsplit(url).path.split("/") if part]
    return parts[-1].replace("-", " ").title() if parts else url


def shown_request(text: str, profile: dict | None) -> str:
    """The request for a report: short, and with no resume text. A user can paste a resume in the first message."""
    words = " ".join(text.split())
    if resume := (profile or {}).get(RESUME):
        words = words.replace(" ".join(resume.split()), "[resume]")
    return goal_title(words, MAX_REQUEST)


def warnings(reads: list[dict], events: list[dict], scrapes: list[dict] = ()) -> list[dict]:
    """What a person must see: a company list that was partial or not read, and a failed search. Never zero roles."""
    out = []
    for read in reads:
        if read["state"] in UNREAD_STATES:
            name = read["company_name"] or _slug(read["company_url"])
            if read["state"] == "partial":
                pages = read["captured_pages"]
                shown = f" from {pages} page{'' if pages == 1 else 's'}" if pages else ""
                text = f"Only part of {name}'s jobs: {read['items']} roles{shown}. It is not the full list."
            else:
                text = f"Could not read {name}'s jobs. This does not mean that it has none."
            out.append({"kind": read["state"], "text": text})
    failed = sum(1 for e in events if e["type"] == "tool_finished" and e.get("tool") == "google_search" and not e.get("ok"))
    if failed:
        out.append({"kind": "google_failed", "text": f"Google search failed {failed} time{'' if failed == 1 else 's'}. "
                                                     "This does not mean that no roles exist."})
    lost = sum(1 for s in scrapes if s["kind"] == "linkedin_job_search" and s["status"] in ("failed", "expired"))
    if lost:
        out.append({"kind": "linkedin_search_failed", "text": f"LinkedIn job search failed {lost} time{'' if lost == 1 else 's'}. "
                                                              "This does not mean that no roles exist."})
    return out


def _wait(row: dict) -> dict:
    return {"kind": row["kind"], "target": row["target"], "status": row["last_status"],
            "reason": wait_text(row["wait_reason"]), "reason_code": row["wait_reason"],
            "next_attempt_at": row["next_attempt_at"], "since": row["submitted_at"]}


def _research(job: dict, goal_id: str) -> list[dict]:
    """Company facts that another goal saved name that goal: this goal used them again."""
    return [{**{k: v for k, v in r.items() if k not in ("goal_id", "goal_title")},
             "from_goal": r["goal_title"] if r["goal_id"] not in (None, goal_id) else None}
            for r in job["research"]]


def job_detail(job: dict, notes: list[dict]) -> dict:
    """One role with all that the card detail shows."""
    return {
        **{key: job[key] for key in ("id", "goal_id", "url", "source", "source_url", "title", "company_name", "location",
                                     "workplace_type", "posted_at", "posting_status", "snippet", "description_text",
                                     "evidence_level", "observed_at", "found_note", "researched_at", "fit", "created_at")},
        "fit_reasons": job["fit_reasons"], "fit_evidence": job["fit_evidence"] or [],
        "company": {"website": job["website"], "linkedin_url": job["linkedin_url"]},
        "research": _research(job, job["goal_id"]),
        "note": job["note"],
        "notes": [{"agent": n["agent"], "kind": n["kind"], "text": n["text"], "at": n["created_at"]}
                  for n in notes if n["job_id"] == job["id"]],
    }


async def build_report(store: Store, artifacts: ArtifactStore, goal_id: str, tasks: list[str] = ()) -> dict | None:
    """tasks: the names of the subagents that work now."""
    goal = await store.get_goal(goal_id)
    if goal is None:
        return None
    events = await store.list_events(goal_id)
    pipeline = await artifacts.pipeline(goal_id)
    notes = await artifacts.list_notes(goal_id)
    rows = await artifacts.list_jobs(goal_id)
    titles = {row["id"]: f"{row['title']}, {row['company_name']}" for row in rows}
    missing = [{"agent": n["agent"], "role": titles.get(n["job_id"]), "text": n["text"], "at": n["created_at"]}
               for n in notes if n["kind"] == "gap"]
    remarks = [{"agent": n["agent"], "text": n["text"], "at": n["created_at"]}
               for n in notes if n["kind"] == "note" and not n["job_id"]]

    jobs = []
    for row in rows:
        job = await artifacts.get_job(goal_id, row["id"])
        if job is not None:
            jobs.append(job_detail(job, notes))
    # Fit roles in the order that fit-check confirmed them. The ones past the target are extra: fit-check often checks
    # a batch, and more roles of it fit than the user asked for.
    fit_order = {job_id: n for n, job_id in enumerate(await artifacts.fit_order(goal_id))}
    target = (goal.get("brief") or {}).get("target_count")
    for job in jobs:
        job["extra"] = bool(target) and fit_order.get(job["id"], -1) >= target
    jobs.sort(key=lambda job: (VERDICT_ORDER[job["fit"]], fit_order.get(job["id"], 0)))

    failed_steps = []
    started = {e["tool_use_id"]: e for e in events if e["type"] == "tool_started"}
    for event in events:
        if event["type"] == "tool_finished" and not event.get("ok"):
            start = started.get(event["tool_use_id"], {})
            failed_steps.append({"agent": event.get("agent"), "label": start.get("label") or event.get("tool"),
                                 "summary": event.get("summary", ""), "failure_kind": event.get("failure_kind"),
                                 "at": event["at"]})
    scrapes = await store.list_scrapes(goal_id)
    failed_scrapes = [s for s in scrapes if s["status"] in ("failed", "expired")]
    waits = await store.waits(goal_id)
    reads = await artifacts.list_job_reads(goal_id)

    return {
        "goal": {key: goal.get(key) for key in GOAL_KEYS},
        # The main agent writes a short title in the brief. Until then, the chat title.
        "title": (goal.get("brief") or {}).get("title") or goal["title"],
        "request": shown_request(_request(goal, events) or await store.first_user_message(goal_id) or goal["title"],
                                 goal.get("profile")),
        "brief": goal.get("brief"),
        "profile": public_profile(goal.get("profile")),
        "pipeline": pipeline,
        "funnel": funnel(pipeline),
        "waits": [_wait(row) for row in waits],
        "warnings": warnings(reads, events, scrapes),
        "now": now_line(goal["status"], list(tasks), pipeline, waits),
        "company_reads": reads,
        "searches": [c["input"]["query"] for c in await store.agent_web_calls(goal_id, "job-discovery")
                     if c["tool"] == "google_search" and c["input"]],
        "jobs": jobs,
        "not_taken": {"missing": missing, "remarks": remarks, "failed_steps": failed_steps,
                      "failed_scrapes": failed_scrapes,
                      "rejected": [{"agent": r["created_by"], "title": r["title"], "company_name": r["company_name"],
                                    "url": r["url"], "reason": r["reason"], "at": r["created_at"]}
                                   for r in await artifacts.list_rejected(goal_id)]},
        "generated_at": datetime.now(UTC).isoformat(),
        "timing": {**goal_timing(goal["created_at"], events, datetime.now(UTC),
                                 goal["updated_at"] if goal["status"] in END_STATES else None),
                   "limit_s": goal["time_limit_s"]},
    }
