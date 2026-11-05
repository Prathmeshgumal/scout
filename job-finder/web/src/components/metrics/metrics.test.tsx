import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { afterEach, describe, expect, it, vi } from "vitest"
import { TooltipProvider } from "@/components/ui/tooltip"
import type { Metrics } from "@/lib/api"
import { parseRoute, routePath } from "@/lib/route"
import { MetricsPage, duration, fillDays, money } from "./MetricsPage"

const metrics = (over: Partial<Metrics> = {}): Metrics => ({
  days: 30,
  totals: { goals: 4, mandate_goals: 1, spend_usd: 3.2, roles: 40, fit_roles: 8, cost_per_fit_usd: 0.4, done: 3, stopped: 1, failed: 0, running: 0, median_busy_s: 1260, p90_busy_s: 4000 },
  daily: [{ day: "2026-11-04", goals: 2, spend_usd: 1.2, fit_roles: 3 }],
  by_model: [{ model: "claude-sonnet-5-5", goals: 4, spend_usd: 3.2, fit_roles: 8 }],
  verdicts: [{ model: "claude-sonnet-5-5", verdict: "fit", roles: 8 }, { model: "claude-sonnet-5-5", verdict: "not_fit", roles: 20 }],
  scrapes: [{ kind: "linkedin_job_search", total: 5, succeeded: 4, failed: 1, cancelled: 0, pending: 0, median_wait_s: 900, p90_wait_s: 1800 }],
  failure_kinds: [{ kind: "not_found", source: "tool", count: 2 }],
  tools: [{ tool: "read_page", calls: 10, failed: 2 }],
  agents: [{ agent: "fit-check", runs: 3, failed: 0, median_s: 95 }],
  refused: { pages: 1, search_results: 4, goals: 1 },
  evals: {
    latest: [{ id: "e2", suite: "fit-check", model: "claude-sonnet-5-5", prompt_sha: "abc1234def", total: 32, passed: 30, score: 30 / 32, cost_usd: 0.42, duration_s: 300, summary: {}, created_at: "2026-11-05T10:00:00Z" }],
    runs: [
      { id: "e2", suite: "fit-check", model: "claude-sonnet-5-5", prompt_sha: "abc1234def", total: 32, passed: 30, score: 30 / 32, cost_usd: 0.42, duration_s: 300, summary: {}, created_at: "2026-11-05T10:00:00Z" },
      { id: "e1", suite: "fit-check", model: "claude-sonnet-5-5", prompt_sha: "0001111aaa", total: 32, passed: 28, score: 28 / 32, cost_usd: 0.4, duration_s: 280, summary: {}, created_at: "2026-11-04T10:00:00Z" },
    ],
  },
  ...over,
})

function serve(body: Metrics | null, status = 200) {
  const fetcher = vi.fn(async (_url: string) => new Response(JSON.stringify(body ?? { detail: "Sign in to job-finder." }), { status }))
  vi.stubGlobal("fetch", fetcher)
  return fetcher
}

const page = () => render(<TooltipProvider><MetricsPage /></TooltipProvider>)

afterEach(() => vi.unstubAllGlobals())

describe("metrics helpers", () => {
  it("formats money and time", () => {
    expect(money(null)).toBe("–")
    expect(money(0.4)).toBe("$0.400")
    expect(money(3.2)).toBe("$3.20")
    expect(duration(45)).toBe("45 s")
    expect(duration(1260)).toBe("21 min")
    expect(duration(4000)).toBe("1 h 7 min")
  })

  it("fills each day of the period, oldest first", () => {
    const rows = fillDays([{ day: "2026-11-04", goals: 2, spend_usd: 1, fit_roles: 1 }], 3, new Date(2026, 10, 5))
    expect(rows.map((r) => r.day)).toEqual(["2026-11-03", "2026-11-04", "2026-11-05"])
    expect(rows.map((r) => r.goals)).toEqual([0, 2, 0])
  })
})

describe("the metrics page", () => {
  it("has its own address", () => {
    expect(parseRoute("/metrics").section).toBe("metrics")
    expect(routePath(parseRoute("/metrics"))).toBe("/metrics")
  })

  it("shows the totals, the evals and the tables", async () => {
    serve(metrics())
    page()
    const tile = (label: string) => (screen.getByText(label).parentElement as HTMLElement)
    await screen.findByText("Cost per fit role")
    expect(within(tile("Cost per fit role")).getByText("$0.400")).toBeInTheDocument()
    expect(within(tile("Time per goal")).getByText("21 min")).toBeInTheDocument()
    const evals = screen.getByRole("region", { name: "Evals" })
    expect(within(evals).getByText("Fit check")).toBeInTheDocument()
    expect(within(evals).getByText("30/32")).toBeInTheDocument()
    expect(within(evals).getByLabelText("Scores: 88%, 94%")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Scrapes" })).getByText("15 min")).toBeInTheDocument()
    expect(within(screen.getByRole("region", { name: "Tools" })).getByText("20%")).toBeInTheDocument()
    expect(within(screen.getByRole("list", { name: "Spend each day" })).getAllByRole("listitem")).toHaveLength(30)
  })

  it("reads the metrics again for another period", async () => {
    const fetcher = serve(metrics())
    page()
    await screen.findByText("Cost per fit role")
    expect(fetcher.mock.calls[0][0]).toContain("/api/metrics?days=30")
    await userEvent.click(screen.getByRole("button", { name: "7 days" }))
    await waitFor(() => expect(fetcher.mock.calls.at(-1)?.[0]).toContain("/api/metrics?days=7"))
    expect(screen.getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true")
  })

  it("says how to run the evals when there are none", async () => {
    serve(metrics({ evals: { latest: [], runs: [] } }))
    page()
    expect(await screen.findByText(/No eval runs yet/)).toBeInTheDocument()
  })

  it("shows an error", async () => {
    serve(null, 500)
    page()
    expect(await screen.findByRole("alert")).toBeInTheDocument()
  })
})
