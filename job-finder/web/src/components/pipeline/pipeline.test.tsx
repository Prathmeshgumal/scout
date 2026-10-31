import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { JobCardData, PipelineItem } from "@/lib/api"
import { groupByGoal, postedText } from "@/lib/jobs"
import { funnelOf } from "@/lib/pipeline"
import { parseRoute, routePath } from "@/lib/route"
import { AllJobs } from "@/components/jobs/AllJobs"
import { Funnel } from "./Funnel"
import { PipelinePage } from "./PipelinePage"

const item = (over: Partial<PipelineItem> = {}): PipelineItem => ({
  role_key: "acme|go|pune", stage: "fit", job_id: "j1", goal_id: "g1", goal_title: "Go roles", title: "Go Engineer", company_name: "Acme",
  location: "Pune", workplace_type: null, posting_status: null, url: "https://jobs.test/1", fit: "fit", fit_reasons: "Matches Go.",
  updated_at: null, notes: [], ...over,
})

const job = (over: Partial<JobCardData> = {}): JobCardData => ({
  id: "j1", goal_id: "g1", goal_title: "Go roles", title: "Go Engineer", company_name: "Acme", location: "Pune", workplace_type: null, posted_at: null,
  posting_status: null, url: "https://jobs.test/1", source: "job_board", evidence_level: "full_description", observed_at: null, fit: "fit", fit_reasons: null,
  researched: true, created_at: "2026-11-04T10:00:00Z", ...over,
})

function serve(routes: Record<string, unknown>) {
  const fetcher = vi.fn(async (url: string, _init?: RequestInit) => new Response(JSON.stringify(routes[url] ?? {})))
  vi.stubGlobal("fetch", fetcher)
  return fetcher
}

describe("funnel numbers", () => {
  it("counts a role in each stage that it reached", () => {
    const steps = funnelOf([item(), item({ stage: "referral" }), item({ stage: "applied" }), item({ stage: "responded" })])
    expect(steps.map((s) => s.reached)).toEqual([4, 3, 2, 1])
    expect(steps.map((s) => s.share)).toEqual([null, 75, 50, 25])
  })

  it("does not divide by zero", () => {
    expect(funnelOf([]).map((s) => s.share)).toEqual([null, null, null, null])
  })

  it("shows each stage with its number as text", () => {
    const items = [item(), item({ stage: "applied" })]
    render(<Funnel steps={funnelOf(items)} counts={{ fit: 1, referral: 0, applied: 1, responded: 0 }} />)
    const funnel = screen.getByRole("region", { name: "Pipeline funnel" })
    expect(within(funnel).getByText("Applied")).toBeInTheDocument()
    expect(within(funnel).getAllByText("50%")).toHaveLength(2)
    expect(within(funnel).getByText("0%")).toBeInTheDocument()
    expect(within(funnel).getByText("Fit jobs")).toBeInTheDocument()
    expect(within(funnel).getAllByText("2 reached")).toHaveLength(1)
  })
})

describe("role links", () => {
  it("keeps a role key through its link", () => {
    const route = parseRoute(routePath({ section: "pipeline", goalId: null, mandateId: null, tab: "chat", roleKey: "acme/x|go dev|pune" }))
    expect(route).toMatchObject({ section: "pipeline", roleKey: "acme/x|go dev|pune" })
  })

  it("opens the board for a plain link and survives a broken one", () => {
    expect(parseRoute("/pipeline").roleKey ?? null).toBeNull()
    expect(parseRoute("/pipeline/%E0%A4%A").roleKey).toBeNull()
  })
})

const page = (over: Partial<React.ComponentProps<typeof PipelinePage>> = {}) => (
  <PipelinePage roleKey={null} onOpenRole={() => {}} onBoard={() => {}} onOpenGoal={() => {}} onJobs={() => {}} {...over} />
)

const stageCall = (fetcher: ReturnType<typeof serve>) => {
  const call = fetcher.mock.calls.find(([url]) => url === "/api/pipeline/stage")
  return call ? JSON.parse(String(call[1]?.body)) : null
}

