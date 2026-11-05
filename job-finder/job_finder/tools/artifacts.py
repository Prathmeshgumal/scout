"""Artifact tools: agents save their work to Postgres and read the work of other agents."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from claude_agent_sdk import SdkMcpTool, ToolAnnotations, create_sdk_mcp_server, tool

from ..artifact_store import EVIDENCE, JOB_FILTERS, SOURCES, VERDICTS, ArtifactStore
from ..files import MAX_IMAGE_BYTES, FileStorageError
from ..jobs import looks_like_sign_in
from ..profile import PROFILE_FIELDS, RESUME, build_profile, public_profile
from . import ToolContext, result

SERVER_NAME = "artifacts"
MAX_DATA = 4000  # characters of one company row that read_job shows
MAX_DESCRIPTION = 12000  # characters of a job description that read_job shows
READS_PROFILE = {"fit-check", "application-prep"}  # only these agents read the candidate profile
READS_RESUME = {"candidate-profile"}  # only this agent reads the uploaded resume as a file
MAX_FILE_REPLY = 60_000  # characters of a file in one reply

STRING = {"type": "string"}
STRINGS = {"type": "array", "items": STRING}
JOB_ID = {"type": "string", "description": "A role id from list_jobs or read_job."}

TITLE_WORDS = 15  # a report title has fewer words than this
HISTORY_LIMIT = 100  # searches and pages that get_search_history shows, newest first
GOALS_LIMIT = 100  # other goals that list_goals shows


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required}


BRIEF_SCHEMA = _object({
    "title": {"type": "string", "description": (
        "The title of the goal, fewer than 15 words. Say what the goal looks for, e.g. "
        "'Senior backend roles in Bengaluru or remote India'. Do not name results.")},
    "criteria": {"type": "string", "description": "What makes a role good for this goal."},
    "target_count": {"type": "integer", "minimum": 1, "description": "The number of fit roles requested."},
}, ["title", "criteria", "target_count"])
LIST_SCHEMA = _object({"filter": {"type": "string", "enum": list(JOB_FILTERS)}}, [])
READ_SCHEMA = _object({"job_id": JOB_ID}, ["job_id"])
GOALS_SCHEMA = _object({"limit": {"type": "integer", "minimum": 1, "maximum": GOALS_LIMIT,
                                  "description": f"The number of goals, newest first. Default 20, {GOALS_LIMIT} maximum."}},
                       [])
PROFILE_FIELD = _object({"value": {"description": "The value: a text, a number or a list of texts."},
                         "basis": {"type": "string", "enum": ["stated", "inferred"],
                                   "description": "stated: the user said it. inferred: you concluded it from the text."}},
                        ["value", "basis"])
PROFILE_SCHEMA = _object({
    **{name: PROFILE_FIELD for name in PROFILE_FIELDS},
    "resume_text": {"type": "string", "description": "The resume that the user pasted, word for word. Leave it out "
                                                     "when there is none."},
}, [])
GOAL_JOBS_SCHEMA = _object({"goal_id": {"type": "string", "description": "A goal id from list_goals."}}, ["goal_id"])
JOBS_SCHEMA = _object({
    "jobs": {"type": "array", "items": _object({
        "url": {"type": "string", "description": "The link of the role."},
        "title": STRING, "company_name": STRING,
        "location": {"type": "string", "description": "As the source gives it. Leave it out when not known."},
        "workplace_type": {"type": "string", "description": "On-site, Hybrid or Remote, as the source gives it."},
        "posted_at": {"type": "string", "description": "As the source gives it, e.g. '2 days ago'."},
        "posting_status": {"type": "string", "description": "As the source gives it, e.g. 'posted' or 'closed'."},
        "snippet": {"type": "string", "description": "The search snippet, word for word."},
        "source": {"type": "string", "enum": list(SOURCES),
                   "description": "linkedin_company: from a company's jobs section. linkedin_search: from a LinkedIn job search."},
        "source_url": {"type": "string", "description": "The company page or the search that showed the role."},
        "observed_at": {"type": "string", "description": "When the source gave this data, from the reply."},
        "note": {"type": "string", "description": "How you found the role."},
    }, ["url", "title", "company_name"])},
    "rejected": {"type": "array", "description": "Roles that you examined and did not take, each with the reason.",
                 "items": _object({"title": STRING, "company_name": STRING, "url": STRING, "reason": STRING},
                                  ["title", "reason"])},
}, [])
JOB_RESEARCH_SCHEMA = _object({
    "job_id": JOB_ID,
    "evidence_level": {"type": "string", "enum": list(EVIDENCE),
                       "description": "full_description: you read the job text. snippet: only a search snippet. "
                                      "listing_only: only title, company and place."},
    "description_text": {"type": "string", "description": "The full job text, word for word. Only with "
                                                          "full_description."},
    "source_url": {"type": "string", "description": "The page that gave the text."},
}, ["job_id", "evidence_level"])
COMPANY_SCHEMA = _object({
    "job_id": JOB_ID,
    "name": {"type": "string", "description": "The company name. Default: the name on the role."},
    "website": STRING, "linkedin_url": STRING,
    "facts": {"type": "object", "description": "What the company does, size, location, recent news if relevant."},
    "sources": {**STRINGS, "description": "URLs that support the facts."},
}, ["job_id", "facts"])
FIT_SCHEMA = _object({
    "job_id": JOB_ID,
    "verdict": {"type": "string", "enum": list(VERDICTS)},
    "reasons": {"type": "string", "description": "Short reasons. Name the profile item and the job text. The first "
                                                 "sentence shows on the role card."},
    "evidence": {**STRINGS, "description": "Quotes from the job text, word for word."},
}, ["job_id", "verdict", "reasons"])
NOTE_SCHEMA = _object({
    "job_id": JOB_ID,
    "why_match": {"type": "string", "description": "Why this role matches the candidate."},
    "resume_points": {"type": "string", "description": "Which resume points to put first."},
    "gaps": {"type": "string", "description": "The gaps to prepare for."},
}, ["job_id", "why_match", "resume_points", "gaps"])
NOTE_KINDS = ("gap", "note")
NOTES_SCHEMA = _object({"items": {"type": "array", "items": _object({
    "kind": {"type": "string", "enum": list(NOTE_KINDS),
             "description": "gap: what you could not find or save. note: an assumption or other fact."},
    "text": {"type": "string", "description": "One or two full sentences for the candidate."},
    "job_id": {"type": ["string", "null"], "description": "The role that the item is about. Leave it out for the goal."},
}, ["kind", "text"])}}, ["items"])
UNKNOWN_JOB = "Unknown job_id for this goal. Use list_jobs to find the ids."


def _json(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=1)


def _for_agent(row: dict) -> dict:
    """A row without the company id. Agents use only the role id, so that they cannot mix up the two."""
    return {key: value for key, value in row.items() if key not in ("company_id", "goal_id")}


def _id(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


def _text(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def _parsed(value: Any, kind: type) -> Any:
    """The value as a list or dict. Models sometimes send JSON text instead. None when it is neither."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, kind) else None


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def saved(text: str, details: str = "", *, error: bool = False) -> dict[str, Any]:
    """The reply of a save tool. Its first block says what the tool saved. The event shows that text on the tool call,
    so details do not go into the events."""
    out = result(text, error=error)
    if details:
        out["content"].append({"type": "text", "text": details})
    return out


