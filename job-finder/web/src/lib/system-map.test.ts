import { describe, expect, it } from "vitest"
import type { GoalEvent } from "./api"
import { buildTimeline, mapState, siteOf } from "./system-map"

const T0 = Date.parse("2026-11-05T10:00:00Z")
const at = (s: number) => new Date(T0 + s * 1000).toISOString()
const ms = (s: number) => T0 + s * 1000
let seq = 0
const ev = (s: number, type: string, fields: Record<string, unknown> = {}): GoalEvent => ({ seq: ++seq, at: at(s), type, ...fields })
const MOVE = 1500
const URL = "https://jobs.lever.co/acme/1"

const EVENTS = [
  ev(0, "user_message", { text: "Find roles" }),
  ev(1, "agent_started", { tool_use_id: "a1", agent: "main", name: "job-research", task: "Read the Acme role" }),
  ev(2, "tool_started", { tool_use_id: "r1", tool: "read_page", label: `Reading ${URL}`, agent: "job-research", call_id: "a1" }),
  ev(2, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "queued" }),
  ev(5, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "queued", wait_reason: "pacing", next_attempt_at: at(40) }),
  ev(40, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "running" }),
  ev(60, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "succeeded", requested_at: at(2), started_at: at(39), finished_at: at(58) }),
  ev(70, "tool_finished", { tool_use_id: "r1", tool: "read_page", agent: "job-research", call_id: "a1", ok: true, summary: "Junior AI engineer, Hyderabad" }),
  ev(71, "tool_started", { tool_use_id: "g1", tool: "google_search", label: "Searching Google: acme careers", agent: "job-research", call_id: "a1" }),
  ev(71, "lens_request", { request_id: "rq-2", tool: "google_search", target: "acme careers", status: "satisfied" }),
  ev(72, "tool_finished", { tool_use_id: "g1", tool: "google_search", agent: "job-research", call_id: "a1", ok: true, summary: "8 results" }),
  ev(73, "tool_started", { tool_use_id: "f1", tool: "save_fit", label: "Saving a verdict: fit", agent: "job-research", call_id: "a1" }),
  ev(74, "tool_finished", { tool_use_id: "f1", tool: "save_fit", agent: "job-research", call_id: "a1", ok: true, saved: "Saved" }),
  ev(75, "tool_started", { tool_use_id: "j1", tool: "save_jobs", label: "Saving roles", agent: "job-research", call_id: "a1" }),
  ev(76, "tool_finished", { tool_use_id: "j1", tool: "save_jobs", agent: "job-research", call_id: "a1", ok: true, saved: "3 new roles, 0 refused" }),
  ev(80, "agent_finished", { tool_use_id: "a1", name: "job-research", ok: true, summary: "Acme is a fit" }),
  ev(81, "response_finished", { spend_usd: 0.42 }),
]

describe("system map timeline", () => {
  const tl = buildTimeline(EVENTS, "done", ms(90))

  it("joins each career-lens request to the step that made it, with the real queue and work times", () => {
    const [read, search] = tl.requests
    expect(read).toMatchObject({ id: "rq-1", agent: "job-research", stepId: "r1", site: "careers", cached: false,
      submitAt: ms(2), startAt: ms(39), endAt: ms(58), status: "succeeded", result: "Junior AI engineer, Hyderabad" })
    expect(search).toMatchObject({ id: "rq-2", stepId: "g1", site: "google", cached: true, result: "From the history: no new scrape" })
  })

  it("counts the board, the spend and the mail between the agents", () => {
    expect(tl.board.map((b) => [b.key, b.n])).toEqual([["checked", 1], ["fit", 1], ["found", 3]])
    expect(tl.spend).toEqual([{ at: ms(81), usd: 0.42 }])
    expect(tl.mail.filter((m) => m.from === "main" || m.to === "main").map((m) => [m.from, m.to, m.text]))
      .toEqual([["main", "job-research", "Read the Acme role"], ["job-research", "main", "Acme is a fit"]])
  })

  it("names the site of a request", () => {
    expect(siteOf("google_search", "x")).toBe("google")
    expect(siteOf("read_page", "https://www.linkedin.com/jobs/view/1")).toBe("linkedin")
    expect(siteOf("search_linkedin_jobs", "ai")).toBe("linkedin")
    expect(siteOf("read_page", URL)).toBe("careers")
  })

  it("gives a background scrape to the newest step of the tool that queued it", () => {
    const events = [
      ev(0, "tool_started", { tool_use_id: "c1", tool: "scrape_company", label: "Requesting a fresh company scrape: https://www.linkedin.com/company/acme", agent: "job-research" }),
      ev(1, "scrape_queued", { request_id: "rq-9", kind: "linkedin_company", target: "https://www.linkedin.com/company/acme" }),
      ev(2, "tool_finished", { tool_use_id: "c1", tool: "scrape_company", agent: "job-research", ok: true }),
      ev(3, "scrape_status", { request_id: "rq-9", status: "queued", wait_reason: "outside_shift" }),
      ev(30, "scrape_status", { request_id: "rq-9", status: "running" }),
      ev(50, "scrape_finished", { request_id: "rq-9", status: "succeeded" }),
    ]
    const [request] = buildTimeline(events, "done", ms(60)).requests
    expect(request).toMatchObject({ agent: "job-research", stepId: "c1", site: "linkedin", background: true, startAt: ms(30), endAt: ms(50) })
  })
})

