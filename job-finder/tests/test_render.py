from datetime import UTC, datetime

from rich.console import Console

from job_finder.render import EventPrinter, pipeline_line, render_event, research_view


def shown(event) -> str:
    view = render_event(event)
    if view is None:
        return ""
    out = Console(record=True, width=100)
    out.print(view)
    return out.export_text().rstrip()


def test_render_each_event_type():
    assert shown({"type": "user_message", "text": "find jobs"}).split() == ["you", "find", "jobs"]
    assert "Job generation is missing" in shown({"type": "text", "text": "Job generation is missing.",
                                                  "agent": "main"})
    assert shown({"type": "text_delta", "text": "x"}) == ""
    assert shown({"type": "run_started", "trigger": "user"}) == ""
    assert "· Searching Google: acme" in shown({"type": "tool_started", "label": "Searching Google: acme",
                                                "tool": "google_search", "agent": "job-discovery"})
    assert shown({"type": "tool_finished", "tool": "x", "ok": True, "summary": "fine"}) == ""
    assert "✗ x: boom" in shown({"type": "tool_finished", "tool": "x", "ok": False, "summary": "boom"})
    assert "→ job-discovery: find 5" in shown({"type": "agent_started", "agent": "main", "name": "job-discovery",
                                         "task": "find 5"})
    assert "← job-discovery: job-discovery is not built yet." in shown({"type": "agent_finished", "agent": "main",
                                                              "name": "job-discovery", "ok": True,
                                                              "summary": "job-discovery is not built yet."})
    full = shown({"type": "agent_finished", "agent": "main", "name": "job-research", "ok": True,
                  "summary": "Read 3 roles.", "report": '{"summary": "Read 3 roles.", "pending_scrapes": []}'})
    assert full.endswith("← job-research: Read 3 roles.")  # the summary only: a report can repeat resume text
    assert "✓ Saved 2 roles" in shown({"type": "tool_finished", "agent": "job-discovery", "tool": "save_jobs",
                                      "ok": True, "summary": "Saved 2 roles", "saved": "Saved 2 roles"})
    assert "Scrape queued: linkedin_company u" in shown({"type": "scrape_queued", "kind": "linkedin_company",
                                                         "target": "u", "request_id": "rq"})
    assert "rq: running (shift)" in shown({"type": "scrape_status", "request_id": "rq", "status": "running",
                                           "wait_reason": "shift"})
    assert "Careful" in shown({"type": "notice", "text": "Careful"})
    assert "Waiting on 2 scrapes. I continue when they finish." in shown(
        {"type": "status_changed", "to": "waiting", "reason": "Waiting on 2 scrapes"})
    assert "Failed: boom" in shown({"type": "status_changed", "to": "failed", "reason": "boom"})
    assert shown({"type": "status_changed", "to": "done", "reason": None}) == ""
    assert "Response success: $0.05, 2 turns (goal total $0.30)" in shown(
        {"type": "response_finished", "result": "success", "cost_usd": 0.05, "turns": 2, "spend_usd": 0.3})


def test_pipeline_line():
    p = {"jobs": 3, "researched": 2, "fit": 1, "partial": 0, "not_fit": 1, "unsure": 0, "noted": 0, "target": 20}
    assert pipeline_line(p) == "3 roles · 2 researched · 1 fit · 0 partial · 1 not fit · 0 unsure · 0 notes · target 20"


async def test_printer_shows_each_stored_event_once(store, hub, recorder):
    goal = await store.create_goal("g")
    out = Console(record=True, width=100)
    printer = EventPrinter(store, hub, out, goal["id"])
    await recorder.record(goal["id"], "notice", text="first")
    await printer.flush()
    await printer.flush()
    printer.echo_user = False
    await recorder.record(goal["id"], "user_message", text="hidden")
    await printer.flush()
    text = out.export_text()
    assert text.count("first") == 1 and "hidden" not in text


def test_research_shows_its_date_in_the_time_zone_of_the_user():
    row = {"kind": "facts", "created_by": "job-research", "created_at": datetime(2026, 10, 12, 20, 0, tzinfo=UTC),
           "data": {"size": "51-200"}, "sources": []}
    job = {"title": "Go Engineer", "company_name": "Acme", "evidence_level": "full_description",
           "description_text": "Build payments in Go.", "research": [row]}

    def shown(zone):
        out = Console(record=True, width=120)
        out.print(research_view([job], zone))
        return out.export_text()
    assert "(job-research, 2026-10-13)" in shown("Asia/Kolkata")  # 01:30 on 13 Oct in India
    assert "(job-research, 2026-10-12)" in shown("UTC")
