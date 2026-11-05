import type { GoalEvent, GoalStatus } from "./api"
import { buildActivity, type Run, type Step } from "./activity"

/**
 * The system map of the Activity tab. buildTimeline reads the events of a goal one time. mapState gives the state of
 * the map at one moment: the live view, the replay and the tests all use it. Nothing here is invented: a part of the
 * map that has no event stays quiet.
 */

export type Site = "google" | "linkedin" | "careers"

/** One career-lens request. Times are ms. startAt: a worker took it. endAt: career-lens finished it. */
export type LensRequest = {
  id: string
  tool: string
  target: string
  agent: string
  stepId: string | null
  site: Site
  submitAt: number
  startAt: number | null
  endAt: number | null
  status: string
  cached: boolean
  background: boolean
  /** The changes while it waits: the time, the status, the wait reason and the next try. */
  changes: { at: number; status: string; waitReason: string | null; nextAt: number | null }[]
  result: string | null
}

export type Mail = { at: number; from: string; to: string; text: string; kind: "request" | "result" | "refused" }
export type BoardCounts = { found: number; researched: number; checked: number; fit: number }
export type Refusal = { at: number; agent: string; target: string; reason: string }

export type Timeline = {
  start: number
  end: number
  steps: Step[]
  runs: Run[]
  requests: LensRequest[]
  mail: Mail[]
  spend: { at: number; usd: number }[]
  board: { at: number; key: keyof BoardCounts; n: number }[]
  refusals: Refusal[]
}

const str = (value: unknown) => (typeof value === "string" ? value : "")
const time = (value: unknown) => {
  const parsed = typeof value === "string" ? Date.parse(value) : Number.NaN
  return Number.isNaN(parsed) ? null : parsed
}

/** What the step list shows for a tool and its target: the request finds its step with this. */
const LABEL: Record<string, string> = {
  google_search: "Searching Google: ",
  read_page: "Reading ",
  search_linkedin_jobs: "Searching LinkedIn jobs: ",
  scrape_company: "Requesting a fresh company scrape: ",
  scrape_linkedin_job: "Requesting a LinkedIn job read: ",
}
/** A background scrape names its kind. The step that queued it has this tool. */
const KIND_TOOL: Record<string, string> = {
  google_search: "google_search",
  linkedin_job_search: "search_linkedin_jobs",
  linkedin_company: "scrape_company",
  linkedin_job: "scrape_linkedin_job",
}
const ENDED = new Set(["satisfied", "succeeded", "failed", "cancelled", "expired"])

export function siteOf(tool: string, target: string): Site {
  if (tool === "google_search") return "google"
  if (tool.includes("linkedin") || tool === "scrape_company" || /linkedin\.com/i.test(target)) return "linkedin"
  return "careers"
}

