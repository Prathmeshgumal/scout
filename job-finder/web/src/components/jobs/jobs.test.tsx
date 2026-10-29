import { render, screen, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { describe, expect, it, vi } from "vitest"
import type { JobCardData, Report } from "@/lib/api"
import { JobCard } from "./JobCard"
import { JobGrid } from "./JobGrid"
import { EmptyRoles } from "./JobsTab"
import { ProgressHeader } from "./ProgressHeader"

const IDLE = { research: false, check: false }
const job = (fields: Partial<JobCardData> = {}): JobCardData => ({
  id: "j1", goal_id: "g1", title: "Senior Backend Engineer", company_name: "Acme Corp", location: "Bengaluru", workplace_type: null,
  posted_at: "2 days ago", posting_status: null, url: "https://jobs.lever.co/acme/1", source: "lever", evidence_level: "full_description",
  observed_at: null, fit: "fit", fit_reasons: "Matches Python and Go.", created_at: "2026-11-04T10:00:00Z", ...fields,
})

const report = (fields: Partial<Report> & { status?: Report["goal"]["status"]; reason?: string } = {}): Report => ({
  goal: { id: "g1", title: "t", status: fields.status ?? "running", status_reason: fields.reason ?? null, spend_usd: 0.6, max_spend_usd: 2, mandate_id: null },
  title: "Go roles in Pune", request: "r", brief: null,
  pipeline: { jobs: 3, researched: 1, checked: 1, fit: 1, partial: 0, not_fit: 0, unsure: 0, noted: 0, target: 3 },
  funnel: { found: 3, researched: 1, checked: 1, fit: 1 }, waits: [], warnings: [], now: "Reading job pages for 2 roles", searches: [], jobs: [],
  timing: { started_at: "x", finished_at: null, work_seconds: 600, limit_s: 7200 }, ...fields,
})

describe("job card", () => {
  it("shows the facts that it has and leaves out the missing ones", () => {
    render(<JobCard job={job()} working={IDLE} onOpen={() => {}} />)
    expect(screen.getByText("Bengaluru · Posted 2 days ago")).toBeInTheDocument()
    expect(screen.getByText("Fit")).toBeInTheDocument()
    expect(screen.getByText("Full description read")).toBeInTheDocument()
    expect(screen.queryByText(/null|unknown|N\/A/i)).not.toBeInTheDocument()
  })

  it("opens the source safely in a new tab", () => {
    render(<JobCard job={job()} working={IDLE} onOpen={() => {}} />)
    const link = screen.getByRole("link", { name: /open role/i })
    expect(link).toHaveAttribute("target", "_blank")
    expect(link).toHaveAttribute("rel", "noopener noreferrer")
  })

  it("shows Closed, not a link, for a closed posting", () => {
    render(<JobCard job={job({ posting_status: "closed" })} working={IDLE} onOpen={() => {}} />)
    expect(screen.getByText("Closed")).toBeInTheDocument()
    expect(screen.queryByRole("link")).not.toBeInTheDocument()
  })

  it("opens the detail from the title", async () => {
    const onOpen = vi.fn()
    render(<JobCard job={job()} working={IDLE} onOpen={onOpen} />)
    await userEvent.click(screen.getByRole("button", { name: "Senior Backend Engineer" }))
    expect(onOpen).toHaveBeenCalledOnce()
  })
})

describe("job grid", () => {
  const list = [job({ id: "a", title: "Good role" }), job({ id: "b", title: "Bad role", fit: "not_fit" })]

  it("keeps not-fit roles in a closed group with a count", async () => {
    render(<JobGrid jobs={list} working={IDLE} onOpen={() => {}} />)
    expect(screen.queryByText("Bad role")).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: /^Not a fit 1$/ , expanded: false }))
    expect(screen.getByText("Bad role")).toBeInTheDocument()
  })

  it("filters with the verdict pills and says when nothing matches", async () => {
    render(<JobGrid jobs={list} working={IDLE} onOpen={() => {}} />)
    await userEvent.click(within(screen.getByRole("group", { name: /verdict/i })).getByRole("button", { name: /Partial/ }))
    expect(screen.getByText("No roles match these filters.")).toBeInTheDocument()
  })
})

describe("progress header", () => {
  it("gives the state, the spend, the target, the funnel and what happens now", () => {
    render(<ProgressHeader report={report()} />)
    expect(screen.getByText("Running")).toBeInTheDocument()
    expect(screen.getByText("1h 50m left")).toBeInTheDocument()
    expect(screen.getByText("$0.60 of $2.00")).toBeInTheDocument()
    expect(screen.getByText("1 of 3 fit roles")).toBeInTheDocument()
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "1")
    expect(screen.getByText("Reading job pages for 2 roles")).toBeInTheDocument()
  })

  it("shows a partial read as a warning", () => {
    render(<ProgressHeader report={report({ warnings: [{ kind: "failed", text: "Could not read Acme's jobs." }] })} />)
    expect(screen.getByText("Could not read Acme's jobs.")).toBeInTheDocument()
  })

  it("uses the moving border only while the goal runs", () => {
    const { container, rerender } = render(<ProgressHeader report={report()} />)
    expect(container.querySelector(".glow-border")).not.toBeNull()
    rerender(<ProgressHeader report={report({ status: "done" })} />)
    expect(container.querySelector(".glow-border")).toBeNull()
  })
})

describe("a goal with no roles", () => {
  it("offers a retry and gives the reason after a failure", async () => {
    const onRetry = vi.fn()
    render(<EmptyRoles report={report({ status: "failed", reason: "Usage limit" })} onRetry={onRetry} onChat={() => {}} />)
    expect(screen.getByText("Usage limit")).toBeInTheDocument()
    await userEvent.click(screen.getByRole("button", { name: "Retry" }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it("says what was searched when a finished goal has nothing", () => {
    render(<EmptyRoles report={report({ status: "done", searches: ["go engineer pune"] })} onRetry={() => {}} onChat={() => {}} />)
    expect(screen.getByText("No role fit this search.")).toBeInTheDocument()
    expect(screen.getByText("go engineer pune")).toBeInTheDocument()
  })

  it("does not look finished while the agents work", () => {
    render(<EmptyRoles report={report()} onRetry={() => {}} onChat={() => {}} />)
    expect(screen.getByText(/No roles saved yet/)).toBeInTheDocument()
  })
})
