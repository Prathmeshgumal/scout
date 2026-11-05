from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import StreamEvent

from job_finder.translate import Emit, Translator, tool_label

from .fakes import assistant as assistant_text
from .fakes import task_notification, task_started, task_updated


def test_tool_labels():
    assert tool_label("mcp__career_lens__google_search", {"query": "acme vp"}) == "Searching Google: acme vp"
    assert tool_label("mcp__career_lens__read_page", {"url": "https://acme.com"}) == "Reading https://acme.com"
    assert tool_label("mcp__career_lens__search_linkedin_jobs", {"keywords": "go engineer", "location": "Pune"}) == \
        "Searching LinkedIn jobs: go engineer in Pune"
    assert tool_label("mcp__career_lens__search_linkedin_jobs", {"keywords": "go"}) == "Searching LinkedIn jobs: go"
    assert tool_label("mcp__career_lens__scrape_linkedin_job", {"url": "u1"}) == "Requesting a LinkedIn job read: u1"
    assert tool_label("mcp__career_lens__get_linkedin_job", {"url": "u1"}) == "Reading the saved LinkedIn job: u1"
    assert tool_label("mcp__career_lens__unknown", {}) == "unknown"


def test_text_delta_is_live_only():
    event = StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}})
    assert Translator().translate(event) == [Emit("text_delta", {"text": "Hel"}, stored=False)]


def test_text_delta_of_a_subagent_is_not_main_text():
    event = StreamEvent(uuid="u", session_id="s", parent_tool_use_id="a1", event={
        "type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}})
    assert Translator().translate(event) == []


def test_assistant_text_and_tool_use_then_result():
    t = Translator()
    message = AssistantMessage(model="m", content=[
        TextBlock(text="Let me search."),
        ToolUseBlock(id="t1", name="mcp__career_lens__google_search", input={"query": "acme"})])
    assert t.translate(message) == [
        Emit("text", {"text": "Let me search.", "agent": "main"}),
        Emit("tool_started", {"tool_use_id": "t1", "tool": "google_search", "label": "Searching Google: acme",
                              "agent": "main"}, detail={"input": {"query": "acme"}})]
    finished = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="t1", content=[{"type": "text", "text": 'Google results for "acme":\n1. Acme'}], is_error=False)]))
    assert finished == [Emit("tool_finished", {"tool_use_id": "t1", "agent": "main", "ok": True,
                                               "summary": 'Google results for "acme":', "tool": "google_search"},
                         detail={"output": 'Google results for "acme":\n1. Acme'})]
    # The input and the output are stored for the Activity tab, never pushed live. See test_steps.py.


def test_a_finished_save_says_what_it_saved_and_a_scrape_gives_its_requests():
    t = Translator()
    t.translate(AssistantMessage(model="m", parent_tool_use_id="a1", content=[
        ToolUseBlock(id="s1", name="mcp__artifacts_job_research__save_job_research", input={}),
        ToolUseBlock(id="s2", name="mcp__artifacts_job_research__save_job_research", input={}),
        ToolUseBlock(id="q1", name="mcp__career_lens__scrape_linkedin_job", input={"url": "u"})]))
    saved, failed, queued = t.translate(UserMessage(parent_tool_use_id="a1", content=[
        ToolResultBlock(tool_use_id="s1", content=[{"type": "text", "text": "Saved the research for 1 role"},
                                                   {"type": "text", "text": "[{}, {}]"}], is_error=False),
        ToolResultBlock(tool_use_id="s2", content="Unknown job_id.", is_error=True),
        ToolResultBlock(tool_use_id="q1", content="LinkedIn job u: queued as rq-1.", is_error=False)]))
    assert saved.data["saved"] == saved.data["summary"] == "Saved the research for 1 role"
    assert saved.data["call_id"] == "a1"
    assert "saved" not in failed.data
    assert "request_ids" not in saved.data
    assert queued.data["request_ids"] == ["rq-1"] and "saved" not in queued.data


def test_blank_text_is_skipped_and_errors_are_flagged():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[ToolUseBlock(id="t2", name="WebFetch", input={"url": "x"})]))
    assert t.translate(AssistantMessage(model="m", content=[TextBlock(text="  ")])) == []
    [emit] = t.translate(UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="boom", is_error=True)]))
    assert emit.data["ok"] is False and emit.data["summary"] == "boom"