describe("Job pipeline board", () => {
  beforeEach(() => vi.unstubAllGlobals())

  it("puts each role in its column", async () => {
    serve({ "/api/pipeline": { items: [item(), item({ role_key: "b", title: "Applied Role", stage: "applied" })] } })
    render(page())
    const applied = await screen.findByRole("region", { name: "Applied" })
    expect(within(applied).getByText("Applied Role")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Fit jobs" })).getByText("Go Engineer")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Responded" })).getByText("No roles")).toBeInTheDocument()
  })

  it("opens the page of a role on a click", async () => {
    serve({ "/api/pipeline": { items: [item()] } })
    const onOpenRole = vi.fn()
    render(page({ onOpenRole }))
    await userEvent.click(await screen.findByRole("button", { name: /Go Engineer/ }))
    expect(onOpenRole).toHaveBeenCalledWith("acme|go|pune")
  })

  it("shows removed roles in a column only when the user asks", async () => {
    serve({ "/api/pipeline": { items: [item()], removed: [item({ role_key: "gone", title: "Old Role", stage: "removed" as never })] } })
    render(page())
    await screen.findByRole("region", { name: "Fit jobs" })
    expect(screen.queryByText("Old Role")).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: /Removed/ }))
    expect(within(screen.getByRole("region", { name: "Removed" })).getByText("Old Role")).toBeInTheDocument()
    const funnel = screen.getByRole("region", { name: "Pipeline funnel" })
    expect(within(funnel).getByText("Not in the funnel")).toBeInTheDocument()
    expect(within(funnel).getAllByText("1 reached").length).toBeGreaterThan(0)
  })

  it("keeps the board when all roles are removed", async () => {
    serve({ "/api/pipeline": { items: [], removed: [item({ role_key: "gone", stage: "removed" as never })] } })
    render(page())
    expect(await screen.findByRole("button", { name: /Removed/ })).toBeInTheDocument()
    expect(screen.queryByText("The pipeline has no roles.")).not.toBeInTheDocument()
  })

  it("opens the page of a removed role and offers to add it again", async () => {
    const fetcher = serve({ "/api/pipeline": { items: [], removed: [item({ job_id: null, goal_id: null, stage: "removed" as never })] } })
    render(page({ roleKey: "acme|go|pune" }))
    await userEvent.click(await screen.findByRole("combobox", { name: "Stage of this role" }))
    await userEvent.click(await screen.findByRole("option", { name: "Add to pipeline" }))
    await waitFor(() => expect(stageCall(fetcher)).toEqual({ role_key: "acme|go|pune", stage: "fit" }))
  })

  it("highlights the card that the user asked to see, then stops", async () => {
    Element.prototype.scrollIntoView = vi.fn()
    serve({ "/api/pipeline": { items: [item(), item({ role_key: "b", title: "Other Role" })] } })
    render(page({ highlight: "acme|go|pune" }))
    const card = await screen.findByRole("button", { name: /Go Engineer/ })
    expect(card).toHaveAttribute("data-highlighted")
    expect(card).toHaveFocus()
    expect(Element.prototype.scrollIntoView).toHaveBeenCalledOnce()
    expect(screen.getByRole("button", { name: /Other Role/ })).not.toHaveAttribute("data-highlighted")
    await waitFor(() => expect(card).not.toHaveAttribute("data-highlighted"), { timeout: 3500 })
  })

  it("says what to do when the pipeline is empty", async () => {
    serve({ "/api/pipeline": { items: [] } })
    render(page())
    expect(await screen.findByText("The pipeline has no roles.")).toBeInTheDocument()
  })
})

