export type GoalStatus = "running" | "waiting" | "done" | "capped" | "failed" | "stopped"

export type GoalEvent = { seq: number | null; at?: string | null; type: string } & Record<string, unknown>

export type GoalSummary = {
  id: string
  title: string
  status: GoalStatus
  agent: string | null
  created_at: string
  updated_at: string
  pending_scrapes: number
  spend_usd: number
  max_spend_usd: number
  jobs: number
  fit: number
  target: number | null
  /** Set on a run of a mandate: the id of the mandate. */
  mandate_id: string | null
  run_number: number | null
  /** Set on the chat of a mandate: the id of the mandate. */
  home_of: string | null
}

/** A file that the user uploaded. `readable` is false when the agents cannot read it. */
export type FileInfo = {
  id: string
  goal_id: string | null
  role: "attachment" | "resume"
  name: string
  kind: string
  size_bytes: number
  readable: boolean
}

export type GoalFiles = { resume: FileInfo | null; files: FileInfo[] }

export type Pipeline = {
  jobs: number
  researched: number
  checked: number
  fit: number
  partial: number
  not_fit: number
  unsure: number
  noted: number
  target: number | null
}

export type Brief = { title?: string; criteria: string; target_count: number }

export type Goal = GoalSummary & {
  status_reason: string | null
  brief: Brief | null
  model?: string | null
  effort?: string | null
}

export type TimeLimitCheck = { ok: boolean; reason: string | null; run_s: number; limit_s: number; mandate_limit_s: number }

export type GoalDetail = {
  goal: Goal
  pipeline: Pipeline
  busy: boolean
  tasks: { name: string }[]
  scrapes: { fresh: number; pending: number }
  time_limit?: TimeLimitCheck | null
  events: GoalEvent[]
}

export type CareerLensState = "checking" | "online" | "unreachable" | "error" | "access_denied" | "not_configured"

export type LoginMode = "api_key" | "oauth_token"
export type LoginResult = { restarting: boolean; mode?: LoginMode }

export type Status = {
  anthropic_configured: boolean
  claude_login: string
  claude_account?: {
    account_label: string | null
    saved_at: string | null
    loaded_at: string | null
    restart_required: boolean
    check_error: boolean
    saved?: Record<LoginMode, boolean>
  }
  career_lens_configured: boolean
  career_lens?: { state: CareerLensState; checked_at: string | null }
  open_sessions: number
}

export type Verdict = "fit" | "partial" | "unsure" | "not_fit"
export type Evidence = "full_description" | "snippet" | "listing_only"

/** What a card needs. It comes from the report of a goal or from the list of all roles. */
export type JobCardData = {
  id: string
  goal_id: string
  goal_title?: string
  title: string
  company_name: string
  location: string | null
  workplace_type: string | null
  posted_at: string | null
  posting_status: string | null
  url: string
  source: string
  evidence_level: Evidence
  observed_at: string | null
  fit: Verdict | null
  fit_reasons: string | null
  researched_at?: string | null
  researched?: boolean
  created_at: string
  extra?: boolean
}

export type CompanyResearch = {
  kind: string
  data: Record<string, unknown>
  sources: string[]
  created_at: string
  from_goal: string | null
}

export type ApplicationNote = { why_match: string; resume_points: string; gaps: string; created_at: string }

export type PipelineStage = "fit" | "referral" | "applied" | "responded"
export type PipelineNote = { id: string; text: string; created_at: string }

/** A role in the Job pipeline. job_id and goal_id are null when the goal of the role is deleted. */
export type PipelineItem = {
  role_key: string
  stage: PipelineStage
  job_id: string | null
  goal_id: string | null
  goal_title: string | null
  title: string
  company_name: string
  location: string | null
  workplace_type: string | null
  posting_status: string | null
  url: string
  evidence_level?: Evidence
  fit: Verdict | null
  fit_reasons: string | null
  updated_at: string | null
  notes: PipelineNote[]
}

export type JobDetail = JobCardData & {
  pipeline: { role_key: string; stage: PipelineStage | null }
  source_url: string | null
  snippet: string | null
  description_text: string | null
  found_note: string | null
  fit_evidence: string[]
  company: { website: string | null; linkedin_url: string | null }
  research: CompanyResearch[]
  note: ApplicationNote | null
  notes: { agent: string; kind: string; text: string; at: string }[]
}

export type Warning = { kind: string; text: string }

export type Report = {
  goal: {
    id: string; title: string; status: GoalStatus; status_reason: string | null
    spend_usd: number; max_spend_usd: number; mandate_id: string | null
  }
  title: string
  request: string
  brief: Brief | null
  pipeline: Pipeline
  funnel: { found: number; researched: number; checked: number; fit: number }
  waits: { kind: string; target: string; status: string; reason: string | null; since: string }[]
  warnings: Warning[]
  now: string
  searches: string[]
  jobs: JobDetail[]
  timing: { started_at: string; finished_at: string | null; work_seconds: number; limit_s: number | null }
}

export type Mandate = {
  id: string
  home_goal_id: string
  title: string
  instruction: string
  status: "active" | "paused" | "ended"
  every_unit: "hour" | "day" | "month"
  every_count: number
  jobs_per_run: number
  window_days: number[]
  window_start: string
  timezone: string
  max_spend_per_run: number
  max_scrapes_per_run: number
  next_run_at: string | null
}

export type ModelInfo = { id: string; label: string; detail: string; efforts?: string[]; badge?: string }
export type ModelChoice = { model: string; effort: string }
export type EffortInfo = { id: string; label: string }
export type ModelCatalog = { models: ModelInfo[]; efforts: EffortInfo[]; default: ModelChoice }