def test_session_id_from_init_and_result():
    t = Translator()
    assert t.translate(SystemMessage(subtype="init", data={"session_id": "s-1"})) == []
    assert t.session_id == "s-1"
    t.translate(ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                              num_turns=1, session_id="s-2"))
    assert t.session_id == "s-2"


def test_text_of_an_errored_assistant_message_is_not_shown_as_an_answer():
    message = AssistantMessage(model="m", content=[TextBlock(text="Not logged in · Please run /login")],
                               error="authentication_failed")
    assert Translator().translate(message) == []


def test_translator_remembers_the_latest_text_block():
    translator = Translator()
    translator.translate(AssistantMessage(model="m", content=[TextBlock(text="first")]))
    translator.translate(AssistantMessage(model="m", content=[TextBlock(text="second")]))
    assert translator.last_text == "second"


def test_subagent_calls_are_tagged_with_the_subagent():
    t = Translator()
    call = ToolUseBlock(id="a1", name="Agent", input={"subagent_type": "job-gen", "description": "Find 5 companies",
                                                      "prompt": "Criteria: fintech"})
    assert t.translate(AssistantMessage(model="m", content=[call])) == [
        Emit("agent_started", {"tool_use_id": "a1", "agent": "main", "name": "job-gen", "task": "Find 5 companies"})]  # not the prompt: it can hold resume text
    inner = AssistantMessage(model="m", parent_tool_use_id="a1", content=[
        TextBlock(text="Searching."), ToolUseBlock(id="t9", name="mcp__career_lens__google_search", input={"query": "fintech"})])
    [started] = t.translate(inner)  # the narration of a subagent is not kept: it can repeat resume text
    assert started.type == "tool_started" and started.data["agent"] == "job-gen" and started.data["call_id"] == "a1"
    assert t.last_text == ""  # subagent text is not the main answer
    [tool_done] = t.translate(UserMessage(parent_tool_use_id="a1", content=[
        ToolResultBlock(tool_use_id="t9", content="results", is_error=False)]))
    assert tool_done.type == "tool_finished" and tool_done.data["agent"] == "job-gen"
    assert tool_done.data["call_id"] == "a1"
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a1", content=[{"type": "text", "text": "job-gen is not built yet.\nagentId: x"}],
        is_error=False)]))
    assert finished == Emit("agent_finished", {"tool_use_id": "a1", "agent": "main", "ok": True,
                                               "summary": "job-gen is not built yet.", "name": "job-gen"})


def test_agent_finished_shows_the_report_inside_the_sdk_frame():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="a2", name="Agent", input={"subagent_type": "job-gen", "description": "Find"})]))
    framed = ("[Subagent hand-back] The text below is the final report of a subagent. The harness indents every "
              "line of the report. The report follows:\n  job-gen is not built yet.\n  Second line.\n"
              "agentId: a87f (use SendMessage to continue this agent)\n<usage>subagent_tokens: 1826</usage>")
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a2", content=[{"type": "text", "text": framed}], is_error=False)]))
    assert finished.data["summary"] == "job-gen is not built yet."


def agent_call(t, call_id="a1", name="job-gen"):
    return t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id=call_id, name="Agent", input={"subagent_type": name, "description": "Find 5"})]))


LAUNCHED = [{"type": "text", "text": "Async agent launched successfully.\nagentId: t1 (internal ID)"}]


def test_background_launch_shows_only_agent_started():
    t = Translator()
    [started] = agent_call(t)
    assert started.type == "agent_started"
    assert t.translate(UserMessage(content=[ToolResultBlock(tool_use_id="a1", content=LAUNCHED,
                                                            is_error=False)])) == []
    assert t.translate(task_started("t1", "a1")) == []
    assert t.task_name("t1") == "job-gen"


def test_task_end_gives_one_agent_finished_with_the_summary():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    assert t.translate(task_updated("t1", "completed")) == []  # held for the notification
    [finished] = t.translate(task_notification("t1", summary="6 jobs saved\nmore", tool_use_id="a1"))
    assert finished.type == "agent_finished"
    assert finished.data == {"tool_use_id": "a1", "agent": "main", "name": "job-gen", "ok": True,
                             "status": "completed", "summary": "6 jobs saved"}
    assert t.translate(task_notification("t1")) == []


def test_task_end_without_a_notification_is_reported_on_the_next_message():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    t.translate(task_updated("t1", "killed"))
    [finished] = t.translate(SystemMessage(subtype="init", data={}))
    assert (finished.data["status"], finished.data["ok"], finished.data["summary"]) == ("stopped", False, "")


