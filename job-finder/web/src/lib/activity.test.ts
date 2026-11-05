import { describe, expect, it } from "vitest"
import type { GoalEvent } from "./api"
import { buildActivity, matches, nextStep } from "./activity"

const T0 = Date.parse("2026-11-05T10:00:00Z")
const at = (s: number) => new Date(T0 + s * 1000).toISOString()
let seq = 0
const ev = (s: number, type: string, fields: Record<string, unknown> = {}): GoalEvent => ({ seq: ++seq, at: at(s), type, ...fields })

const EVENTS = [
  ev(0, "user_message", { text: "Find junior AI roles in Hyderabad\nMy resume is attached" }),
  ev(1, "tool_started", { tool_use_id: "m1", tool: "set_brief", label: "Saving the brief", agent: "main" }),
  ev(2, "tool_finished", { tool_use_id: "m1", tool: "set_brief", agent: "main", ok: true, summary: "Saved", saved: "Saved" }),
  ev(3, "agent_started", { tool_use_id: "a1", agent: "main", name: "job-discovery", task: "Find roles" }),
  ev(4, "tool_started", { tool_use_id: "s1", tool: "search_linkedin_jobs", label: "Searching LinkedIn jobs: AI", agent: "job-discovery", call_id: "a1" }),
  ev(9, "agent_started", { tool_use_id: "a2", agent: "main", name: "job-research", task: "Read roles" }),
  ev(10, "tool_started", { tool_use_id: "r1", tool: "read_page", label: "Reading a page", agent: "job-research", call_id: "a2" }),
  ev(12, "tool_finished", { tool_use_id: "r1", tool: "read_page", agent: "job-research", call_id: "a2", ok: false, summary: "career-lens error (HTTP 503)" }),
  ev(20, "tool_finished", { tool_use_id: "s1", tool: "search_linkedin_jobs", agent: "job-discovery", call_id: "a1", ok: true, summary: "18 roles" }),
  ev(21, "agent_finished", { tool_use_id: "a1", agent: "main", name: "job-discovery", ok: true, summary: "Found 18 roles" }),
  ev(22, "response_finished", {}),
  ev(30, "user_message", { text: "Check the fit" }),
  ev(31, "tool_started", { tool_use_id: "m2", tool: "list_jobs", label: "Listing the roles", agent: "main" }),
]

describe("activity", () => {
  it("puts each answer, its agent runs and their steps in a tree", () => {
    const a = buildActivity(EVENTS, "running", T0 + 40_000)
    expect(a.answers.map((x) => [x.n, x.text])).toEqual([[1, "Find junior AI roles in Hyderabad"], [2, "Check the fit"]])
    const [first] = a.answers
    expect(first.items.map((i) => (i.kind === "run" ? `run:${i.run.agent}` : `step:${i.step.id}`)))
      .toEqual(["step:m1", "run:job-discovery", "run:job-research"])
    const discovery = first.items[1].kind === "run" ? first.items[1].run : null
    expect(discovery).toMatchObject({ id: "a1", state: "done", summary: "Found 18 roles", endedAt: T0 + 21_000 })
    expect(discovery?.steps.map((s) => [s.id, s.state, s.summary])).toEqual([["s1", "done", "18 roles"]])
  })

  it("marks a failed step and saves", () => {
    const a = buildActivity(EVENTS, "running", T0 + 40_000)
    const byId = Object.fromEntries(a.steps.map((s) => [s.id, s]))
    expect(byId.r1).toMatchObject({ state: "failed", summary: "career-lens error (HTTP 503)", agent: "job-research", runId: "a2" })
    expect(byId.m1.saved).toBe(true)
    expect(byId.m2.state).toBe("running")
  })

  it("shows a step that still runs as stopped once the mission stopped", () => {
    const a = buildActivity(EVENTS, "stopped", T0 + 40_000)
    expect(a.steps.find((s) => s.id === "m2")?.state).toBe("stopped")
    const research = a.answers[0].items[2]
    expect(research.kind === "run" && research.run.state).toBe("stopped")
  })

  it("lists the steps in time order and gives one lane for each agent", () => {
    const a = buildActivity(EVENTS, "running", T0 + 40_000)
    expect(a.steps.map((s) => s.id)).toEqual(["m1", "s1", "r1", "m2"])
    expect(a.agents).toEqual(["main", "job-discovery", "job-research"])
    expect([a.start, a.end]).toEqual([T0, T0 + 40_000])
    expect(buildActivity(EVENTS, "done", T0 + 99_000).end).toBe(T0 + 31_000)
  })

  it("goes to the next and the previous step", () => {
    const { steps } = buildActivity(EVENTS, "running", T0)
    expect(nextStep(steps, "s1", 1)).toBe("r1")
    expect(nextStep(steps, "s1", -1)).toBe("m1")
    expect(nextStep(steps, "m1", -1)).toBeNull()
    expect(nextStep(steps, "m2", 1)).toBeNull()
  })

  it("filters by problems, saves and words", () => {
    const { steps } = buildActivity(EVENTS, "running", T0)
    const ids = (filter: Parameters<typeof matches>[1]) => steps.filter((s) => matches(s, filter)).map((s) => s.id)
    expect(ids({ query: "", kind: "problems" })).toEqual(["r1"])
    expect(ids({ query: "", kind: "saves" })).toEqual(["m1"])
    expect(ids({ query: "linkedin", kind: "all" })).toEqual(["s1"])
    expect(ids({ query: "JOB-RESEARCH", kind: "all" })).toEqual(["r1"])
  })

  it("keeps steps from before the first message", () => {
    const a = buildActivity([ev(0, "tool_started", { tool_use_id: "x", tool: "list_jobs", label: "Listing", agent: "main" })], "done", T0)
    expect(a.answers).toHaveLength(1)
    expect(a.answers[0].items).toHaveLength(1)
  })
})