# The tools that the main agent uses. A new tool stays hidden from the main agent until it is added here.
MAIN_TOOLS = frozenset({"set_brief", "get_pipeline", "list_jobs", "read_job", "list_files", "read_file", "list_goals",
                        "read_goal_jobs", "finish_goal"})


def build_tools(ctx: ToolContext, agent: str) -> list[SdkMcpTool]:
    """The artifact tools for one agent. Rows that the tools save record that agent's name."""
    read_only = ToolAnnotations(readOnlyHint=True)

    def store() -> ArtifactStore:
        return ArtifactStore(ctx.store.pool, ctx.research_max_age_days)

    @tool("set_brief", "Save the brief of this goal: a short title, the criteria for a good role and the number of fit "
          "roles requested.", BRIEF_SCHEMA)
    async def set_brief(args: dict[str, Any]) -> dict[str, Any]:
        criteria = _text(args, "criteria")
        try:
            target = int(args.get("target_count"))
        except (TypeError, ValueError):
            target = 0
        if not criteria or target < 1:
            return result("Give criteria and a target_count of 1 or more.", error=True)
        title = " ".join(_text(args, "title").split())
        if not title or len(title.split()) >= TITLE_WORDS:
            return result(f"Give a title of fewer than {TITLE_WORDS} words that says what the goal looks for.",
                          error=True)
        goal = await ctx.store.get_goal(ctx.goal_id) or {}
        fixed = goal.get("mandate_id") and (goal.get("brief") or {}).get("target_count")
        if fixed:  # a run of a mandate: the quota is its target, and only the mandate changes it
            target = int(fixed)
        await ctx.store.set_brief(ctx.goal_id, {"title": title, "criteria": criteria, "target_count": target})
        note = " (fixed by the mandate)" if fixed else ""
        return saved(f"Brief saved: {_plural(target, 'fit role')}{note}. {criteria}")

    @tool("get_brief", "Read the brief of this goal, and the candidate profile"
          + (" with the resume text." if agent in READS_PROFILE else " without the resume text."),
          _object({}, []), annotations=read_only)
    async def get_brief(args: dict[str, Any]) -> dict[str, Any]:
        goal = await ctx.store.get_goal(ctx.goal_id) or {}
        brief = goal.get("brief")
        out = {**brief} if brief else {"brief": "This goal has no brief yet. Use the criteria in your task."}
        profile = goal.get("profile")
        resume = await ctx.files.resume_text() if ctx.files else None  # the uploaded resume is the current one
        if agent in READS_PROFILE and resume:
            profile = {**(profile or {}), RESUME: resume}
        elif profile and agent not in READS_PROFILE:
            profile = {**public_profile(profile), **({"has_resume": True} if resume else {})}
        out["profile"] = profile or "This goal has no candidate profile yet."
        return result(_json(out))

    @tool("list_files", "List the files of this goal, and the resume that the user uploaded. It gives the id of each "
          "file and says if you can read it.", _object({}, []), annotations=read_only)
    async def list_files(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.files is None:
            return result(_json({"resume": None, "files": []}))
        shown = lambda row: {key: row[key] for key in ("id", "name", "kind", "readable")}  # noqa: E731
        resume = await ctx.files.resume()
        rows = await ctx.files.of_goal(ctx.goal_id)
        return result(_json({"resume": shown(resume) if resume else None, "files": [shown(row) for row in rows]}))

    @tool("read_file", "Read one file from list_files: its text, or the image.",
          _object({"file_id": {"type": "string"}}, ["file_id"]), annotations=read_only)
    async def read_file(args: dict[str, Any]) -> dict[str, Any]:
        file_id = _id(args.get("file_id"))
        row = await ctx.files.get(file_id) if ctx.files and file_id else None
        if row is None or (row["role"] == "attachment" and row["goal_id"] != ctx.goal_id):
            return result("This goal has no such file. Call list_files.", error=True)
        if row["role"] == "resume" and agent not in READS_RESUME:
            return result("The resume goes only to the subagents that need it. Run candidate-profile.", error=True)
        if row["kind"] == "image":
            if row["size_bytes"] > MAX_IMAGE_BYTES:
                return result(f"{row['name']} is too large to read as an image.", error=True)
            try:
                data = base64.b64encode(await ctx.files.data(file_id)).decode()
            except FileStorageError as exc:
                return result(str(exc), error=True)
            return {"content": [{"type": "text", "text": f"The image {row['name']}."},
                                {"type": "image", "data": data, "mimeType": row["content_type"]}]}
        text = await ctx.files.text(file_id)
        if text is None:
            return result(f"{row['name']} has no text that the app can read. Ask the user to paste the text.", error=True)
        return result(text[:MAX_FILE_REPLY])

    @tool("save_profile", "Save the candidate profile. A new call replaces it. The reply says if titles or locations "
          "are missing. Then ask the main agent to ask the user.", PROFILE_SCHEMA)
    async def save_profile(args: dict[str, Any]) -> dict[str, Any]:
        profile, missing = build_profile(args)
        await ctx.store.set_profile(ctx.goal_id, profile, ready=not missing)
        if missing:
            return saved(f"Profile saved, but it is not ready. Missing: {', '.join(missing)}. Tell the main agent to "
                         "ask the user for them. Do not guess them.")
        return saved("Profile saved and ready")

    @tool("get_pipeline", "Count the roles of this goal at each stage: found, researched, checked, fit, partial, not "
          "fit, unsure, noted. Also counts the roles that wait for each subagent: needs_research, research_again, "
          "needs_fit, needs_note, and waiting_scrapes: researched roles that wait for their scrapes before fit-check. "
          "Also gives the target from the brief, and if the candidate profile is ready.", _object({}, []),
          annotations=read_only)
    async def get_pipeline(args: dict[str, Any]) -> dict[str, Any]:
        return result(_json(await store().pipeline(ctx.goal_id)))

    @tool("list_jobs", "List the roles of this goal, with ids. Filters: all, needs_research, research_again, needs_fit, "
          "fit, partial, needs_note.", LIST_SCHEMA, annotations=read_only)
    async def list_jobs(args: dict[str, Any]) -> dict[str, Any]:
        name = _text(args, "filter") or "all"
        if name not in JOB_FILTERS:
            return result(f"Unknown filter. Use one of: {', '.join(JOB_FILTERS)}.", error=True)
        rows = await store().list_jobs(ctx.goal_id, name)
        return result(_json([_for_agent(row) for row in rows]) if rows else "No roles match.")

    @tool("read_job", "Read everything saved for one role: the job text, the company facts (also from other goals, "
          "when they are newer than the age limit), the verdict, the application note, and the gaps and notes that "
          "agents saved.", READ_SCHEMA, annotations=read_only)
    async def read_job(args: dict[str, Any]) -> dict[str, Any]:
        job_id = _id(args.get("job_id"))
        job = await store().get_job(ctx.goal_id, job_id, fresh=True) if job_id else None
        if job is None:
            return result(UNKNOWN_JOB, error=True)
        for row in job["research"]:
            text = _json(row["data"])
            if len(text) > MAX_DATA:
                row["data"] = text[:MAX_DATA] + "…"
        if len(job["description_text"] or "") > MAX_DESCRIPTION:
            job["description_text"] = job["description_text"][:MAX_DESCRIPTION] + "…"
        job["report_notes"] = [{"agent": row["agent"], "kind": row["kind"], "text": row["text"]}
                               for row in await store().list_notes(ctx.goal_id) if row["job_id"] == job_id]
        return result(_json(_for_agent(job)))

    @tool("save_jobs", "Save job roles of this goal. Give each a url, a title and a company_name. Leave out a value "
          "that the source does not give. The tool refuses duplicates and roles from earlier goals of the mandate, and "
          "says what it refused. Give the roles that you did not take in `rejected`, each with the reason.", JOBS_SCHEMA)
    async def save_jobs(args: dict[str, Any]) -> dict[str, Any]:
        items = _parsed(args.get("jobs") or [], list) or []
        rejected = [item for item in _parsed(args.get("rejected") or [], list) or [] if isinstance(item, dict)]
        if not items and not rejected:
            return result("Give jobs, rejected roles, or both.", error=True)
        good = [item for item in items if isinstance(item, dict)]
        rows = [{"error": "Not an object."} for _ in range(len(items) - len(good))]
        rows = (await store().save_jobs(ctx.goal_id, good, agent) if good else []) + rows
        new = sum(1 for row in rows if row.get("new"))
        refused = sum(1 for row in rows if row.get("refused"))
        failed = sum(1 for row in rows if "error" in row)
        text = f"{_plural(new, 'new role')}, {refused} refused, {failed} not saved"
        if rejected:
            text += f", {await store().save_rejected(ctx.goal_id, rejected, agent)} rejected"
        return saved(text, _json(rows), error=bool(rows) and failed == len(rows))

    @tool("save_job_research", "Save what you found for one role. Give full_description with the job text only when you "
          "read it. This marks the role as researched, also when you found nothing better. A sign-in wall is refused.",
          JOB_RESEARCH_SCHEMA)
    async def save_job_research(args: dict[str, Any]) -> dict[str, Any]:
        job_id, evidence = _id(args.get("job_id")), _text(args, "evidence_level")
        if job_id is None:
            return result(UNKNOWN_JOB, error=True)
        if evidence not in EVIDENCE:
            return result(f"evidence_level must be one of: {', '.join(EVIDENCE)}.", error=True)
        text = _text(args, "description_text") or None
        if text and looks_like_sign_in(text):
            return result("This text is a sign-in wall, not a job description. Do not save it. Save the evidence that "
                          "you have.", error=True)
        why = await store().save_job_research(ctx.goal_id, job_id, text, evidence, _text(args, "source_url") or None)
        return result(why, error=True) if why else saved(f"Research saved: {evidence}")

    @tool("save_company", "Save the facts of the company of a role. Facts are shared with other goals.", COMPANY_SCHEMA)
    async def save_company(args: dict[str, Any]) -> dict[str, Any]:
        job_id = _id(args.get("job_id"))
        facts = _parsed(args.get("facts"), dict)
        sources = _parsed(args.get("sources") or [], list)
        if job_id is None:
            return result(UNKNOWN_JOB, error=True)
        if facts is None:
            return result("Give facts (a JSON object).", error=True)
        if sources is None:
            return result("sources must be a list of URLs.", error=True)
        company = await store().save_company(
            ctx.goal_id, job_id, name=_text(args, "name") or None, website=_text(args, "website") or None,
            linkedin_url=_text(args, "linkedin_url") or None, data=facts, sources=[str(s) for s in sources],
            created_by=agent)
        return saved("Company facts saved") if company else result(UNKNOWN_JOB, error=True)

    @tool("save_fit", "Save the verdict for one role: fit, partial, not_fit or unsure, with reasons and evidence quotes. "
          "A role with listing_only evidence cannot be fit.", FIT_SCHEMA)
    async def save_fit(args: dict[str, Any]) -> dict[str, Any]:
        job_id = _id(args.get("job_id"))
        verdict = _text(args, "verdict").lower()
        reasons = _text(args, "reasons")
        evidence = _parsed(args.get("evidence") or [], list)
        if verdict not in VERDICTS:
            return result(f"verdict must be one of: {', '.join(VERDICTS)}.", error=True)
        if not reasons:
            return result("Give the reasons for the verdict.", error=True)
        if evidence is None:
            return result("evidence must be a list of quotes.", error=True)
        if job_id is None:
            return result(UNKNOWN_JOB, error=True)
        row = await store().set_fit(ctx.goal_id, job_id, verdict, reasons, evidence)
        if "error" in row:
            return result(row["error"], error=True)
        return saved(f"Verdict saved: {row['title']} is {verdict}")

    @tool("save_application_note", "Save the application note of one fit role: why it matches, which resume points to "
          "put first, and the gaps to prepare for. It sends nothing.", NOTE_SCHEMA)
    async def save_application_note(args: dict[str, Any]) -> dict[str, Any]:
        job_id = _id(args.get("job_id"))
        why, points, gaps = _text(args, "why_match"), _text(args, "resume_points"), _text(args, "gaps")
        if not (why and points and gaps):
            return result("Give why_match, resume_points and gaps.", error=True)
        if job_id is None:
            return result(UNKNOWN_JOB, error=True)
        refused = await store().save_note(ctx.goal_id, job_id, why, points, gaps, agent)
        return result(refused, error=True) if refused else saved("Application note saved")

    @tool("save_notes", "Save the gaps and notes for the goal report. Call it one time, at the end, with all "
          "items. A new call replaces your earlier items for the same roles.", NOTES_SCHEMA)
    async def save_notes(args: dict[str, Any]) -> dict[str, Any]:
        items = _parsed(args.get("items"), list)
        if not items:
            return result("Give items: a list of {kind, text, job_id}.", error=True)
        jobs = {row["id"] for row in await store().list_jobs(ctx.goal_id)}
        good = []
        for item in items:
            item = item if isinstance(item, dict) else {}
            kind, text = str(item.get("kind") or "").lower(), str(item.get("text") or "").strip()
            if kind not in NOTE_KINDS or not text:
                return result(f"Each item needs a kind ({', '.join(NOTE_KINDS)}) and a text.", error=True)
            job_id = _id(item["job_id"]) if item.get("job_id") else None
            if item.get("job_id") and job_id not in jobs:
                return result(UNKNOWN_JOB, error=True)
            good.append({"kind": kind, "text": text, "job_id": job_id})
        count = await store().save_notes(ctx.goal_id, agent, good)
        return saved(f"Saved {_plural(count, 'item')} for the report")

    @tool("get_search_history", "Read what job-discovery did before in this goal: the searches, the pages read and "
          "the roles rejected or refused, newest first. Do not repeat them.", _object({}, []), annotations=read_only)
    async def get_search_history(args: dict[str, Any]) -> dict[str, Any]:
        calls = await ctx.store.agent_web_calls(ctx.goal_id, "job-discovery")
        searches = [{"query": (c["input"] or {}).get("query"), "at": c["at"]}
                    for c in calls if c["tool"] == "google_search"]
        pages = [{"url": (c["input"] or {}).get("url"), "at": c["at"], "ok": c["ok"],
                  **({"failure": c["summary"]} if c["ok"] is False else {})}
                 for c in calls if c["tool"] == "read_page"]
        out: dict[str, Any] = {
            "searches": searches[:HISTORY_LIMIT], "pages": pages[:HISTORY_LIMIT],
            "rejected": [{"title": r["title"], "company_name": r["company_name"], "reason": r["reason"]}
                         for r in await store().list_rejected(ctx.goal_id)],
            "company_job_reads": await store().list_job_reads(ctx.goal_id)}
        older = {key: len(rows) - HISTORY_LIMIT for key, rows in (("searches", searches), ("pages", pages))
                 if len(rows) > HISTORY_LIMIT}
        if older:
            out["older_not_shown"] = older
        return result(_json(out))

    @tool("list_goals", "List the other goals, newest first: the title, criteria, status, date and mandate, and "
          "the number of roles, fit, partial and not_fit roles.", GOALS_SCHEMA, annotations=read_only)
    async def list_goals(args: dict[str, Any]) -> dict[str, Any]:
        try:
            limit = min(max(int(args.get("limit") or 20), 1), GOALS_LIMIT)
        except (TypeError, ValueError):
            limit = 20
        rows = await store().list_goals(ctx.goal_id, limit)
        return result(_json(rows) if rows else "There are no other goals.")

    @tool("read_goal_jobs", "Read the roles of another goal: each with its verdict and the reasons.", GOAL_JOBS_SCHEMA,
          annotations=read_only)
    async def read_goal_jobs(args: dict[str, Any]) -> dict[str, Any]:
        goal_id = _id(args.get("goal_id"))
        rows = await store().goal_jobs(goal_id) if goal_id else []
        return result(_json(rows) if rows else "This goal has no roles. Use list_goals to find the goal ids.")

    @tool("finish_goal", "Finish the goal when it is reached, or when more work will not help. Give the reason. "
          "After this call, the work does not continue toward the target until the user writes again.",
          _object({"reason": {"type": "string", "description": "Why the work stops, in one sentence."}}, ["reason"]))
    async def finish_goal(args: dict[str, Any]) -> dict[str, Any]:
        reason = _text(args, "reason")
        if not reason:
            return result("Give the reason why the work stops.", error=True)
        # The work is not finished while scrapes are pending: their data can change the result.
        if pending := await ctx.store.count_pending(ctx.goal_id):
            return result(f"{_plural(pending, 'scrape')} still pending. End your turn. You get a note when they "
                          "finish.", error=True)
        return saved(f"Goal finished: {reason}")

    tools = [set_brief, get_brief, save_profile, get_pipeline, list_jobs, read_job, save_jobs, save_job_research,
             save_company, save_fit, save_application_note, save_notes, get_search_history, list_goals, read_goal_jobs,
             finish_goal, list_files, read_file]
    return tools


def build_main_tools(ctx: ToolContext) -> list[SdkMcpTool]:
    """The tools of the main agent: only MAIN_TOOLS. Subagents save and read through their own servers."""
    return [t for t in build_tools(ctx, "main") if t.name in MAIN_TOOLS]


def build_main_server(ctx: ToolContext):
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=build_main_tools(ctx))


def build_server(ctx: ToolContext, agent: str, name: str = SERVER_NAME):
    return create_sdk_mcp_server(name=name, version="1.0.0", tools=build_tools(ctx, agent))