def test_seeded_tasks_name_a_resumed_subagent():
    t = Translator()
    t.seed([{"task_id": "t1", "name": "job-gen", "tool_use_id": "a1", "status": "interrupted"}])
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="s1", name="SendMessage", input={"to": "t1", "message": "continue"})]))
    t.translate(task_started("t1", "s1"))
    [started] = t.translate(AssistantMessage(model="m", parent_tool_use_id="a1", content=[
        ToolUseBlock(id="x1", name="WebSearch", input={"query": "fintech"})]))
    assert started.data["agent"] == "job-gen"
    [finished] = t.translate(task_notification("t1", summary="done", tool_use_id="s1"))
    assert finished.data["name"] == "job-gen"
    assert (finished.data["tool_use_id"], finished.data["call_id"]) == ("s1", "a1")  # the run that it continues


def test_a_resume_in_the_same_session_names_the_first_call():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    [first] = t.translate(task_notification("t1", summary="done", tool_use_id="a1"))
    assert "call_id" not in first.data
    t.translate(task_started("t1", "s1"))
    [resumed] = t.translate(task_notification("t1", summary="done again", tool_use_id="s1"))
    assert (resumed.data["tool_use_id"], resumed.data["call_id"]) == ("s1", "a1")


def test_foreground_result_and_a_later_notification_give_one_agent_finished():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a1", content=[{"type": "text", "text": "  6 jobs saved."}], is_error=False)]))
    assert finished.data["summary"] == "6 jobs saved."
    assert t.translate(task_updated("t1", "completed")) == []
    assert t.translate(task_notification("t1", summary="6 jobs saved.")) == []


def test_foreground_notification_before_the_result_gives_one_agent_finished():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    [finished] = t.translate(task_notification("t1", summary="6 jobs saved.", tool_use_id="a1"))
    assert finished.data["summary"] == "6 jobs saved."
    assert t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a1", content=[{"type": "text", "text": "  6 jobs saved."}], is_error=False)])) == []


def test_foreground_task_update_before_the_result_gives_one_agent_finished():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    assert t.translate(task_updated("t1", "completed")) == []  # held until the notification
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a1", content=[{"type": "text", "text": "  6 jobs saved."}], is_error=False)]))
    assert finished.type == "agent_finished"
    assert t.translate(assistant_text("Next.")) == [Emit("text", {"text": "Next.", "agent": "main"})]


def test_a_json_report_is_summarized_by_its_lists():
    t = Translator()
    agent_call(t)
    t.translate(task_started("t1", "a1"))
    report = ('{"saved": [{"job_id": "x"}, {"job_id": "y"}], "missing": ["headcount"], "pending_scrapes": [], '
              '"notes": ""}')
    [finished] = t.translate(task_notification("t1", summary=report, tool_use_id="a1"))
    assert finished.data["summary"] == "2 saved, 1 missing"
    assert "report" not in finished.data  # a report can repeat resume text


def test_a_late_notification_for_a_finished_task_is_not_shown_again():
    from .fakes import task_notification
    t = Translator()
    t.seed([{"task_id": "a1", "name": "job-gen", "tool_use_id": "call-1", "status": "stopped"}])
    assert t.translate(task_notification("a1", status="stopped", summary="didn't finish")) == []


def test_agent_finished_keeps_one_line_without_the_sdk_frame():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="a3", name="Agent", input={"subagent_type": "research", "description": "Research"})]))
    framed = ("[Subagent hand-back] The text below is the final report of a subagent. The report follows:\n"
              "  {\"jobs\": [1]}\n  \n  Second part.\n"
              "agentId: a87f (use SendMessage to continue this agent)\n<usage>subagent_tokens: 1826</usage>")
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="a3", content=[{"type": "text", "text": framed}], is_error=False)]))
    assert finished.data["summary"] == '{"jobs": [1]}' and "report" not in finished.data


def test_agent_finished_report_without_a_frame_drops_the_agent_id_lines():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="a4", name="Agent", input={"subagent_type": "fit-check", "description": "Fit"})]))
    [finished] = t.translate(UserMessage(content=[ToolResultBlock(tool_use_id="a4", content=[
        {"type": "text", "text": "fit-check is not built yet.\nagentId: a3c1 (use SendMessage)"}], is_error=False)]))
    assert finished.data["summary"] == "fit-check is not built yet."


