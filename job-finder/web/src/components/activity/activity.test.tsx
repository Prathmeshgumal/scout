import { act, render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { GoalEvent } from "@/lib/api"
import { AgentCard } from "@/components/chat/AgentCard"
import type { AgentLine } from "@/lib/chat-state"

const getStep = vi.fn()
const download = vi.fn()
vi.mock("@/lib/api", async (original) => ({ ...(await original<typeof import("@/lib/api")>()), getStep: (...a: unknown[]) => getStep(...a), download: (...a: unknown[]) => download(...a) }))

const { ActivityTab, TREE_KEY, LANES_KEY } = await import("./ActivityTab")

const T0 = Date.parse("2026-11-05T10:00:00Z")
const at = (s: number) => new Date(T0 + s * 1000).toISOString()
let seq = 0
const ev = (s: number, type: string, fields: Record<string, unknown> = {}): GoalEvent => ({ seq: ++seq, at: at(s), type, ...fields })

const EVENTS = [
  ev(0, "user_message", { text: "Find AI roles" }),
  ev(1, "tool_started", { tool_use_id: "m1", tool: "set_brief", label: "Saving the brief", agent: "main" }),
  ev(2, "tool_finished", { tool_use_id: "m1", tool: "set_brief", agent: "main", ok: true, summary: "Saved", saved: "Saved" }),
  ev(3, "agent_started", { tool_use_id: "a1", agent: "main", name: "job-discovery", task: "Find roles" }),
  ev(4, "tool_started", { tool_use_id: "s1", tool: "search_linkedin_jobs", label: "Searching LinkedIn jobs: AI", agent: "job-discovery", call_id: "a1" }),
  ev(10, "tool_finished", { tool_use_id: "s1", tool: "search_linkedin_jobs", agent: "job-discovery", call_id: "a1", ok: true, summary: "18 roles" }),
  ev(11, "tool_started", { tool_use_id: "s2", tool: "read_page", label: "Reading a page", agent: "job-discovery", call_id: "a1" }),
  ev(12, "tool_finished", { tool_use_id: "s2", tool: "read_page", agent: "job-discovery", call_id: "a1", ok: false, summary: "career-lens error (HTTP 503)" }),
  ev(13, "agent_finished", { tool_use_id: "a1", agent: "main", name: "job-discovery", ok: true, summary: "Found 18 roles" }),
]

const detailPane = () => screen.getByRole("region", { name: "Step detail" })

beforeEach(() => {
  localStorage.clear()
  getStep.mockReset().mockResolvedValue({ input: { keywords: "AI" }, output: "18 roles found", truncated: false, kept: true })
  download.mockReset().mockResolvedValue(undefined)
})

describe("activity tab", () => {
  it("selects the first failed step and loads its input and output", async () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    expect(within(detailPane()).getByRole("heading", { name: "Reading a page" })).toBeInTheDocument()
    expect(getStep).toHaveBeenCalledWith("g1", "s2")
    expect(await within(detailPane()).findByText("18 roles found")).toBeInTheDocument()
    expect(within(detailPane()).getByText(/"keywords": "AI"/)).toBeInTheDocument()
  })

  it("starts on the step that the chat asked for", () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="m1" />)
    expect(within(detailPane()).getByRole("heading", { name: "Saving the brief" })).toBeInTheDocument()
  })

  it("says why a step has no input or output", async () => {
    getStep.mockResolvedValue({ input: null, output: null, truncated: false, kept: false })
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="m1" />)
    expect(await within(detailPane()).findByText(/Not kept: this step can hold resume text/)).toBeInTheDocument()
  })

  it("says when a step ran before the log kept its input and output", async () => {
    getStep.mockResolvedValue({ input: null, output: null, truncated: false, kept: true })
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="s1" />)
    expect(await within(detailPane()).findByText(/Not recorded: this step ran before the log kept its input and output/)).toBeInTheDocument()
  })

  it("goes to the next and the previous step", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="s1" />)
    await user.click(screen.getByRole("button", { name: "Next step" }))
    expect(within(detailPane()).getByRole("heading", { name: "Reading a page" })).toBeInTheDocument()
    await user.click(screen.getByRole("button", { name: "Previous step" }))
    await user.click(screen.getByRole("button", { name: "Previous step" }))
    expect(within(detailPane()).getByRole("heading", { name: "Saving the brief" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Previous step" })).toBeDisabled()
  })

  it("selects a step from a lane block and marks it in the step list", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const lanes = screen.getByRole("region", { name: "Agent lanes" })
    await user.click(within(lanes).getByRole("button", { name: /Searching LinkedIn jobs: AI/ }))
    expect(within(detailPane()).getByRole("heading", { name: "Searching LinkedIn jobs: AI" })).toBeInTheDocument()
    const tree = screen.getByRole("tree", { name: "Steps" })
    expect(within(tree).getByRole("treeitem", { name: /Searching LinkedIn jobs: AI/ })).toHaveAttribute("aria-selected", "true")
  })

  it("hides the step list and keeps that choice", async () => {
    const user = userEvent.setup()
    const { unmount } = render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await user.click(screen.getByRole("button", { name: "Hide the step list" }))
    expect(screen.queryByRole("tree")).not.toBeInTheDocument()
    expect(localStorage.getItem(TREE_KEY)).toBe("hidden")
    unmount()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    expect(screen.queryByRole("tree")).not.toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Show the step list" })).toBeInTheDocument()
  })

  it("filters the step list to problems", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await user.click(screen.getByRole("button", { name: /^Problems/ }))
    const tree = screen.getByRole("tree", { name: "Steps" })
    expect(within(tree).getAllByRole("treeitem").map((i) => i.getAttribute("data-step")).filter(Boolean)).toEqual(["s2"])
  })

  it("shows all the steps again after a filter", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await user.click(screen.getByRole("button", { name: /^Problems/ }))
    await user.click(screen.getByRole("button", { name: /^All/ }))
    const tree = screen.getByRole("tree", { name: "Steps" })
    expect(within(tree).getAllByRole("treeitem").map((i) => i.getAttribute("data-step")).filter(Boolean)).toEqual(["m1", "s1", "s2"])
    expect(screen.getByRole("button", { name: /^All/ })).toHaveAttribute("aria-pressed", "true")
  })

  it("shows the count of each filter", () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    expect(screen.getByRole("button", { name: "All 3" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Problems 1" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Saves 1" })).toBeInTheDocument()
  })

  it("puts the hide button on the step list, and the show button where the list was", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await user.click(within(screen.getByRole("complementary", { name: "Step list" })).getByRole("button", { name: "Hide the step list" }))
    expect(screen.queryByRole("complementary", { name: "Step list" })).not.toBeInTheDocument()
    expect(within(detailPane()).getByRole("button", { name: "Show the step list" })).toBeInTheDocument()
  })

  it("changes the height of the lanes with the keyboard and keeps it", async () => {
    const user = userEvent.setup()
    const { unmount } = render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const handle = screen.getByRole("separator", { name: "Change the height of the lanes" })
    const start = Number(handle.getAttribute("aria-valuenow"))
    handle.focus()
    await user.keyboard("{ArrowUp}{ArrowUp}")
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(start + 40)
    expect(localStorage.getItem(LANES_KEY)).toBe(String(start + 40))
    unmount()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    expect(screen.getByRole("separator", { name: "Change the height of the lanes" })).toHaveAttribute("aria-valuenow", String(start + 40))
  })

  it("makes the bars thicker when the lanes get taller", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const lanes = screen.getByRole("region", { name: "Agent lanes" })
    const thickness = () => parseFloat((lanes.querySelector("[data-lane]") as HTMLElement).style.height)
    const before = thickness()
    screen.getByRole("separator", { name: "Change the height of the lanes" }).focus()
    await user.keyboard("{ArrowUp}{ArrowUp}{ArrowUp}")
    expect(thickness()).toBeGreaterThan(before)
  })

  it("does not make the lanes smaller than one lane", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const handle = screen.getByRole("separator", { name: "Change the height of the lanes" })
    handle.focus()
    for (let i = 0; i < 30; i++) await user.keyboard("{ArrowDown}")
    expect(Number(handle.getAttribute("aria-valuenow"))).toBe(Number(handle.getAttribute("aria-valuemin")))
  })

  it("collapses and expands the steps of an agent run", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="m1" />)
    const tree = screen.getByRole("tree", { name: "Steps" })
    await user.click(within(tree).getByRole("button", { name: "Collapse job-discovery" }))
    expect(within(tree).queryByRole("treeitem", { name: /Searching LinkedIn jobs/ })).not.toBeInTheDocument()
    expect(within(tree).getByRole("treeitem", { name: /^job-discovery/ })).toHaveAttribute("aria-expanded", "false")
    await user.click(within(tree).getByRole("button", { name: "Expand job-discovery" }))
    expect(within(tree).getByRole("treeitem", { name: /Searching LinkedIn jobs/ })).toBeInTheDocument()
  })

  it("collapses a message with all its work", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const tree = screen.getByRole("tree", { name: "Steps" })
    await user.click(within(tree).getByRole("button", { name: "Collapse message 1" }))
    expect(within(tree).queryByRole("treeitem", { name: /^job-discovery/ })).not.toBeInTheDocument()
    expect(within(tree).queryByRole("treeitem", { name: /Saving the brief/ })).not.toBeInTheDocument()
  })

  it("collapses all the runs and expands them again", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="m1" />)
    const list = screen.getByRole("complementary", { name: "Step list" })
    await user.click(within(list).getByRole("button", { name: "Collapse all" }))
    expect(within(list).queryByRole("treeitem", { name: /Reading a page/ })).not.toBeInTheDocument()
    expect(within(list).getByRole("treeitem", { name: /Saving the brief/ })).toBeInTheDocument()
    await user.click(within(list).getByRole("button", { name: "Expand all" }))
    expect(within(list).getByRole("treeitem", { name: /Reading a page/ })).toBeInTheDocument()
  })

  it("opens a collapsed run when one of its steps is selected", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" initialStep="m1" />)
    const tree = screen.getByRole("tree", { name: "Steps" })
    await user.click(within(tree).getByRole("button", { name: "Collapse job-discovery" }))
    await user.click(screen.getByRole("button", { name: "Next step" }))
    expect(within(tree).getByRole("treeitem", { name: /Searching LinkedIn jobs/ })).toHaveAttribute("aria-selected", "true")
  })

  it("shows the count of failed steps on a run", () => {
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    const run = within(screen.getByRole("tree", { name: "Steps" })).getByRole("treeitem", { name: /^job-discovery/ })
    expect(within(run).getByText("1 failed")).toBeInTheDocument()
  })

  it("downloads the log with the token", async () => {
    const user = userEvent.setup()
    render(<ActivityTab goalId="g1" events={EVENTS} status="done" />)
    await user.click(screen.getByRole("button", { name: "Download log" }))
    expect(download).toHaveBeenCalledWith("/api/goals/g1/events.jsonl")
  })

  it("tells the user when the mission has no steps yet", () => {
    render(<ActivityTab goalId="g1" events={[]} status="running" />)
    expect(screen.getByText("No steps yet. Steps show here when the agents start to work.")).toBeInTheDocument()
  })

  it("shows a step that comes in while the tab is open", async () => {
    const { rerender } = render(<ActivityTab goalId="g1" events={EVENTS.slice(0, 3)} status="running" />)
    await act(async () => {
      rerender(<ActivityTab goalId="g1" events={EVENTS} status="running" />)
    })
    await waitFor(() => expect(screen.getByRole("tree", { name: "Steps" })).toHaveTextContent("Reading a page"))
  })
})