export type DeletePreview = { goals: number; jobs: number; notes: number; working: boolean }

/** Waits between reconnect tries: long enough to ride out a server restart. */
export const RECONNECT_DELAYS_MS: readonly number[] = [1000, 2000, 4000, 8000, 8000, 8000]

export function parseSSE(buffer: string): { events: GoalEvent[]; rest: string } {
  const chunks = buffer.split("\n\n")
  const rest = chunks.pop() ?? ""
  const events: GoalEvent[] = []
  for (const chunk of chunks) {
    for (const line of chunk.split("\n")) {
      if (line.startsWith("data: ")) events.push(JSON.parse(line.slice(6)) as GoalEvent)
    }
  }
  return { events, rest }
}

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

async function errorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body.detail === "string") return body.detail
    if (Array.isArray(body.detail) && body.detail[0]?.msg) return String(body.detail[0].msg)
    return response.statusText
  } catch {
    return response.statusText
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) throw new ApiError(await errorDetail(response), response.status)
  return (await response.json()) as T
}

const postJson = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
})

/** The time zone of the user. A new mandate plans its goals in it. */
const userZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone

export const getStatus = () => request<Status>("/api/status")
export const saveLogin = (credential: string, label = "") =>
  request<LoginResult>("/api/login", postJson({ credential, account_label: label }))
export const chooseLogin = (mode: LoginMode) => request<LoginResult>("/api/login/use", postJson({ mode }))
export const startSubscriptionLogin = () => request<{ url: string }>("/api/login/subscription/start", { method: "POST" })
export const finishSubscriptionLogin = (code: string, label = "") =>
  request<LoginResult>("/api/login/subscription/code", postJson({ code, account_label: label }))
export const listGoals = () => request<GoalSummary[]>("/api/goals")
export const listJobs = () => request<JobCardData[]>("/api/jobs")
export const listMandates = () => request<Mandate[]>("/api/mandates")
export const getGoal = (id: string) => request<GoalDetail>(`/api/goals/${id}`)
export const getReport = (id: string) => request<Report>(`/api/goals/${id}/report`)
export const getJob = (goalId: string, jobId: string) => request<JobDetail>(`/api/goals/${goalId}/jobs/${jobId}`)
export const getModels = () => request<ModelCatalog>("/api/models")
export const createGoal = (text: string, choice?: ModelChoice, fileIds: string[] = []) =>
  request<{ goal_id: string }>("/api/goals", postJson({ text, ...choice, timezone: userZone(), file_ids: fileIds }))
export const setGoalModel = (id: string, choice: ModelChoice) =>
  request<{ ok: boolean }>(`/api/goals/${id}/model`, { ...postJson(choice), method: "PUT" })
export const sendMessage = (id: string, text: string, fileIds: string[] = []) =>
  request<{ ok: boolean }>(`/api/goals/${id}/messages`, postJson({ text, timezone: userZone(), file_ids: fileIds }))
export const stopGoal = (id: string) => request<{ ok: boolean }>(`/api/goals/${id}/stop`, { method: "POST" })
export const retryGoal = (id: string) => request<{ ok: boolean }>(`/api/goals/${id}/retry`, { method: "POST" })
export const continueGoal = (id: string) => request<{ ok: boolean }>(`/api/goals/${id}/continue`, { method: "POST" })
export const reportMarkdownUrl = (id: string) => `/api/goals/${id}/report.md?tz=${encodeURIComponent(userZone())}`

export const goalDeletePreview = (id: string) => request<DeletePreview>(`/api/goals/${id}/delete-preview`)
export const mandateDeletePreview = (id: string) => request<DeletePreview>(`/api/mandates/${id}/delete-preview`)

async function remove(path: string): Promise<void> {
  const response = await fetch(path, { method: "DELETE" })
  if (!response.ok) throw new ApiError(await errorDetail(response), response.status)
}
export const deleteFile = (id: string) => remove(`/api/files/${id}`)
export const fileUrl = (id: string) => `/api/files/${id}/download`
export const listFiles = (goalId?: string | null) =>
  request<GoalFiles>(goalId ? `/api/files?goal_id=${goalId}` : "/api/files")
export function uploadFile(file: File, role: "attachment" | "resume" = "attachment") {
  const body = new FormData()
  body.append("file", file)
  body.append("role", role)
  return request<FileInfo>("/api/files", { method: "POST", body })
}
export const getPipeline = () => request<{ items: PipelineItem[] }>("/api/pipeline")
export const setPipelineStage = (roleKey: string, stage: PipelineStage | "removed") =>
  request<unknown>("/api/pipeline/stage", { ...postJson({ role_key: roleKey, stage }), method: "PUT" })
export const addPipelineNote = (roleKey: string, text: string) =>
  request<PipelineNote>("/api/pipeline/notes", postJson({ role_key: roleKey, text }))
export const deletePipelineNote = (id: string) => remove(`/api/pipeline/notes/${id}`)
export const deleteGoal = (id: string) => remove(`/api/goals/${id}`)
export const deleteMandate = (id: string) => remove(`/api/mandates/${id}`)

/** Stream the events of a goal after `after`. It resolves when the server closes the stream. */
export async function watchGoal(id: string, after: number, onEvent: (event: GoalEvent) => void, signal?: AbortSignal): Promise<void> {
  const response = await fetch(`/api/goals/${id}/stream?after=${after}`, { signal })
  if (!response.ok || !response.body) throw new ApiError(await errorDetail(response), response.status)
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ""
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    const parsed = parseSSE(buffer)
    buffer = parsed.rest
    parsed.events.forEach(onEvent)
  }
}