def test_background_agent_finished_keeps_one_line_of_the_notification():
    from .fakes import task_notification, task_started
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="a5", name="Agent", input={"subagent_type": "job-gen", "description": "Find"})]))
    t.translate(task_started("t5", "a5"))
    [finished] = t.translate(task_notification("t5", summary="I saved 2 new jobs.\n\n1. Silverflow\n2. Finary"))
    assert finished.data["summary"] == "I saved 2 new jobs."
    assert "Silverflow" not in str(finished.data)


def test_artifact_tools_have_readable_labels():
    assert tool_label("mcp__artifacts__save_jobs", {"jobs": '[{"title": "A"}, {"title": "B"}]'}) == "Saving roles: 2 items"
    assert tool_label("mcp__artifacts__save_fit", {"verdict": "not_fit"}) == "Saving a verdict: not_fit"
    assert tool_label("mcp__artifacts__list_jobs", {}) == "Listing the roles: all"
    assert tool_label("mcp__artifacts__set_brief", {"target_count": 1}) == "Saving the brief: 1 role"
    assert tool_label("mcp__career_lens__wait_for_scrapes", {"request_ids": ["rq-1"]}) == "Waiting for scrapes: rq-1"


def test_a_json_result_is_summed_up_by_its_size():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[ToolUseBlock(id="t4", name="mcp__artifacts__list_jobs", input={})]))
    [emit] = t.translate(UserMessage(content=[ToolResultBlock(tool_use_id="t4", content='[\n {"id": 1},\n {"id": 2}\n]',
                                                              is_error=False)]))
    assert emit.data["summary"] == "2 items"


def test_summary_uses_the_summary_line_of_a_lean_reply():
    import json

    from job_finder.translate import summary_line
    reply = json.dumps({"saved": [{"job_id": "l1", "status": "fit"}], "summary": "1 fit, 0 unsure.",
                        "pending_scrapes": []})
    assert summary_line(reply) == "1 fit, 0 unsure."


def test_saving_gaps_and_notes_shows_as_a_save_in_the_log():
    assert tool_label("mcp__artifacts_fit_check__save_notes", {"items": [{}, {}]}) == "Saving gaps and notes for the report: 2 items"
    assert tool_label("mcp__artifacts_fit_check__save_notes", {"items": '[{"kind": "gap"}]'}) == \
        "Saving gaps and notes for the report: 1 item"
    t = Translator()
    t.translate(AssistantMessage(model="m", parent_tool_use_id="a1", content=[
        ToolUseBlock(id="n1", name="mcp__artifacts_fit_check__save_notes", input={"items": []})]))
    [saved] = t.translate(UserMessage(parent_tool_use_id="a1", content=[
        ToolResultBlock(tool_use_id="n1", content=[{"type": "text", "text": "Saved 2 items for the report"}],
                        is_error=False)]))
    assert saved.data["saved"] == "Saved 2 items for the report"


def test_save_jobs_label_counts_the_rejected_roles():
    label = tool_label("mcp__artifacts_job_discovery__save_jobs",
                       {"jobs": [{"title": "A"}], "rejected": [{"title": "B"}, {"title": "C"}]})
    assert label == "Saving roles: 1 item, 2 rejected"
    assert tool_label("mcp__artifacts_job_discovery__save_jobs", {"jobs": [{"title": "A"}]}) == "Saving roles: 1 item"


def test_a_resume_names_the_subagent_that_it_resumes():
    t = Translator()
    t.seed([{"task_id": "t1", "name": "research", "tool_use_id": "a1", "status": "completed"}])
    [started] = t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="s1", name="SendMessage", input={"to": "t1", "message": "The scrapes finished."})]))
    assert started.data["label"] == "Resuming research"
    [unknown] = t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="s2", name="SendMessage", input={"to": "t9", "message": "continue"})]))
    assert unknown.data["label"] == "Resuming a subagent"


def test_a_role_read_is_summed_up_by_its_lists():
    t = Translator()
    t.translate(AssistantMessage(model="m", content=[
        ToolUseBlock(id="t5", name="mcp__artifacts__read_job", input={"job_id": "l1"})]))
    [emit] = t.translate(UserMessage(content=[ToolResultBlock(
        tool_use_id="t5", content='{"title": "Go Engineer", "research": [{"kind": "facts"}], "notes": [{}, {}]}',
        is_error=False)]))
    assert emit.data["summary"] == "1 research, 2 notes"