const line = (fields: Partial<AgentLine> = {}): AgentLine => ({
  kind: "agent", callId: "a1", name: "job-discovery", task: "Find roles", state: "running",
  steps: [1, 2, 3, 4, 5].map((n) => ({ id: `s${n}`, label: `Step ${n}`, state: "done" as const })), ...fields,
})

describe("agent card in the chat", () => {
  it("shows the last three steps and the count of the earlier ones", () => {
    render(<AgentCard line={line()} status="running" />)
    expect(screen.queryByText("Step 2")).not.toBeInTheDocument()
    expect(screen.getByText("Step 3")).toBeInTheDocument()
    expect(screen.getByText("Step 5")).toBeInTheDocument()
    expect(screen.getByText("2 earlier steps")).toBeInTheDocument()
  })

  it("opens the last step in Activity", async () => {
    const user = userEvent.setup()
    const onActivity = vi.fn()
    render(<AgentCard line={line()} status="running" onActivity={onActivity} />)
    await user.click(screen.getByRole("button", { name: "Open in Activity" }))
    expect(onActivity).toHaveBeenCalledWith("s5")
  })

  it("has no Activity link when the chat has no Activity tab", () => {
    render(<AgentCard line={line()} status="running" />)
    expect(screen.queryByRole("button", { name: "Open in Activity" })).not.toBeInTheDocument()
  })

  it("shows the summary of a finished run", () => {
    render(<AgentCard line={line({ state: "done", summary: "Found 18 roles" })} status="done" />)
    expect(screen.getByText("Found 18 roles")).toBeInTheDocument()
    expect(screen.getByText("finished")).toBeInTheDocument()
  })
})
