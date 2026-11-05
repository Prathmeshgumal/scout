export type ApiKey = {
  id: string
  name: string
  prefix: string
  created_at: string
  revoked_at: string | null
}

export type RequestRow = {
  id: string
  kind: string
  status: string
  label: string | null
  target: string | null
  requested_at: string
  wait_reason: string | null
  attempts: number
}
export type RequestPage = { items: RequestRow[]; counts: Record<string, number>; next: string | null }

export type RunEvent = {
  id: number
  attempt: number
  at: string
  elapsed_ms: number
  duration_ms: number | null
  kind: string
  level: string
  phase: string
  detail: string | null
  failed: boolean
}

export type RequestDetail = {
  request_id: string
  kind: string
  status: string
  label: string | null
  url: string | null
  query: string | null
  requested_at: string
  started_at: string | null
  finished_at: string | null
  failure: { kind: string; detail: string | null } | null
  max_attempts: number
  next_attempt_at: string | null
  wait_reason: string | null
  cancel_requested_at: string | null
  satisfied_at: string | null
  attempts: { n: number; started_at: string | null; ended_at: string | null; cause: string | null; cause_detail: string | null }[]
  captures: { id: string; section: string | null; url_final: string | null; http_status: number | null; captured_at: string; attempt: number | null }[]
  extraction: { id: string; extractor: string; version: string; extracted_at: string; outcome: string } | null
  sections: { section?: string; name?: string; status?: string; outcome?: string }[]
  events: RunEvent[]
  screenshots: { id: string; capture_id: string; ordinal: number }[]
}

export type Assignment = {
  id: string
  account_id: string
  worker_id: string
  state: string
  detail: string | null
  waiting_for_you: boolean
  action: string | null
}

export type GooglePolicy = {
  enabled: boolean
  max_searches_hour: number
  max_searches_24h: number
  min_interval_seconds: number
}

export type Worker = {
  id: string
  name: string
  supervisor: string
  state: string
  health: string
  health_detail: string | null
  live: boolean
  last_heartbeat_at: string | null
  google: { capable: boolean; policy: GooglePolicy } | null
  assignment: Assignment | null
}

export type ShiftDay = {
  weekday: number
  working: boolean
  start_local: string | null
  end_local: string | null
  lunch_minutes: number
  break_count: number
  break_minutes: number
}

export type Schedule = {
  enabled: boolean
  timezone: string
  days: ShiftDay[]
  start_jitter_minutes: number
  end_jitter_minutes: number
}

export type Account = {
  id: string
  label: string
  username: string
  state: string
  max_attempts_24h: number
  min_interval_seconds: number
  cooldown_until: string | null
  attention_detail: string | null
  assignment: Assignment | null
  schedule: Schedule | null
  attempts_24h: number
}

export type Overview = { workers: Worker[]; accounts: Account[]; requests: { id: string; kind: string; status: string; target: string | null; requested_at: string; wait_reason: string | null }[] }

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

/** Plain words for the codes of the sign-in routes. */
const CODES: Record<string, string> = {
  wrong_password: "The password is wrong.",
  too_many_attempts: "Too many wrong passwords. Wait one minute.",
  operator_password_not_set: "Set CAREER_LENS_OPERATOR_PASSWORD on the server.",
  not_signed_in: "Sign in again.",
}

async function detail(response: Response): Promise<string> {
  try {
    const body = await response.json()
    if (typeof body.detail === "string") return body.detail
    if (body.detail?.code) return CODES[body.detail.code] ?? body.detail.code
    if (Array.isArray(body.detail) && body.detail[0]?.msg) return String(body.detail[0].msg)
  } catch { /* not JSON */ }
  return response.statusText || `Error ${response.status}`
}

let onSignedOut = () => {}
export const whenSignedOut = (action: () => void) => { onSignedOut = action }

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/operator/api${path}`, init)
  if (response.status === 401 && !path.startsWith("/login")) onSignedOut()
  if (!response.ok) throw new ApiError(await detail(response), response.status)
  return (response.status === 204 ? undefined : await response.json()) as T
}

const send = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
})

export const session = () => call<{ signed_in: boolean }>("/session")
export const signIn = (password: string) => call<void>("/login", send("POST", { password }))
export const signOut = () => call<void>("/logout", send("POST"))
export const overview = () => call<Overview>("/overview")
export const requestDetail = (id: string) => call<RequestDetail>(`/requests/${encodeURIComponent(id)}`)
export const cancelRequest = (id: string) => call<{ status: string; cancel: "cancelled" | "stopping" }>(`/requests/${encodeURIComponent(id)}/cancel`, send("POST"))
export const requestResult = (id: string) => call<unknown>(`/requests/${encodeURIComponent(id)}/result`)
export const screenshotUrl = (id: string) => `/operator/api/artifacts/${encodeURIComponent(id)}`

export function requests(filters: { status?: string; kind?: string; q?: string; before?: string }) {
  const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value) as [string, string][])
  return call<RequestPage>(`/requests?${query}`)
}

export const apiKeys = () => call<ApiKey[]>("/api-keys")
export const createApiKey = (name: string) => call<ApiKey & { token: string }>("/api-keys", send("POST", { name }))
export const revokeApiKey = (id: string) => call<ApiKey>(`/api-keys/${encodeURIComponent(id)}/revoke`, send("POST"))
export const addAccount = (body: unknown) => call<Account>("/accounts", send("POST", body))
export const changePassword = (id: string, password: string) => call<unknown>(`/accounts/${id}/password`, send("PUT", { password }))
export const saveLimits = (id: string, body: { max_attempts_24h: number; min_interval_seconds: number }) => call<unknown>(`/accounts/${id}/limits`, send("PUT", body))
export const saveShift = (id: string, body: unknown) => call<unknown>(`/accounts/${id}/shift`, send("PUT", body))
export const assign = (workerId: string, accountId: string) => call<unknown>(`/workers/${workerId}/assignments`, send("POST", { account_id: accountId }))
export const assignmentCommand = (id: string, command: string) => call<unknown>(`/assignments/${id}/${command}`, send("POST"))
export const patchGoogle = (workerId: string, body: Partial<GooglePolicy>) => call<unknown>(`/workers/${workerId}/google-policy`, send("PATCH", body))
export const tryRequest = (path: string, body: unknown) => call<{ request_id: string }>(`/try/${path}`, send("POST", body))
