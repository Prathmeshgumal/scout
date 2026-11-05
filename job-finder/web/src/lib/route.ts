export type Section = "goals" | "mandates" | "jobs" | "pipeline" | "metrics"

export type GoalTab = "chat" | "jobs" | "activity"

/** What the URL opens. A goal has three tabs: Chat, Jobs and Activity. step: the step to select in Activity. The URL does not keep it. A role of the Job pipeline or of the Jobs page has its own page. highlight: the card to show on the board. The URL does not keep it. */
export type Route = { section: Section; goalId: string | null; mandateId: string | null; tab: GoalTab; step?: string | null; roleKey?: string | null; job?: { goalId: string; jobId: string } | null; highlight?: string | null }

const ID = "([0-9a-f-]{36})"
const GOAL = new RegExp(`^/goals/${ID}(/jobs|/activity)?/?$`, "i")
const MANDATE = new RegExp(`^/mandates/${ID}/?$`, "i")
const JOB = new RegExp(`^/jobs/${ID}/${ID}/?$`, "i")
const ROLE = /^\/pipeline\/([^/]+)\/?$/i

function decoded(text: string): string | null {
  try {
    return decodeURIComponent(text)
  } catch {
    return null
  }
}

export function parseRoute(pathname: string): Route {
  const goal = GOAL.exec(pathname)
  if (goal) return { section: "goals", goalId: goal[1].toLowerCase(), mandateId: null, tab: goal[2] ? (goal[2].slice(1).toLowerCase() as GoalTab) : "chat" }
  const mandate = MANDATE.exec(pathname)
  if (mandate) return { section: "mandates", goalId: null, mandateId: mandate[1].toLowerCase(), tab: "chat" }
  if (/^\/mandates\/?$/i.test(pathname)) return { section: "mandates", goalId: null, mandateId: null, tab: "chat" }
  const job = JOB.exec(pathname)
  if (job) return { section: "jobs", goalId: null, mandateId: null, tab: "chat", job: { goalId: job[1].toLowerCase(), jobId: job[2].toLowerCase() } }
  if (/^\/jobs\/?$/i.test(pathname)) return { section: "jobs", goalId: null, mandateId: null, tab: "chat" }
  const role = ROLE.exec(pathname)
  if (role) return { section: "pipeline", goalId: null, mandateId: null, tab: "chat", roleKey: decoded(role[1]) }
  if (/^\/pipeline\/?$/i.test(pathname)) return { section: "pipeline", goalId: null, mandateId: null, tab: "chat" }
  if (/^\/metrics\/?$/i.test(pathname)) return { section: "metrics", goalId: null, mandateId: null, tab: "chat" }
  return { section: "goals", goalId: null, mandateId: null, tab: "chat" }
}

export function routePath(route: Route): string {
  if (route.goalId) return `/goals/${route.goalId}${route.tab === "chat" ? "" : `/${route.tab}`}`
  if (route.mandateId) return `/mandates/${route.mandateId}`
  if (route.section === "mandates") return "/mandates"
  if (route.section === "metrics") return "/metrics"
  if (route.section === "pipeline") return route.roleKey ? `/pipeline/${encodeURIComponent(route.roleKey)}` : "/pipeline"
  if (route.section === "jobs") return route.job ? `/jobs/${route.job.goalId}/${route.job.jobId}` : "/jobs"
  return "/"
}