export function buildTimeline(events: GoalEvent[], status: GoalStatus, now: number): Timeline {
  const activity = buildActivity(events, status, now)
  const runs = activity.answers.flatMap((a) => a.items.flatMap((i) => (i.kind === "run" ? [i.run] : [])))
  const requests = new Map<string, LensRequest>()
  const used = new Set<string>()
  const mail: Mail[] = []
  const spend: Timeline["spend"] = []
  const board: Timeline["board"] = []
  const refusals: Refusal[] = []
  const labels = new Map<string, string>()

  /** The step that made a request: the same tool and target, started at or before it, not used yet. */
  const stepFor = (tool: string, target: string, at: number): Step | undefined => {
    const label = (LABEL[tool] ?? "") + target
    const open = activity.steps.filter((s) => s.tool === tool && !used.has(s.id) && s.startedAt <= at + 1000)
    const step = open.find((s) => s.label.trim() === label.trim()) ?? open.find((s) => target && s.label.includes(target))
      ?? open.filter((s) => s.endedAt === null || s.endedAt >= at).at(-1)
    if (step) used.add(step.id)
    return step
  }

  let at = Number.NaN
  for (const event of events) {
    at = time(event.at) ?? at
    if (Number.isNaN(at)) continue
    switch (event.type) {
      case "tool_started":
        labels.set(str(event.tool_use_id), str(event.label))
        break
      case "tool_finished": {
        if (!event.ok) break
        const tool = str(event.tool)
        if (tool === "save_jobs") {
          const n = Number(/(\d+) new role/.exec(str(event.saved))?.[1] ?? 0)
          if (n) board.push({ at, key: "found", n })
        } else if (tool === "save_job_research") board.push({ at, key: "researched", n: 1 })
        else if (tool === "save_fit") {
          board.push({ at, key: "checked", n: 1 })
          if (/:\s*fit$/.test(labels.get(str(event.tool_use_id)) ?? "")) board.push({ at, key: "fit", n: 1 })
        }
        break
      }
      case "response_finished":
        if (typeof event.spend_usd === "number") spend.push({ at, usd: event.spend_usd })
        break
      case "agent_started":
        mail.push({ at, from: "main", to: str(event.name), text: str(event.task), kind: "request" })
        break
      case "agent_finished":
        mail.push({ at, from: str(event.name), to: "main", text: str(event.summary), kind: "result" })
        break
      case "site_blocked": {
        const tool = str(event.tool)
        const step = activity.steps.filter((s) => s.tool === tool && s.startedAt <= at).at(-1)
        const target = str(event.url) || str(event.query)
        const reason = str(event.reason) || (event.count ? `${event.count} results on blocked sites removed` : "a blocked site")
        refusals.push({ at, agent: step?.agent ?? "main", target, reason })
        mail.push({ at, from: "gate", to: step?.agent ?? "main", text: `${target}: ${reason}`, kind: "refused" })
        break
      }
      case "lens_request":
      case "scrape_queued":
      case "scrape_status":
      case "scrape_finished": {
        const id = str(event.request_id)
        if (!id) break
        let request = requests.get(id)
        if (!request) {
          const tool = event.type === "lens_request" ? str(event.tool) : KIND_TOOL[str(event.kind)] ?? str(event.kind)
          const target = str(event.target)
          const step = stepFor(tool, target, at)
          request = { id, tool, target, agent: step?.agent ?? "main", stepId: step?.id ?? null, site: siteOf(tool, target),
            submitAt: at, startAt: null, endAt: null, status: "queued", cached: false, background: event.type !== "lens_request",
            changes: [], result: null }
          requests.set(id, request)
          mail.push({ at, from: request.agent, to: "career-lens", text: `${tool}("${target}")`, kind: "request" })
        }
        if (event.type === "scrape_queued") request.background = true
        const state = event.type === "scrape_finished" ? str(event.status) || "succeeded" : str(event.status) || request.status
        request.status = state
        if (state === "running" || state === "assigned") request.startAt ??= at
        request.changes.push({ at, status: state, waitReason: str(event.wait_reason) || null, nextAt: time(event.next_attempt_at) })
        if (ENDED.has(state)) {
          request.cached = state === "satisfied" && request.startAt === null
          const started = time(event.started_at)
          if (started !== null && started >= request.submitAt) request.startAt = started
          const finished = time(event.finished_at)
          request.endAt = finished !== null && finished >= request.submitAt && finished <= at ? finished : at
          const step = request.stepId ? activity.steps.find((s) => s.id === request!.stepId) : undefined
          request.result = request.cached ? "From the history: no new scrape" : step?.summary ?? state
        }
        break
      }
    }
  }
  // A step's summary comes after the request ends: fill the result again.
  for (const request of requests.values()) {
    const step = request.stepId ? activity.steps.find((s) => s.id === request.stepId) : undefined
    if (request.endAt !== null && step?.summary && !request.cached) request.result = step.summary
    if (request.endAt !== null) mail.push({ at: request.endAt, from: "career-lens", to: request.agent, text: request.result ?? request.status, kind: "result" })
  }
  mail.sort((a, b) => a.at - b.at)
  return { start: activity.start, end: activity.end, steps: activity.steps, runs, requests: [...requests.values()], mail, spend, board, refusals }
}

// The state at one moment.

export type AgentState = { mode: "working" | "thinking" | "waiting" | "done" | "idle"; text: string; queued: number }
export type Place = "agent" | "gate" | "dispatcher" | "queue" | "worker" | "site" | "history" | "reader"
/** path: map places. "agent" is the agent of the mover; "agent:<name>" is that agent; "worker" is browser `worker`. */
export type Mover = { kind: "envelope" | "page" | "runner"; path: string[]; f: number; caption?: string; tone: "request" | "result" | "refused"; agent: string; worker?: number; site?: Site }
export type Ticket = { request: LensRequest; wait: string }
export type WorkerState = { request: LensRequest; progress: number }

export type MapState = {
  agents: Record<string, AgentState>
  claude: boolean
  spend: number
  board: BoardCounts
  tickets: Ticket[]
  workers: WorkerState[]
  reader: boolean
  dispatcher: boolean
  gate: "pass" | "stop" | null
  history: { reused: number; flash: boolean }
  sites: Record<Site, boolean>
  linkedin: "unused" | "open" | "used"
  movers: Mover[]
  mail: Mail[]
  step: Step | null
}