describe("Role page", () => {
  beforeEach(() => vi.unstubAllGlobals())

  const detail = {
    pipeline: { role_key: "acme|go|pune", stage: "fit" }, source: "job_board", fit_reasons: "Matches Go.", fit_evidence: ["Uses Go daily"],
    description_text: "The long description", snippet: null, research: [{ kind: "co", from_goal: null, data: { size: "50 people" } }], notes: [],
    note: { why_match: "Why", resume_points: "Points", gaps: "Gaps", created_at: "" },
  }
  const routes = { "/api/pipeline": { items: [item()] }, "/api/goals/g1/jobs/j1": detail }

  it("shows the note, and keeps long parts closed until asked", async () => {
    serve(routes)
    render(page({ roleKey: "acme|go|pune" }))
    expect(await screen.findByRole("heading", { name: "Go Engineer" })).toBeInTheDocument()
    expect(await screen.findByText("Resume points to put first")).toBeInTheDocument()
    expect(screen.queryByText("The long description")).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Job description" }))
    expect(screen.getByText("The long description")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Company facts" }))
    expect(screen.getByText("50 people")).toBeInTheDocument()
  })

  it("sends a new stage to the server", async () => {
    const fetcher = serve(routes)
    render(page({ roleKey: "acme|go|pune" }))
    await userEvent.click(await screen.findByRole("combobox", { name: "Stage of this role" }))
    await userEvent.click(await screen.findByRole("option", { name: "Looking for referral" }))
    await waitFor(() => expect(stageCall(fetcher)).toEqual({ role_key: "acme|go|pune", stage: "referral" }))
  })

  it("asks if the user applied after the role opens, and marks it", async () => {
    const fetcher = serve(routes)
    render(page({ roleKey: "acme|go|pune" }))
    const link = await screen.findByRole("link", { name: /open role/i })
    link.addEventListener("click", (e) => e.preventDefault())
    await userEvent.click(link)
    await userEvent.click(screen.getByRole("button", { name: "Mark as applied" }))
    await waitFor(() => expect(stageCall(fetcher)?.stage).toBe("applied"))
  })

  it("saves a note", async () => {
    const fetcher = serve(routes)
    render(page({ roleKey: "acme|go|pune" }))
    await userEvent.type(await screen.findByRole("textbox", { name: /Add a note/ }), "Asked a friend")
    await userEvent.click(screen.getByRole("button", { name: "Save note" }))
    await waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => url === "/api/pipeline/notes")
      expect(JSON.parse(String(call?.[1]?.body)).text).toBe("Asked a friend")
    })
  })

  it("keeps a note draft when the user goes to the board and back", async () => {
    serve(routes)
    const { rerender } = render(page({ roleKey: "acme|go|pune" }))
    await userEvent.type(await screen.findByRole("textbox", { name: /Add a note/ }), "Not saved yet")
    rerender(page())
    await screen.findByRole("region", { name: "Fit jobs" })
    rerender(page({ roleKey: "acme|go|pune" }))
    expect(await screen.findByRole("textbox", { name: /Add a note/ })).toHaveValue("Not saved yet")
  })

  it("goes back to the board when the role is removed", async () => {
    const fetcher = serve(routes)
    const onBoard = vi.fn()
    render(page({ roleKey: "acme|go|pune", onBoard }))
    await userEvent.click(await screen.findByRole("button", { name: "Remove from the pipeline" }))
    expect(onBoard).toHaveBeenCalled()
    await waitFor(() => expect(stageCall(fetcher)?.stage).toBe("removed"))
  })

  it("keeps the focus in the note box when the page renders again", async () => {
    serve(routes)
    const { rerender } = render(page({ roleKey: "acme|go|pune", onBoard: () => {} }))
    const box = await screen.findByRole("textbox", { name: /Add a note/ })
    await userEvent.click(box)
    rerender(page({ roleKey: "acme|go|pune", onBoard: () => {} }))
    expect(box).toHaveFocus()
  })

  it("goes back on Escape, but not while typing a note", async () => {
    serve(routes)
    const onBoard = vi.fn()
    render(page({ roleKey: "acme|go|pune", onBoard }))
    await userEvent.click(await screen.findByRole("textbox", { name: /Add a note/ }))
    await userEvent.keyboard("{Escape}")
    expect(onBoard).not.toHaveBeenCalled()
    ;(document.activeElement as HTMLElement).blur()
    await userEvent.keyboard("{Escape}")
    expect(onBoard).toHaveBeenCalled()
  })

  it("keeps a role whose goal is deleted, with no link to the goal", async () => {
    serve({ "/api/pipeline": { items: [item({ job_id: null, goal_id: null, stage: "applied" })] } })
    render(page({ roleKey: "acme|go|pune" }))
    expect(await screen.findByText("The goal of this role is deleted.")).toBeInTheDocument()
    expect(screen.getByText("Matches Go.")).toBeInTheDocument()
  })

  it("says so when the role is not in the pipeline", async () => {
    serve({ "/api/pipeline": { items: [] } })
    render(page({ roleKey: "gone" }))
    expect(await screen.findByText("The pipeline does not have this role.")).toBeInTheDocument()
  })
})

