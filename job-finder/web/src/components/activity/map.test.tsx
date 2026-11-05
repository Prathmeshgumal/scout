import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { GoalEvent } from "@/lib/api"

const getStep = vi.fn()
vi.mock("@/lib/api", async (original) => ({ ...(await original<typeof import("@/lib/api")>()), getStep: (...a: unknown[]) => getStep(...a) }))

const { ActivityTab, VIEW_KEY } = await import("./ActivityTab")

const T0 = Date.parse("2026-11-05T10:00:00Z")
const at = (s: number) => new Date(T0 + s * 1000).toISOString()
let seq = 0
const ev = (s: number, type: string, fields: Record<string, unknown> = {}): GoalEvent => ({ seq: ++seq, at: at(s), type, ...fields })
const URL = "https://jobs.lever.co/acme/1"

const EVENTS = [
  ev(0, "user_message", { text: "Find roles" }),
  ev(1, "agent_started", { tool_use_id: "a1", agent: "main", name: "job-research", task: "Read the Acme role" }),
  ev(2, "tool_started", { tool_use_id: "r1", tool: "read_page", label: `Reading ${URL}`, agent: "job-research", call_id: "a1" }),
  ev(2, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "queued", wait_reason: "pacing", next_attempt_at: at(40) }),
  ev(40, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "running" }),
  ev(60, "lens_request", { request_id: "rq-1", tool: "read_page", target: URL, status: "succeeded", started_at: at(39), finished_at: at(58) }),
  ev(70, "tool_finished", { tool_use_id: "r1", tool: "read_page", agent: "job-research", call_id: "a1", ok: true, summary: "Junior AI engineer" }),
  ev(80, "agent_finished", { tool_use_id: "a1", name: "job-research", ok: true, summary: "Acme is a fit" }),
  ev(81, "response_finished", { spend_usd: 0.2 }),
]
const PLAIN = [
  ev(0, "tool_started", { tool_use_id: "m1", tool: "set_brief", label: "Saving the brief", agent: "main" }),
  ev(2, "tool_finished", { tool_use_id: "m1", tool: "set_brief", agent: "main", ok: true }),
]

const map = () => screen.getByRole("group", { name: "System map" })

beforeEach(() => {
  localStorage.clear()
  getStep.mockReset().mockResolvedValue({ input: null, output: null, truncated: false, kept: true })
})

describe("activity map", () => {
  it("opens on the map for a mission with career-lens requests, at the end of the mission", () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" maxSpend={0.5} />)
    expect(map()).toBeInTheDocument()
    expect(within(map()).getByRole("button", { name: "Sage: job-research" })).toHaveTextContent("Acme is a fit")
    expect(within(map()).getByRole("button", { name: "Spend jar" })).toHaveTextContent("$0.20")
    expect(screen.getByRole("list", { name: "Requests and results" })).toHaveTextContent("Scout → Sage")
  })

  it("opens on the step detail for a mission without career-lens requests", () => {
    render(<ActivityTab goalId="g1" events={PLAIN} status="done" />)
    expect(screen.getByRole("region", { name: "Step detail" })).toBeInTheDocument()
  })

  it("keeps the choice of the view", async () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await userEvent.click(screen.getByRole("button", { name: "Step detail" }))
    expect(screen.getByRole("region", { name: "Step detail" })).toBeInTheDocument()
    expect(localStorage.getItem(VIEW_KEY)).toBe("step")
  })

  it("moves the replay to a step that the user selects, with the ticket and its wait", async () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await userEvent.click(screen.getByRole("treeitem", { name: `Reading ${URL}, finished` }))
    const slider = screen.getByRole("slider", { name: "Replay time" })
    expect(slider).toHaveAttribute("aria-valuenow", String(T0 + 2000))
    expect(map()).toHaveTextContent(`read_page: ${URL}`)
    // One key press moves the replay 1/50 of the goal (1.62 s): the envelope is in the queue now.
    slider.focus()
    await userEvent.keyboard("{ArrowRight}")
    expect(within(map()).getByRole("button", { name: "Queue" })).toHaveTextContent("read_page · wait 0:37")
  })

  it("explains the part of the map that the user clicks", async () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await userEvent.click(within(map()).getByRole("button", { name: "Gate: guardrails" }))
    expect(screen.getByText(/The gate checks each address before it leaves job-finder/)).toBeInTheDocument()
  })

  it("adds a career-lens queue lane", () => {
    const { container } = render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    expect(container.querySelector('[data-lane="career-lens"]')).not.toBeNull()
  })
})