export const AGENTS = ["main", "candidate-profile", "job-discovery", "job-research", "fit-check", "application-prep"] as const

/** The known agents, then each other agent that worked in the goal. */
export function agentsOf(tl: Timeline): string[] {
  const names: string[] = [...AGENTS]
  for (const s of [...tl.runs, ...tl.steps]) if (s.agent && !names.includes(s.agent)) names.push(s.agent)
  return names
}

const WAIT_WORDS: Record<string, string> = {
  pacing: "pacing gap",
  outside_shift: "outside the shift",
  google_budget: "Google budget used",
  cooldown: "site cooldown",
  no_worker: "no free worker",
}

function waitText(request: LensRequest, t: number): string {
  const change = request.changes.filter((c) => c.at <= t).at(-1)
  if (change?.nextAt && change.nextAt > t) {
    const s = Math.ceil((change.nextAt - t) / 1000)
    return `wait ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`
  }
  if (change?.waitReason) return WAIT_WORDS[change.waitReason] ?? change.waitReason.replaceAll("_", " ")
  return "waiting"
}

const between = (t: number, a: number, b: number) => t >= a && t < b
const frac = (t: number, a: number, b: number) => (b <= a ? 1 : Math.min(1, Math.max(0, (t - a) / (b - a))))
const short = (text: string, n = 48) => (text.length > n ? `${text.slice(0, n - 1)}…` : text)

