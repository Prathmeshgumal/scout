export type Section = "chats" | "mandates" | "jobs" | "pipeline" | "settings"

/** What the URL opens. A goal has two tabs: Chat and Jobs. */
export type Route = { section: Section; goalId: string | null; mandateId: string | null; tab: "chat" | "jobs" }

const ID = "([0-9a-f-]{36})"
const GOAL = new RegExp(`^/goals/${ID}(/jobs)?/?$`, "i")
const MANDATE = new RegExp(`^/mandates/${ID}/?$`, "i")

export function parseRoute(pathname: string): Route {
  const goal = GOAL.exec(pathname)
  if (goal) return { section: "chats", goalId: goal[1].toLowerCase(), mandateId: null, tab: goal[2] ? "jobs" : "chat" }
  const mandate = MANDATE.exec(pathname)
  if (mandate) return { section: "mandates", goalId: null, mandateId: mandate[1].toLowerCase(), tab: "chat" }
  if (/^\/mandates\/?$/i.test(pathname)) return { section: "mandates", goalId: null, mandateId: null, tab: "chat" }
  if (/^\/jobs\/?$/i.test(pathname)) return { section: "jobs", goalId: null, mandateId: null, tab: "chat" }
  if (/^\/pipeline\/?$/i.test(pathname)) return { section: "pipeline", goalId: null, mandateId: null, tab: "chat" }
  if (/^\/settings\/?$/i.test(pathname)) return { section: "settings", goalId: null, mandateId: null, tab: "chat" }
  return { section: "chats", goalId: null, mandateId: null, tab: "chat" }
}

export function routePath(route: Route): string {
  if (route.goalId) return `/goals/${route.goalId}${route.tab === "jobs" ? "/jobs" : ""}`
  if (route.mandateId) return `/mandates/${route.mandateId}`
  if (route.section === "mandates") return "/mandates"
  if (route.section === "pipeline") return "/pipeline"
  if (route.section === "settings") return "/settings"
  return route.section === "jobs" ? "/jobs" : "/"
}
