import { describe, expect, it } from "vitest"
import type { JobCardData } from "./api"
import { NO_FILTERS, filterJobs, isClosed, monogram, monogramHue, sortJobs, span, stageLabel, workplaces } from "./jobs"

const job = (fields: Partial<JobCardData> = {}): JobCardData => ({
  id: "j1", goal_id: "g1", title: "Backend Engineer", company_name: "Acme Corp", location: "Pune", workplace_type: "Hybrid",
  posted_at: null, posting_status: null, url: "https://jobs.lever.co/acme/1", source: "lever", evidence_level: "listing_only",
  observed_at: null, fit: null, fit_reasons: null, created_at: "2026-11-04T10:00:00Z", ...fields,
})

describe("role rules", () => {
  it("orders fit, partial, unsure, then roles in progress, and keeps equal roles in place", () => {
    const list = [job({ id: "a" }), job({ id: "b", fit: "unsure" }), job({ id: "c", fit: "fit" }), job({ id: "d", fit: "partial" }), job({ id: "e" })]
    expect(sortJobs(list).map((j) => j.id)).toEqual(["c", "d", "b", "a", "e"])
  })

  it("names the stage of a role with no verdict", () => {
    const idle = { research: false, check: false }
    expect(stageLabel(job(), idle)).toBe("Found")
    expect(stageLabel(job(), { ...idle, research: true })).toBe("Researching")
    expect(stageLabel(job({ researched: true }), { ...idle, check: true })).toBe("Checking")
    expect(stageLabel(job({ researched_at: "2026-11-04" }), idle)).toBe("Not checked")
  })

  it("filters by verdict, workplace and words", () => {
    const list = [job({ id: "a", fit: "fit" }), job({ id: "b", workplace_type: "Remote", company_name: "Globex" })]
    expect(filterJobs(list, { ...NO_FILTERS, verdict: "fit" }).map((j) => j.id)).toEqual(["a"])
    expect(filterJobs(list, { ...NO_FILTERS, verdict: "progress" }).map((j) => j.id)).toEqual(["b"])
    expect(filterJobs(list, { ...NO_FILTERS, workplace: "remote" }).map((j) => j.id)).toEqual(["b"])
    expect(filterJobs(list, { ...NO_FILTERS, text: "GLOBEX backend" }).map((j) => j.id)).toEqual(["b"])
    expect(workplaces(list)).toEqual(["hybrid", "remote"])
  })

  it("says closed only for a posting that is closed", () => {
    expect(isClosed({ posting_status: "open" })).toBe(false)
    expect(isClosed({ posting_status: null })).toBe(false)
    expect(isClosed({ posting_status: "closed" })).toBe(true)
    expect(isClosed({ posting_status: "reposted" })).toBe(false)
  })

  it("makes a monogram from any company name", () => {
    expect(monogram("Acme Corp")).toBe("AC")
    expect(monogram("Mesh")).toBe("ME")
    expect(monogram("(!)")).toBe("?")
    expect(monogramHue("Acme")).toBe(monogramHue("acme"))
  })

  it("writes a length of time", () => {
    expect(span(2520)).toBe("42m")
    expect(span(3900)).toBe("1h 05m")
    expect(span(-5)).toBe("0m")
  })
})