describe("Jobs page", () => {
  beforeEach(() => vi.unstubAllGlobals())

  const jobs = [
    job(),
    job({ id: "j2", title: "Partial Role", fit: "partial", researched: false }),
    job({ id: "j3", goal_id: "g2", goal_title: "Data roles", title: "Data Engineer", fit: null, researched: false }),
    job({ id: "j4", title: "Bad Role", fit: "not_fit" }),
  ]
  const board = (over: Partial<React.ComponentProps<typeof AllJobs>> = {}) => (
    <AllJobs openJob={null} onOpenJob={() => {}} onBoard={() => {}} onShowInPipeline={() => {}} onNewChat={() => {}} onOpenGoal={() => {}} {...over} />
  )
  const open = { goalId: "g1", jobId: "j1" }
  const detail = { pipeline: { role_key: "acme|go|pune", stage: null }, source: "job_board", fit_reasons: null, fit_evidence: [], description_text: null, snippet: null, research: [], notes: [], note: null }

  it("groups roles by goal", () => {
    expect(groupByGoal(jobs).map((g) => g.title)).toEqual(["Go roles", "Data roles"])
  })

  it("puts each role in the column of its verdict", async () => {
    serve({ "/api/jobs": jobs })
    render(board())
    expect(within(await screen.findByRole("region", { name: "Fit" })).getByText("Go Engineer")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Partial" })).getByText("Partial Role")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "In progress" })).getByText("Data Engineer")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Not a fit" })).getByText("Bad Role")).toBeInTheDocument()
  })

  it("filters by goal and by text", async () => {
    serve({ "/api/jobs": jobs })
    render(board())
    await userEvent.click(await screen.findByRole("combobox", { name: "Filter by goal" }))
    await userEvent.click(await screen.findByRole("option", { name: "Data roles" }))
    expect(screen.queryByText("Go Engineer")).not.toBeInTheDocument()
    expect(screen.getByText("Data Engineer")).toBeInTheDocument()
    await userEvent.type(screen.getByRole("textbox", { name: "Search roles" }), "nothing like this")
    expect(screen.queryByText("Data Engineer")).not.toBeInTheDocument()
  })

  it("opens the page of a role on a click", async () => {
    serve({ "/api/jobs": jobs })
    const onOpenJob = vi.fn()
    render(board({ onOpenJob }))
    await userEvent.click(await screen.findByRole("button", { name: /Partial Role/ }))
    expect(onOpenJob).toHaveBeenCalledWith(jobs[1])
  })

  it("adds a role with any verdict to the pipeline, and then offers to show it", async () => {
    const fetcher = serve({ "/api/jobs": jobs, "/api/pipeline": { items: [] }, "/api/goals/g1/jobs/j1": detail })
    render(board({ openJob: open }))
    expect(await screen.findByText("Add this role to the pipeline to write notes.")).toBeInTheDocument()
    expect(screen.queryByRole("textbox", { name: /Add a note/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Remove from the pipeline" })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("combobox", { name: "Stage of this role" }))
    expect(screen.queryByRole("button", { name: "Show in pipeline" })).not.toBeInTheDocument()
    await userEvent.click(await screen.findByRole("option", { name: "Add to pipeline" }))
    await waitFor(() => expect(stageCall(fetcher)).toEqual({ role_key: "acme|go|pune", stage: "fit" }))
  })

  it("shows the notes of a role that is in the pipeline", async () => {
    serve({
      "/api/jobs": jobs, "/api/goals/g1/jobs/j1": detail,
      "/api/pipeline": { items: [item({ stage: "applied", notes: [{ id: "n1", text: "Asked a friend", created_at: "2026-11-04T10:00:00Z" }] })] },
    })
    const onShowInPipeline = vi.fn()
    render(board({ openJob: open, onShowInPipeline }))
    expect(await screen.findByText("Asked a friend")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Remove from the pipeline" })).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Show in pipeline" }))
    expect(onShowInPipeline).toHaveBeenCalledWith("acme|go|pune")
  })

  it("says so when the role does not exist", async () => {
    serve({ "/api/jobs": jobs })
    render(board({ openJob: { goalId: "g1", jobId: "gone" } }))
    expect(await screen.findByText(/This role does not exist/)).toBeInTheDocument()
  })
})

describe("posted date", () => {
  it("does not repeat the word Posted", () => {
    expect(postedText("Posted a day ago")).toBe("Posted a day ago")
    expect(postedText("2 days ago")).toBe("Posted 2 days ago")
    expect(postedText(null)).toBeNull()
  })
})

describe("job links", () => {
  it("keeps a role through its link", () => {
    const ids = { goalId: "b7602321-26e1-4e7c-942e-243d23fec79b", jobId: "b7602321-26e1-4e7c-942e-243d23fec79c" }
    expect(parseRoute(routePath({ section: "jobs", goalId: null, mandateId: null, tab: "chat", job: ids }))).toMatchObject({ section: "jobs", goalId: null, job: ids })
  })
})