/** t: the moment, in ms. move: how long one envelope flies, in ms of goal time (longer at a fast replay). */
export function mapState(tl: Timeline, t: number, move: number): MapState {
  const movers: Mover[] = []
  const tickets: Ticket[] = []
  const workers: WorkerState[] = []
  let reader = false, dispatcher = false, gate: MapState["gate"] = null, flash = false, reused = 0
  const sites: Record<Site, boolean> = { google: false, linkedin: false, careers: false }
  let linkedin: MapState["linkedin"] = "unused"

  for (const r of tl.requests) {
    if (r.submitAt > t) continue
    if (r.site === "linkedin") linkedin = linkedin === "open" ? "open" : "used"
    const step = r.stepId ? tl.steps.find((s) => s.id === r.stepId) : undefined
    const back = r.tool === "read_page" ? ["history", "dispatcher", "gate", "reader"] : ["history", "dispatcher", "gate", "agent"]
    if (r.cached) {
      if (r.endAt !== null && r.endAt <= t) reused += 1
      const f = frac(t, r.submitAt, r.submitAt + 2 * move)
      if (f < 1) {
        if (f < 0.5) movers.push({ kind: "envelope", path: ["agent", "gate", "dispatcher", "history"], f: f * 2, caption: short(`${r.tool}: ${r.target}`), tone: "request", agent: r.agent })
        else movers.push({ kind: "page", path: back, f: (f - 0.5) * 2, caption: "From the history: no new scrape", tone: "result", agent: r.agent })
        flash ||= between(f, 0.4, 0.6)
        gate ??= "pass"
      }
      continue
    }
    const sent = r.submitAt + move
    const started = r.startAt ?? (r.endAt ?? Number.POSITIVE_INFINITY)
    const ended = r.endAt ?? Number.POSITIVE_INFINITY
    if (t < sent && t < started) {
      movers.push({ kind: "envelope", path: ["agent", "gate", "dispatcher", "queue"], f: frac(t, r.submitAt, sent), caption: short(`${r.tool}: ${r.target}`), tone: "request", agent: r.agent })
      gate ??= "pass"
      dispatcher = true
    } else if (t < started) {
      tickets.push({ request: r, wait: waitText(r, t) })
      dispatcher = true
      if (r.site === "linkedin") linkedin = "open"
    } else if (t < ended) {
      const f = frac(t, started, Number.isFinite(ended) ? ended : started + 4 * move)
      workers.push({ request: r, progress: Number.isFinite(ended) ? f : Math.min(0.9, f) })
      const out = Math.min(move, (ended - started) / 2)
      const worker = workers.length - 1
      if (t < started + out) movers.push({ kind: "runner", path: ["worker", "site"], f: frac(t, started, started + out), tone: "request", agent: r.agent, worker, site: r.site })
      else if (Number.isFinite(ended) && t > ended - out) movers.push({ kind: "page", path: ["site", "worker"], f: frac(t, ended - out, ended), tone: "result", agent: r.agent, worker, site: r.site })
      sites[r.site] = true
      if (r.site === "linkedin") linkedin = "open"
    } else if (t < ended + move) {
      const f = frac(t, ended, ended + move)
      movers.push({ kind: "page", path: ["worker", ...back], f, caption: short(r.result ?? r.status), tone: "result", agent: r.agent })
      flash ||= f < 0.25
      gate ??= "pass"
    }
    // The small model reads the page after career-lens gave it, until the step ends.
    if (r.tool === "read_page" && step && r.endAt !== null && t >= r.endAt && (step.endedAt === null ? true : t < step.endedAt)) reader = true
    if (r.tool === "read_page" && step && r.endAt !== null && step.endedAt !== null && between(t, Math.max(r.endAt + move, step.endedAt - move), step.endedAt + move) && t >= step.endedAt)
      movers.push({ kind: "envelope", path: ["reader", "agent"], f: frac(t, step.endedAt, step.endedAt + move), tone: "result", agent: r.agent })
  }

  for (const refusal of tl.refusals) {
    const f = frac(t, refusal.at - move, refusal.at + move)
    if (f <= 0 || f >= 1) continue
    gate = "stop"
    if (f < 0.5) movers.push({ kind: "envelope", path: ["agent", "gate"], f: f * 2, caption: short(refusal.target), tone: "request", agent: refusal.agent })
    else movers.push({ kind: "envelope", path: ["gate", "agent"], f: (f - 0.5) * 2, caption: "Refused: blocked site", tone: "refused", agent: refusal.agent })
  }

  // Envelopes between the agents: the request of a run and its result.
  for (const m of tl.mail) {
    if (m.from === "career-lens" || m.to === "career-lens" || m.from === "gate") continue
    const f = frac(t, m.at, m.at + move)
    if (f > 0 && f < 1) movers.push({ kind: "envelope", path: [`agent:${m.from}`, `agent:${m.to}`], f, caption: short(m.text), tone: m.kind === "result" ? "result" : "request", agent: m.from })
  }

  const agents: Record<string, AgentState> = {}
  for (const name of agentsOf(tl)) agents[name] = { mode: "idle", text: name === "fit-check" ? "Waiting for roles to check" : "Waiting for work", queued: 0 }
  for (const run of tl.runs) {
    if (run.startedAt > t || !agents[run.agent]) continue
    if (run.endedAt !== null && run.endedAt <= t) agents[run.agent] = { mode: "done", text: short(run.summary ?? "Done", 60), queued: 0 }
    else agents[run.agent] = { mode: "thinking", text: short(run.task || "Thinking", 60), queued: 0 }
  }
  const open = tl.steps.filter((s) => s.startedAt <= t && (s.endedAt === null || s.endedAt > t))
  for (const step of open) if (agents[step.agent]) agents[step.agent] = { mode: "working", text: step.label, queued: 0 }
  for (const ticket of tickets) if (agents[ticket.request.agent]) agents[ticket.request.agent].queued += 1
  const busyRuns = tl.runs.filter((r) => r.startedAt <= t && (r.endedAt === null || r.endedAt > t))
  if (agents.main.mode !== "working" && busyRuns.length) {
    agents.main = { mode: "waiting", text: `Waiting for ${busyRuns.map((r) => NAMES[r.agent] ?? r.agent).join(", ")}`, queued: 0 }
  }
  if (t >= tl.end && tl.steps.length && agents.main.mode !== "working") agents.main = { mode: "done", text: "The goal ended", queued: 0 }
  const claude = Object.values(agents).some((a) => (a.mode === "working" || a.mode === "thinking") && a.queued === 0)

  const board: BoardCounts = { found: 0, researched: 0, checked: 0, fit: 0 }
  for (const b of tl.board) if (b.at <= t) board[b.key] += b.n
  const spend = tl.spend.filter((s) => s.at <= t).at(-1)?.usd ?? 0
  const step = tl.steps.filter((s) => s.startedAt <= t).at(-1) ?? null
  return {
    agents, claude, spend, board, tickets, workers, reader, dispatcher, gate,
    history: { reused, flash }, sites, linkedin, movers, mail: tl.mail.filter((m) => m.at <= t), step,
  }
}

export const NAMES: Record<string, string> = {
  main: "Scout",
  "candidate-profile": "Iris",
  "job-discovery": "Hunter",
  "job-research": "Sage",
  "fit-check": "Judge",
  "application-prep": "Penn",
  gate: "Gate",
  "career-lens": "career-lens",
}
