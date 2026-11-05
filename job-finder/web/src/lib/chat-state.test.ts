import { describe, expect, it } from "vitest"
import type { GoalEvent } from "./api"
import { applyEvent, fromEvents, runningAgents, shownState } from "./chat-state"

const ev = (seq: number, type: string, fields: Record<string, unknown> = {}): GoalEvent => ({ seq, type, ...fields })

describe("chat state", () => {
  it("streams text and replaces the draft with the final text", () => {
    const view = fromEvents("running", [
      ev(1, "user_message", { text: "Find roles" }),
      { seq: null, type: "text_delta", text: "Hel" },
      { seq: null, type: "text_delta", text: "lo" },
    ])
    expect(view.messages[1].parts).toEqual([{ kind: "text", text: "Hello", draft: true }])
    expect(applyEvent(view, ev(2, "text", { text: "Hello there" })).messages[1].parts).toEqual([{ kind: "text", text: "Hello there" }])
  })

  it("shows one line for a subagent, from its start to its end", () => {
    const started = fromEvents("running", [ev(1, "agent_started", { tool_use_id: "a1", name: "job-discovery", task: "Find" })])
    expect(runningAgents(started).map((a) => a.name)).toEqual(["job-discovery"])
    const done = applyEvent(started, ev(2, "agent_finished", { tool_use_id: "a1", name: "job-discovery", ok: true, summary: "3 new roles" }))
    expect(done.messages[0].parts).toEqual([{ kind: "agent", callId: "a1", name: "job-discovery", task: "Find", state: "done", summary: "3 new roles", steps: [] }])
    expect(runningAgents(done)).toEqual([])
  })

  it("shows the steps of a subagent in its card, and their results", () => {
    const view = fromEvents("running", [
      ev(1, "agent_started", { tool_use_id: "a1", name: "job-discovery", task: "Find", at: "2026-11-05T10:00:00Z" }),
      ev(2, "tool_started", { tool_use_id: "s1", tool: "search_linkedin_jobs", label: "Searching LinkedIn jobs: AI", call_id: "a1" }),
      ev(3, "tool_started", { tool_use_id: "s2", tool: "read_page", label: "Reading a page", call_id: "a1" }),
      ev(4, "tool_finished", { tool_use_id: "s1", ok: true, summary: "18 roles", call_id: "a1" }),
      ev(5, "tool_finished", { tool_use_id: "s2", ok: false, summary: "career-lens error", call_id: "a1" }),
    ])
    const [card] = view.messages[0].parts
    expect(card.kind === "agent" && card.steps).toEqual([
      { id: "s1", label: "Searching LinkedIn jobs: AI", state: "done", summary: "18 roles" },
      { id: "s2", label: "Reading a page", state: "failed", summary: "career-lens error" },
    ])
    expect(card.kind === "agent" && card.at).toBe("2026-11-05T10:00:00Z")
  })

  it("keeps the stored events for the Activity tab, not the live typing", () => {
    const view = fromEvents("running", [ev(1, "user_message", { text: "x" }), { seq: null, type: "text_delta", text: "a" }])
    expect(view.events.map((e) => e.seq)).toEqual([1])
    expect(applyEvent(view, ev(2, "text", { text: "b" })).events).toHaveLength(2)
  })

  it("does not show the steps of the main agent in the chat", () => {
    const view = fromEvents("running", [ev(1, "tool_started", { tool: "google_search", label: "Searching" })])
    expect(view.messages).toEqual([])
  })

  it("shows a stopped goal's line as stopped", () => {
    const view = fromEvents("stopped", [ev(1, "agent_started", { tool_use_id: "a1", name: "fit-check" })])
    const line = view.messages[0].parts[0]
    expect(line.kind === "agent" && shownState(line, "stopped")).toBe("stopped")
  })

  it("starts a new message after a response ends", () => {
    const view = fromEvents("running", [ev(1, "text", { text: "a" }), ev(2, "response_finished"), ev(3, "text", { text: "b" })])
    expect(view.messages).toHaveLength(2)
    expect(view.messages[0].closed).toBe(true)
  })

  it("does not repeat an error when a retry fails for the same reason", () => {
    const failed = (seq: number) => ev(seq, "status_changed", { to: "failed", reason: "Usage limit" })
    const view = fromEvents("failed", [failed(1), ev(2, "response_finished"), failed(3)])
    expect(view.messages.flatMap((m) => m.parts)).toEqual([{ kind: "error", text: "Usage limit" }])
  })

  it("shows a notice for the user, with its reason", () => {
    const view = fromEvents("running", [ev(1, "notice", { text: "Mandate paused: the last 3 goals failed.", trigger: "mandate_paused" })])
    expect(view.messages[0].parts).toEqual([{ kind: "notice", text: "Mandate paused: the last 3 goals failed." }])
  })

  it("keeps the messages for the main agent away from the user", () => {
    const hidden = ["wrap_up", "time_limit", "time_continue", "continue", "restart", "retry"]
    expect(fromEvents("running", hidden.map((trigger, i) => ev(i + 1, "notice", { text: "An instruction.", trigger }))).messages).toEqual([])
  })

  it("shows only the first line of an expired wait", () => {
    const view = fromEvents("running", [ev(1, "notice", { text: "Scrapes expired:\n- rq-1", trigger: "wait_expired" })])
    expect(view.messages[0].parts).toEqual([{ kind: "notice", text: "Scrapes expired" }])
  })

  it("keeps the notes of the watcher away from the user", () => {
    const view = fromEvents("running", [ev(1, "notice", { text: "Scrapes finished for rq-1", trigger: "scrapes_finished" })])
    expect(view.messages).toEqual([])
  })
})
