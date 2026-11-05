import type { Evidence, JobCardData, Verdict } from "./api"

export const VERDICT_LABEL: Record<Verdict, string> = { fit: "Fit", partial: "Partial", unsure: "Unsure", not_fit: "Not a fit" }

export const EVIDENCE_LABEL: Record<Evidence, string> = {
  full_description: "Full description read",
  snippet: "From search snippet",
  listing_only: "Listing only",
}

/** The label of a card with no verdict. working: the agents of this goal work now. */
export function stageLabel(job: JobCardData, working: { research: boolean; check: boolean }): string {
  const researched = Boolean(job.researched ?? job.researched_at)
  if (researched) return working.check ? "Checking" : "Not checked"
  return working.research ? "Researching" : "Found"
}

// fit, partial, unsure, then the roles in progress. Not-fit roles have their own group.
const ORDER = (job: JobCardData) => ({ fit: 0, partial: 1, unsure: 2 } as Record<string, number>)[job.fit ?? ""] ?? 3

/** Keep the order of equal roles, so that a live update does not move the cards. */
export function sortJobs<T extends JobCardData>(jobs: T[]): T[] {
  return jobs.map((job, i) => [job, i] as const).sort((a, b) => ORDER(a[0]) - ORDER(b[0]) || a[1] - b[1]).map(([job]) => job)
}

/** "Posted 2 days ago". Some sources already put the word "Posted" in the date. */
export function postedText(posted: string | null | undefined): string | null {
  const text = posted?.trim().replace(/^posted\s*:?\s*/i, "")
  return text ? `Posted ${text}` : null
}

export function isClosed(job: Pick<JobCardData, "posting_status">): boolean {
  return ![null, undefined, "", "posted", "reposted", "open"].includes(job.posting_status?.trim().toLowerCase())
}

/** The two letters of the tinted square. */
export function monogram(company: string): string {
  const words = company.replace(/[^\p{L}\p{N} ]/gu, " ").split(/\s+/).filter(Boolean)
  if (!words.length) return "?"
  const letters = words.length > 1 ? words[0][0] + words[1][0] : words[0].slice(0, 2)
  return letters.toUpperCase()
}

/** The same company has the same hue on each card. */
export function monogramHue(company: string): number {
  let hash = 0
  for (const char of company.toLowerCase()) hash = (hash * 31 + char.charCodeAt(0)) % 360
  return hash
}

export type JobFilters = { verdict: Verdict | "all" | "progress"; workplace: string; text: string }
export const NO_FILTERS: JobFilters = { verdict: "all", workplace: "all", text: "" }

export function filterJobs<T extends JobCardData>(jobs: T[], f: JobFilters): T[] {
  const words = f.text.toLowerCase().split(/\s+/).filter(Boolean)
  return jobs.filter((job) => {
    if (f.verdict === "progress" ? job.fit !== null : f.verdict !== "all" && job.fit !== f.verdict) return false
    if (f.workplace !== "all" && (job.workplace_type ?? "").toLowerCase() !== f.workplace) return false
    const haystack = `${job.title} ${job.company_name} ${job.location ?? ""}`.toLowerCase()
    return words.every((word) => haystack.includes(word))
  })
}

export function workplaces(jobs: JobCardData[]): string[] {
  return [...new Set(jobs.flatMap((job) => (job.workplace_type ? [job.workplace_type.toLowerCase()] : [])))].sort()
}

/** "42m", "1h 05m" */
export function span(seconds: number): string {
  const minutes = Math.max(0, Math.round(seconds / 60))
  if (minutes < 60) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`
}

export function timeAgo(iso: string, now = Date.now()): string {
  const minutes = Math.floor(Math.max(0, now - new Date(iso).getTime()) / 60_000)
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

export type GoalGroup = {
  goalId: string
  title: string
  jobs: JobCardData[]
  counts: { fit: number; partial: number; unsure: number; notFit: number; researched: number; waiting: number }
}

/** The roles of each goal. The goal with the newest role comes first. */
export function groupByGoal(jobs: JobCardData[]): GoalGroup[] {
  const groups = new Map<string, GoalGroup>()
  for (const job of jobs) {
    let group = groups.get(job.goal_id)
    if (!group) {
      group = { goalId: job.goal_id, title: job.goal_title ?? "Mission", jobs: [], counts: { fit: 0, partial: 0, unsure: 0, notFit: 0, researched: 0, waiting: 0 } }
      groups.set(job.goal_id, group)
    }
    group.jobs.push(job)
    if (job.fit === "fit") group.counts.fit += 1
    else if (job.fit === "partial") group.counts.partial += 1
    else if (job.fit === "unsure") group.counts.unsure += 1
    else if (job.fit === "not_fit") group.counts.notFit += 1
    else group.counts.waiting += 1
    if (job.researched ?? job.researched_at) group.counts.researched += 1
  }
  return [...groups.values()]
}