describe("system map state", () => {
  const tl = buildTimeline(EVENTS, "done", ms(90))

  it("flies the request envelope to career-lens first", () => {
    const s = mapState(tl, ms(2) + 500, MOVE)
    const envelope = s.movers.find((m) => m.kind === "envelope" && m.path.includes("queue"))
    expect(envelope?.f).toBeCloseTo(1 / 3)
    expect(s.gate).toBe("pass")
  })

  it("shows a ticket with the live wait while the request is in the queue", () => {
    const s = mapState(tl, ms(10), MOVE)
    expect(s.tickets.map((t) => [t.request.id, t.wait])).toEqual([["rq-1", "wait 0:30"]])
    expect(s.agents["job-research"]).toMatchObject({ mode: "working", queued: 1 })
    expect(s.agents.main.mode).toBe("waiting")
    expect(s.workers).toEqual([])
  })

  it("shows a browser at work and the site in use while the request runs", () => {
    const s = mapState(tl, ms(48), MOVE)
    expect(s.tickets).toEqual([])
    expect(s.workers[0].request.id).toBe("rq-1")
    expect(s.workers[0].progress).toBeCloseTo(9 / 19)
    expect(s.sites.careers).toBe(true)
    expect(s.claude).toBe(false)
  })

  it("runs the reader after the page came back and until the step ends", () => {
    expect(mapState(tl, ms(65), MOVE).reader).toBe(true)
    expect(mapState(tl, ms(71), MOVE).reader).toBe(false)
  })

  it("counts a request from the history and ends with the totals", () => {
    const s = mapState(tl, ms(90), MOVE)
    expect(s.history.reused).toBe(1)
    expect(s.board).toEqual({ found: 3, researched: 0, checked: 1, fit: 1 })
    expect(s.spend).toBe(0.42)
    expect(s.agents["job-research"]).toMatchObject({ mode: "done", text: "Acme is a fit" })
    expect(s.movers).toEqual([])
  })

  it("shows nothing before the goal starts", () => {
    const s = mapState(tl, ms(-5), MOVE)
    expect(s.movers).toEqual([])
    expect(s.mail).toEqual([])
    expect(Object.values(s.agents).every((a) => a.mode === "idle")).toBe(true)
  })

  it("stops an envelope at the gate for a blocked site", () => {
    const blocked = buildTimeline([
      ev(0, "tool_started", { tool_use_id: "r2", tool: "read_page", label: "Reading https://naukri.com/x", agent: "main" }),
      ev(1, "site_blocked", { tool: "read_page", url: "https://naukri.com/x", reason: "blocked site" }),
      ev(1, "tool_finished", { tool_use_id: "r2", tool: "read_page", agent: "main", ok: false }),
    ], "done", ms(5))
    const s = mapState(blocked, ms(1), MOVE)
    expect(s.gate).toBe("stop")
    expect(s.movers.at(-1)).toMatchObject({ path: ["gate", "agent"], tone: "refused" })
  })
})
