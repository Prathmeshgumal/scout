import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import type { JobCardData, PipelineItem } from "@/lib/api"
import { groupByGoal, groupSummary } from "@/lib/jobs"
import { funnelOf } from "@/lib/pipeline"
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
    render(<Funnel steps={funnelOf([item(), item({ stage: "applied" })])} />)
    const funnel = screen.getByRole("region", { name: "Pipeline funnel" })
    expect(within(funnel).getByText("Applied")).toBeInTheDocument()
    expect(within(funnel).getAllByText("50%")).toHaveLength(2)
    expect(within(funnel).getByText("0%")).toBeInTheDocument()
    expect(within(funnel).getByText("Fit jobs")).toBeInTheDocument()
  })
})

describe("Job pipeline page", () => {
  beforeEach(() => vi.unstubAllGlobals())

  it("puts each role in its stage", async () => {
    serve({ "/api/pipeline": { items: [item(), item({ role_key: "b", title: "Applied Role", stage: "applied" })] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    const applied = await screen.findByRole("region", { name: "Applied" })
    expect(within(applied).getByText("Applied Role")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Fit jobs" })).getByText("Go Engineer")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Responded" })).getByText("No roles in this stage.")).toBeInTheDocument()
  })

  it("sends a new stage to the server", async () => {
    const fetcher = serve({ "/api/pipeline": { items: [item()] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    await userEvent.click(await screen.findByRole("combobox", { name: "Stage of Go Engineer" }))
    await userEvent.click(await screen.findByRole("option", { name: "Looking for referral" }))
    await waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => url === "/api/pipeline/stage")
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ role_key: "acme|go|pune", stage: "referral" })
    })
  })

  it("asks if the user applied after the role opens, and marks it", async () => {
    const fetcher = serve({ "/api/pipeline": { items: [item()] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    const link = await screen.findByRole("link", { name: /open role/i })
    link.addEventListener("click", (e) => e.preventDefault())
    await userEvent.click(link)
    await userEvent.click(screen.getByRole("button", { name: "Mark as applied" }))
    await waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => url === "/api/pipeline/stage")
      expect(JSON.parse(String(call?.[1]?.body)).stage).toBe("applied")
    })
  })

  it("saves a note", async () => {
    const fetcher = serve({ "/api/pipeline": { items: [item()] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    await userEvent.click(await screen.findByRole("button", { name: /Go Engineer/ }))
    await userEvent.type(screen.getByRole("textbox", { name: /Add a note/ }), "Asked a friend")
    await userEvent.click(screen.getByRole("button", { name: "Save note" }))
    await waitFor(() => {
      const call = fetcher.mock.calls.find(([url]) => url === "/api/pipeline/notes")
      expect(JSON.parse(String(call?.[1]?.body)).text).toBe("Asked a friend")
    })
  })

  it("keeps the row open and the note draft when the role moves to a new stage", async () => {
    const rows = [item()]
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/pipeline/stage") rows[0] = { ...rows[0], stage: JSON.parse(String(init?.body)).stage }
      return new Response(JSON.stringify({ items: rows }))
    }))
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    await userEvent.click(await screen.findByRole("button", { name: /Go Engineer/ }))
    await userEvent.type(screen.getByRole("textbox", { name: /Add a note/ }), "Not saved yet")
    await userEvent.click(screen.getByRole("combobox", { name: "Stage of Go Engineer" }))
    await userEvent.click(await screen.findByRole("option", { name: "Applied" }))
    const applied = await screen.findByRole("region", { name: "Applied" })
    await waitFor(() => expect(within(applied).getByText("Go Engineer")).toBeInTheDocument())
    expect(within(applied).getByRole("textbox", { name: /Add a note/ })).toHaveValue("Not saved yet")
  })

  it("keeps a role whose goal is deleted, with no link to the goal", async () => {
    serve({ "/api/pipeline": { items: [item({ job_id: null, goal_id: null, stage: "applied" })] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    await userEvent.click(await screen.findByRole("button", { name: /Go Engineer/ }))
    expect(screen.getByText("The goal of this role is deleted.")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Open goal" })).not.toBeInTheDocument()
  })

  it("says what to do when the pipeline is empty", async () => {
    serve({ "/api/pipeline": { items: [] } })
    render(<PipelinePage onOpenGoal={() => {}} onJobs={() => {}} />)
    expect(await screen.findByText("The pipeline has no roles.")).toBeInTheDocument()
  })
})

describe("Jobs page", () => {
  const jobs = [
    job(),
    job({ id: "j2", title: "Partial Role", fit: "partial", researched: false }),
    job({ id: "j3", goal_id: "g2", goal_title: "Data roles", title: "Data Engineer", fit: null, researched: false }),
  ]

  it("groups roles by goal with their counts", () => {
    const groups = groupByGoal(jobs)
    expect(groups.map((g) => g.title)).toEqual(["Go roles", "Data roles"])
    expect(groupSummary(groups[0])).toBe("2 roles: 1 fit, 1 partial, 1 researched")
    expect(groupSummary(groups[1])).toBe("1 role")
  })

  it("shows goals first, a goal's roles on a click, and all roles on the button", async () => {
    serve({ "/api/jobs": jobs })
    render(<AllJobs onNewChat={() => {}} onOpenGoal={() => {}} />)
    await userEvent.click(await screen.findByRole("button", { name: /Go roles/ }))
    expect(screen.getByRole("button", { name: "Partial Role" })).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "Data Engineer" })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Show all jobs" }))
    expect(screen.getByRole("button", { name: "Data Engineer" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Show jobs by goal" })).toBeInTheDocument()
  })
})
