export type Section = "overview" | "requests" | "workers" | "accounts" | "keys" | "try"
export type Route = { section: Section; requestId: string | null }

const SECTIONS: Section[] = ["overview", "requests", "workers", "accounts", "keys", "try"]
const BASE = "/console"

export function parseRoute(path: string): Route {
  const [section, id] = path.replace(/^\/console\/?/, "").split("/")
  if (!SECTIONS.includes(section as Section)) return { section: "overview", requestId: null }
  return { section: section as Section, requestId: section === "requests" && id ? decodeURIComponent(id) : null }
}

export function routePath(route: Route): string {
  if (route.requestId) return `${BASE}/requests/${encodeURIComponent(route.requestId)}`
  return route.section === "overview" ? BASE : `${BASE}/${route.section}`
}
