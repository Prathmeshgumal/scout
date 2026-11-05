import { Check, CircleSlash, Loader2, TriangleAlert } from "lucide-react"
import type { StepState } from "@/lib/chat-state"
import { cn } from "@/lib/utils"

/** One color for each agent, the same in the chat, the step list and the lanes. */
const COLORS: Record<string, string> = {
  main: "bg-zinc-500 dark:bg-zinc-400",
  "candidate-profile": "bg-sky-500 dark:bg-sky-400",
  "job-discovery": "bg-violet-500 dark:bg-violet-400",
  "job-research": "bg-amber-500 dark:bg-amber-400",
  "fit-check": "bg-teal-500 dark:bg-teal-400",
  "application-prep": "bg-pink-500 dark:bg-pink-400",
}
const OTHER = ["bg-indigo-500 dark:bg-indigo-400", "bg-lime-600 dark:bg-lime-400", "bg-orange-500 dark:bg-orange-400"]

export function agentColor(agent: string): string {
  if (COLORS[agent]) return COLORS[agent]
  let hash = 0
  for (const c of agent) hash = (hash * 31 + c.charCodeAt(0)) >>> 0
  return OTHER[hash % OTHER.length]
}

export function AgentDot({ agent, className }: { agent: string; className?: string }) {
  return <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", agentColor(agent), className)} />
}

export const STATE_WORD: Record<StepState, string> = { running: "working", done: "finished", failed: "failed", stopped: "stopped" }

export function StateIcon({ state, className }: { state: StepState; className?: string }) {
  const Icon = state === "running" ? Loader2 : state === "done" ? Check : state === "failed" ? TriangleAlert : CircleSlash
  return (
    <Icon aria-label={STATE_WORD[state]} className={cn("size-3.5 shrink-0", state === "running" && "animate-spin text-violet-500",
      state === "done" && "text-emerald-600 dark:text-emerald-400", state === "failed" && "text-red-600 dark:text-red-400",
      state === "stopped" && "text-muted-foreground", className)} />
  )
}

/** "4 s", "1 min 5 s", "1 h 2 min". */
export function duration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s} s`
  if (s < 3600) return `${Math.floor(s / 60)} min${s % 60 ? ` ${s % 60} s` : ""}`
  return `${Math.floor(s / 3600)} h${Math.floor((s % 3600) / 60) ? ` ${Math.floor((s % 3600) / 60)} min` : ""}`
}
