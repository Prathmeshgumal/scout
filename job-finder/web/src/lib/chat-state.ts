import type { GoalEvent, GoalStatus } from "./api"

/** A subagent run. The chat shows one line for it, not a log of its steps. */
export type AgentLine = { kind: "agent"; callId: string; name: string; task: string; state: "running" | "done" | "failed" | "stopped"; summary?: string }

export type Part =
  | { kind: "text"; text: string; draft?: boolean }
  | AgentLine
  | { kind: "notice"; text: string }
  | { kind: "error"; text: string }

/** closed: the response of the main agent ended. Its next response starts a new message. */
export type ChatMessage = { id: string; role: "user" | "assistant"; parts: Part[]; closed?: boolean }

export type GoalView = { status: GoalStatus; messages: ChatMessage[]; lastSeq: number }

const str = (value: unknown) => (typeof value === "string" ? value : "")

function onAssistant(messages: ChatMessage[], change: (parts: Part[]) => Part[], key: string): ChatMessage[] {
  const last = messages[messages.length - 1]
  if (!last || last.role !== "assistant" || last.closed) return [...messages, { id: `a-${key}`, role: "assistant", parts: change([]) }]
  return [...messages.slice(0, -1), { ...last, parts: change(last.parts) }]
}

function finishState(event: GoalEvent): AgentLine["state"] {
  if (event.ok) return "done"
  return event.status === "stopped" || event.status === "killed" ? "stopped" : "failed"
}

/** Notices that are messages to the main agent. The user does not read these. */
const FOR_THE_AGENT = new Set(["scrapes_finished", "wrap_up", "time_limit", "time_continue", "continue", "restart", "retry"])

export function applyEvent(view: GoalView, event: GoalEvent): GoalView {
  const next: GoalView = { ...view, lastSeq: typeof event.seq === "number" ? event.seq : view.lastSeq }
  const key = String(event.seq ?? view.messages.length)
  const add = (part: Part): GoalView => ({ ...next, messages: onAssistant(view.messages, (parts) => [...parts, part], key) })
  // A retry that fails for the same reason does not add the same error again.
  const addError = (text: string): GoalView => {
    const last = view.messages.at(-1)?.parts.at(-1)
    return last?.kind === "error" && last.text === text ? next : add({ kind: "error", text })
  }
  switch (event.type) {
    case "user_message":
      return { ...next, messages: [...view.messages, { id: `u-${key}`, role: "user", parts: [{ kind: "text", text: str(event.text) }] }] }
    case "text_delta":
      return {
        ...next,
        messages: onAssistant(view.messages, (parts) => {
          const last = parts[parts.length - 1]
          if (last?.kind === "text" && last.draft) return [...parts.slice(0, -1), { ...last, text: last.text + str(event.text) }]
          return [...parts, { kind: "text", text: str(event.text), draft: true }]
        }, key),
      }
    case "text":
      return {
        ...next,
        messages: onAssistant(view.messages, (parts) => {
          const last = parts[parts.length - 1]
          const final: Part = { kind: "text", text: str(event.text) }
          return last?.kind === "text" && last.draft ? [...parts.slice(0, -1), final] : [...parts, final]
        }, key),
      }
    case "agent_started":
      return add({ kind: "agent", callId: str(event.tool_use_id), name: str(event.name), task: str(event.task), state: "running" })
    case "agent_finished": {
      const id = str(event.tool_use_id)
      const name = str(event.name)
      let found = false
      const messages = [...view.messages].reverse().map((message) => ({
        ...message,
        parts: [...message.parts].reverse().map((part) => {
          if (found || part.kind !== "agent") return part
          if (id ? part.callId !== id : part.name !== name || part.state !== "running") return part
          found = true
          return { ...part, state: finishState(event), summary: str(event.summary) }
        }).reverse(),
      })).reverse()
      return { ...next, messages }
    }
    case "status_changed": {
      const to = str(event.to) as GoalStatus
      if (to === "failed") return { ...addError(str(event.reason) || "The goal failed."), status: to }
      if (to === "capped" || to === "stopped") return { ...add({ kind: "notice", text: str(event.reason) || "The goal stopped." }), status: to }
      return { ...next, status: to }
    }
    case "response_finished": {
      const last = view.messages.at(-1)
      if (last?.role !== "assistant") return next
      return { ...next, messages: [...view.messages.slice(0, -1), { ...last, closed: true }] }
    }
    case "notice": {
      const trigger = str(event.trigger)
      const text = str(event.text)
      if (!text || FOR_THE_AGENT.has(trigger)) return next
      return add({ kind: "notice", text: trigger === "wait_expired" ? text.split("\n")[0].replace(/:$/, "") : text })
    }
    case "error":
      return addError(str(event.text))
    default:
      return next
  }
}

export function fromEvents(status: GoalStatus, events: GoalEvent[]): GoalView {
  return { ...events.reduce(applyEvent, { status, messages: [], lastSeq: 0 }), status }
}

/** A line that shows as running only while the goal works. After a stop, its work stopped. */
export function shownState(line: AgentLine, status: GoalStatus): AgentLine["state"] {
  return line.state === "running" && status !== "running" ? "stopped" : line.state
}

export function runningAgents(view: GoalView): AgentLine[] {
  return view.messages.flatMap((m) => m.parts.filter((p): p is AgentLine => p.kind === "agent" && shownState(p, view.status) === "running"))
}
