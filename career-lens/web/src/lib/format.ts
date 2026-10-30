export const KINDS: Record<string, string> = {
  linkedin_person: "LinkedIn person",
  linkedin_company: "LinkedIn company",
  linkedin_job: "LinkedIn job",
  linkedin_job_search: "LinkedIn job search",
  generic: "Single page",
  google_search: "Google search",
}

export const STATUSES = ["queued", "assigned", "running", "succeeded", "failed", "cancelled", "satisfied"]

const WAITS: Record<string, string> = {
  accounts_off_shift: "LinkedIn accounts are off shift",
  shift_not_planned: "LinkedIn accounts have no shift plan",
  all_workers_busy: "All workers are busy",
  accounts_cooling_down: "LinkedIn accounts are cooling down",
  minimum_interval: "LinkedIn accounts wait between scrapes",
  budgets_exhausted: "LinkedIn accounts used their daily limit",
  no_google_capable_workers: "No worker can do Google searches",
  google_budget_exhausted: "Google searches used their limit",
}

/** A code from the server, in plain words. */
export const words = (code: string) => code.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase())
export const waitText = (reason: string | null) => (reason ? WAITS[reason] ?? words(reason) : null)
export const kindText = (kind: string) => KINDS[kind] ?? words(kind)

export function statusTone(status: string): "good" | "bad" | "busy" | "wait" | "idle" {
  if (status === "succeeded" || status === "satisfied") return "good"
  if (status === "failed") return "bad"
  if (status === "running" || status === "assigned") return "busy"
  if (status === "queued") return "wait"
  return "idle"
}

export const isFinished = (status: string) => ["succeeded", "failed", "cancelled", "satisfied"].includes(status)

export function when(value: string | null | undefined): string {
  if (!value) return "Not yet"
  return new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

export function ago(value: string, now = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(value).getTime()) / 1000))
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function elapsed(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}
