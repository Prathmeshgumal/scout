import type { GoalEvent, GoalStatus } from "./api"
import { applyEvent, fromEvents, type GoalView } from "./chat-state"

export type ViewsState = Record<string, GoalView>

export type ViewsAction =
  | { type: "loaded"; id: string; status: GoalStatus; events: GoalEvent[] }
  | { type: "event"; id: string; event: GoalEvent }

export function viewsReducer(state: ViewsState, action: ViewsAction): ViewsState {
  if (action.type === "loaded") return { ...state, [action.id]: fromEvents(action.status, action.events) }
  const current = state[action.id] ?? fromEvents("running", [])
  const seq = action.event.seq
  if (typeof seq === "number" && seq <= current.lastSeq) return state // already applied
  return { ...state, [action.id]: applyEvent(current, action.event) }
}

export const canStop = (status: GoalStatus | undefined) => status === "running" || status === "waiting"
export const canRetry = (status: GoalStatus | undefined) => status === "failed" || status === "stopped"

/** A reply that applies only while its goal is still open. A late reply for another goal does nothing. */
export function onlyFor<T>(current: () => string | null, id: string, apply: (value: T) => void) {
  return (value: T) => { if (current() === id) apply(value) }
}
